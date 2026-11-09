#!/usr/bin/env python3
"""FrameX bulk downloader — banyak URL, satu musim penuh, atau semua episode.

Contoh:
    python bulk.py "https://framextv.xyz/tv/1399" --season 1
    python bulk.py "https://framextv.xyz/tv/1399" --season 1 --episodes 1-5,8
    python bulk.py "https://framextv.xyz/anime/182255" --all-episodes
    python bulk.py -f senarai.txt --jobs 3 --subs all
    python bulk.py 550 1399 --season 2 --quality 1080p
    python bulk.py -f senarai.txt --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

import framextv as ftv


# ── Model ───────────────────────────────────────────────────────────────────

@dataclass
class Job:
    index: int
    link: ftv.Link
    details: dict
    base: str
    status: str = "queued"
    message: str = ""
    percent: float = 0.0
    bytes: float = 0.0
    speed: float = 0.0
    eta: float = 0.0
    filename: str = ""
    source: str = ""


@dataclass
class Results:
    done: int = 0
    skipped: int = 0
    failed: list[tuple[Job, str]] = field(default_factory=list)


# ── Input & pelan ───────────────────────────────────────────────────────────

def read_urls(args) -> list[str]:
    urls = list(args.urls)
    for name in args.file:
        if name == "-":
            text = sys.stdin.read()
        else:
            path = Path(name).expanduser()
            if not path.is_file():
                sys.exit(f"Fail tidak dijumpai: {path}")
            text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                urls.append(line)

    seen: set[str] = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    if not unique:
        sys.exit("Tiada URL. Guna argumen URL atau -f senarai.txt.")
    return unique


def parse_range_spec(spec: str) -> list[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = part.split("-", 1)
                a, b = int(start), int(end)
                if a > b:
                    a, b = b, a
                out.update(range(a, b + 1))
            else:
                out.add(int(part))
        except ValueError:
            raise ValueError(f"Format episode tidak sah: '{part}'")
    if not out:
        raise ValueError("Senarai episode kosong.")
    return sorted(out)


def apply_overrides(link: ftv.Link, args) -> ftv.Link:
    if args.season is not None:
        link.season = args.season
        if link.media_type == "movie":
            link.media_type = "tv"
    return link


def resolve_episodes(url_episode: Optional[int], spec: Optional[str],
                     all_episodes: bool, count: int,
                     label: str) -> list[int]:
    if spec:
        return parse_range_spec(spec)
    if all_episodes:
        if count <= 0:
            raise ValueError(
                f"{label}: jumlah episode tidak diketahui. Guna --episodes 1-10."
            )
        return list(range(1, count + 1))
    if url_episode is not None:
        return [url_episode]
    if count > 1:
        return list(range(1, count + 1))
    return [1]


def make_job(details: dict, link: ftv.Link) -> Job:
    return Job(index=-1, link=link, details=details,
               base=ftv.media_base(link, details))


async def plan_url(session, raw: str, args) -> list[Job]:
    link = apply_overrides(ftv.parse_link(raw), args)

    if link.media_type == "anime":
        details = await ftv.fetch_anilist(session, link.media_id)
        total = int(details.get("total_episodes") or 1)
        is_movie = (details.get("format") or "").upper() == "MOVIE" or total == 1
        label = details.get("title") or f"Anime {link.media_id}"
        if is_movie and not args.episodes and not args.all_episodes:
            episodes = [link.episode or 1]
        else:
            episodes = resolve_episodes(link.episode, args.episodes,
                                        args.all_episodes, total, label)
        return [
            make_job(details, ftv.Link("anime", link.media_id, episode=ep))
            for ep in episodes
        ]

    if link.media_type == "tv":
        details = await ftv.fetch_details(session, link.media_id, "tv")
        seasons = [s for s in details.get("seasons", []) if isinstance(s, dict)]

        season = link.season
        if season is None and seasons:
            regular = sorted(
                (s for s in seasons if int(s.get("season_number") or 0) > 0),
                key=lambda s: int(s.get("season_number") or 0),
            )
            season = int((regular or seasons)[0].get("season_number") or 1)
        season = season if season is not None else 1

        season_meta = next(
            (s for s in seasons if int(s.get("season_number") or 0) == season),
            None,
        )
        count = int(season_meta.get("episode_count") or 0) if season_meta else 0
        if seasons and season_meta is None:
            available = ", ".join(str(s.get("season_number")) for s in seasons)
            raise ValueError(
                f"{details.get('title') or link.media_id}: Season {season} tiada. "
                f"Pilihan: {available}"
            )

        label = f"{details.get('title') or 'TV'} S{season:02d}"
        episodes = resolve_episodes(link.episode, args.episodes,
                                    args.all_episodes, count, label)
        return [
            make_job(details, ftv.Link("tv", link.media_id,
                                       season=season, episode=ep))
            for ep in episodes
        ]

    details = await ftv.fetch_details(session, link.media_id, "movie")
    return [make_job(details, ftv.Link("movie", link.media_id))]


async def plan_jobs(urls: list[str], args) -> list[Job]:
    jobs: list[Job] = []
    async with aiohttp.ClientSession(
        headers=ftv.DEFAULT_HEADERS,
        timeout=aiohttp.ClientTimeout(total=25),
    ) as session:
        for raw in urls:
            try:
                jobs.extend(await plan_url(session, raw, args))
            except ValueError as exc:
                print(f"  ! {raw}: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"  ! {raw}: gagal baca metadata — {exc}", file=sys.stderr)

    seen: set[tuple] = set()
    unique: list[Job] = []
    for job in jobs:
        key = (job.link.media_type, job.link.media_id,
               job.link.season, job.link.episode)
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)

    for i, job in enumerate(unique):
        job.index = i
    return unique


# ── Pilihan sumber & subtitle ───────────────────────────────────────────────

def parse_quality(value: str) -> int:
    m = re.search(r"(\d{3,4})", value or "")
    return int(m.group(1)) if m else 0


def quality_tag(quality: str) -> str:
    m = re.search(r"(\d{3,4})p", quality or "")
    return f"{m.group(1)}p" if m else "auto"


def pick_source(sources: list[ftv.Source], args) -> ftv.Source:
    if args.source is not None:
        if not 0 <= args.source < len(sources):
            raise RuntimeError(
                f"--source {args.source} luar julat (0..{len(sources) - 1})"
            )
        return sources[args.source]

    candidates = list(sources)
    if args.audio:
        match = [s for s in candidates
                 if (s.audio or "").lower() == args.audio.lower()]
        if match:
            candidates = match
    if args.prefer:
        match = [s for s in candidates
                 if args.prefer.lower() in s.label.lower()]
        if match:
            candidates = match
    if args.quality:
        target = parse_quality(args.quality)
        exact = [s for s in candidates if s.height == target]
        above = [s for s in candidates if s.height >= target]
        candidates = exact or above or candidates
    if not args.audio:
        subtitled = [s for s in candidates
                     if (s.audio or "").lower() == "sub"]
        if subtitled:
            candidates = subtitled

    return max(candidates, key=lambda s: (
        s.height, 1 if s.stream_type == "hls" else 0
    ))


def dedupe_subtitles(subtitles: list[ftv.Subtitle]) -> list[ftv.Subtitle]:
    chosen: dict[str, ftv.Subtitle] = {}
    order: list[str] = []
    for sub in subtitles:
        key = (ftv.canonical_language(sub.language)
               or ftv.canonical_language(sub.label)
               or ftv.sanitize(sub.label or sub.language or "sub").lower())
        if not key:
            continue
        if key not in chosen:
            chosen[key] = sub
            order.append(key)
        elif len(sub.language or "") > len(chosen[key].language or ""):
            chosen[key] = sub
    return [chosen[key] for key in order]


def pick_subtitles(subtitles: list[ftv.Subtitle], spec: str) -> list[ftv.Subtitle]:
    return ftv.select_subtitles(dedupe_subtitles(subtitles), spec)


def parse_duration(value: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", value or "")
    if not m or float(m.group(1)) <= 0:
        raise argparse.ArgumentTypeError(
            f"durasi tidak sah: '{value}' (cth: 60, 1m, 1m30s tidak disokong)"
        )
    scale = {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    return float(m.group(1)) * scale


def duration_tag(seconds: Optional[float]) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    if seconds % 60 == 0:
        return f" [first{seconds // 60}m]"
    return f" [first{seconds}s]"


def find_existing(out_dir: Path, base: str) -> Optional[Path]:
    if not out_dir.is_dir():
        return None
    for path in out_dir.iterdir():
        if not path.is_file() or path.suffix.lower() != ".mp4":
            continue
        if path.stem == base or path.stem.startswith(base + " ["):
            return path
    return None


# ── Papan progress ──────────────────────────────────────────────────────────

class Board:
    def __init__(self, jobs: list[Job], enabled: bool):
        self.jobs = jobs
        self.enabled = enabled
        self.painted = 0
        self.last = 0.0

    def update(self, job: Job, force: bool = False) -> None:
        self.render(force=force)

    def line(self, job: Job) -> str:
        head = f"[{job.index + 1:>{len(str(len(self.jobs)))}}/{len(self.jobs)}]"
        name = job.base if len(job.base) <= 58 else job.base[:55] + "…"

        if job.status == "queued":
            tail = "menunggu"
        elif job.status == "checking":
            tail = "menyemak sumber…"
        elif job.status == "downloading":
            tail = (f"{job.percent:5.1f}%  {ftv.fmt_size(job.bytes):>9}  "
                    f"{ftv.fmt_size(job.speed)}/s  ETA {ftv.fmt_eta(job.eta)}")
        elif job.status == "merging":
            tail = "menggabung MP4…"
        elif job.status == "trimming":
            tail = job.message or "memotong video…"
        elif job.status == "subtitles":
            tail = "muat subtitle…"
        elif job.status == "retrying":
            tail = f"cuba semula · {job.message[:48]}"
        elif job.status == "done":
            tail = f"SELESAI · {ftv.fmt_size(job.bytes)}"
        elif job.status == "skipped":
            tail = f"dilangkau · sudah ada"
        elif job.status == "failed":
            tail = f"GAGAL · {job.message[:56]}"
        else:
            tail = job.status
        return f"{head} {name:<58}  {tail}"

    def render(self, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self.last < 0.4:
            return
        self.last = now
        lines = [self.line(job) for job in self.jobs]
        buf = ""
        if self.painted:
            buf += f"\x1b[{self.painted}A"
        for line in lines:
            buf += "\x1b[2K" + line + "\n"
        self.painted = len(lines)
        sys.stderr.write(buf)
        sys.stderr.flush()


def report(job: Job, board: Board, message: str) -> None:
    if not board.enabled:
        width = len(str(len(board.jobs)))
        print(f"[{job.index + 1:>{width}}/{len(board.jobs)}] {job.base}: {message}",
              file=sys.stderr, flush=True)


async def render_loop(board: Board, stop: asyncio.Event) -> None:
    while not stop.is_set():
        board.render()
        try:
            await asyncio.wait_for(stop.wait(), 0.5)
        except asyncio.TimeoutError:
            pass


# ── Muat turun ──────────────────────────────────────────────────────────────

def progress_callback(job: Job, board: Board):
    def _cb(info: dict) -> None:
        job.status = info.get("status", job.status)
        job.percent = float(info.get("percent") or 0)
        job.bytes = float(info.get("bytes") or 0)
        job.speed = float(info.get("speed") or 0)
        job.eta = float(info.get("eta") or 0)
        board.update(job)
    return _cb


async def trim_remote_mp4(source: ftv.Source, output: Path, seconds: float) -> None:
    ffmpeg = ftv.find_ffmpeg()
    headers = ftv.build_headers(source)
    header_arg = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    cmd = [
        ffmpeg, "-y",
        "-headers", header_arg,
        "-i", source.url,
        "-t", f"{seconds:.3f}",
        "-c", "copy",
        "-movflags", "+faststart",
        str(output),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg potong gagal: {err.decode(errors='replace')[-400:]}"
        )


async def download_one(job: Job, args, board: Board, out_dir: Path) -> None:
    job.status = "checking"
    job.message = ""
    board.update(job, force=True)

    async with aiohttp.ClientSession(
        headers=ftv.DEFAULT_HEADERS,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        sources, subtitles = await ftv.fetch_sources(session, job.link)
        if not sources:
            raise RuntimeError("tiada sumber stream aktif")

        source = pick_source(sources, args)
        job.source = source.label
        board.update(job, force=True)

        stem = job.base + duration_tag(args.duration)
        name = f"{stem} [{quality_tag(source.quality)}]"
        output = out_dir / f"{name}.mp4"
        if output.exists():
            output = ftv.unique_path(out_dir, name, "mp4")
        job.filename = output.name

        job.status = "downloading"
        job.percent = 0.0
        board.update(job, force=True)

        is_mp4 = (source.stream_type.lower() == "mp4"
                  or source.url.split("?")[0].endswith(".mp4"))
        callback = progress_callback(job, board)
        if is_mp4 and args.duration:
            job.status = "trimming"
            job.message = f"memotong {int(args.duration)}s pertama…"
            board.update(job, force=True)
            await trim_remote_mp4(source, output, args.duration)
        elif is_mp4:
            await ftv.download_mp4(source, output, on_progress=callback)
        else:
            await ftv.download_hls(source, output, ftv.find_ffmpeg(),
                                   on_progress=callback, verbose=not board.enabled,
                                   max_duration=args.duration)

        selected = pick_subtitles(subtitles, args.subs)
        if selected:
            job.status = "subtitles"
            board.update(job, force=True)
            await ftv.download_subtitles(session, selected, output.stem, out_dir)

        job.status = "done"
        job.percent = 100.0
        job.speed = 0.0
        job.eta = 0.0
        job.bytes = float(output.stat().st_size)
        board.update(job, force=True)
        subs = f" +{len(selected)} subtitle" if selected else ""
        report(job, board, f"SELESAI · {ftv.fmt_size(job.bytes)}{subs} · {output.name}")


async def run_job(job: Job, args, sem: asyncio.Semaphore,
                  board: Board, results: Results, out_dir: Path) -> None:
    skip_base = job.base + duration_tag(args.duration)
    existing = find_existing(out_dir, skip_base)
    if existing and not args.overwrite:
        job.status = "skipped"
        job.filename = existing.name
        job.bytes = float(existing.stat().st_size)
        results.skipped += 1
        board.update(job, force=True)
        report(job, board, f"dilangkau · {existing.name} sudah ada")
        return

    async with sem:
        for attempt in range(args.retries + 1):
            try:
                await download_one(job, args, board, out_dir)
                results.done += 1
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt < args.retries:
                    job.status = "retrying"
                    job.message = str(exc)
                    board.update(job, force=True)
                    report(job, board, f"cuba semula ({attempt + 1}/{args.retries}): {exc}")
                    await asyncio.sleep(min(2 * (attempt + 1), 10))
                    continue
                job.status = "failed"
                job.message = str(exc)
                board.update(job, force=True)
                results.failed.append((job, str(exc)))
                report(job, board, f"GAGAL · {exc}")
                return


# ── Cetak pelan & sumber ────────────────────────────────────────────────────

def print_plan(jobs: list[Job], args) -> None:
    print(f"\n  {len(jobs)} fail akan dimuat turun · {args.jobs} serentak")
    filters = []
    if args.quality:
        filters.append(f"kualiti {args.quality}+")
    if args.audio:
        filters.append(f"audio {args.audio}")
    if args.prefer:
        filters.append(f"utamakan '{args.prefer}'")
    if args.source is not None:
        filters.append(f"sumber #{args.source}")
    if args.duration:
        filters.append(f"{int(args.duration)}s pertama sahaja")
    filters.append(f"subtitle {args.subs}")
    print(f"  {' · '.join(filters)}")
    print(f"  Output: {Path(args.out).expanduser().resolve()}\n")
    for job in jobs:
        print(f"   [{job.index + 1:>3}] {job.base}")
    print()


async def list_sources(jobs: list[Job], args) -> None:
    async with aiohttp.ClientSession(
        headers=ftv.DEFAULT_HEADERS,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        for job in jobs:
            print(f"\n  {job.base}")
            try:
                sources, subtitles = await ftv.fetch_sources(session, job.link)
            except Exception as exc:
                print(f"    ! gagal: {exc}")
                continue
            if not sources:
                print("    ! tiada sumber aktif")
                continue
            for i, source in enumerate(sources):
                print(f"    [{i}] {source.label}")
            langs = sorted({s.language for s in subtitles if s.language})
            print(f"    subtitle: {', '.join(langs) if langs else 'tiada'}")
    print()


# ── Main ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FrameX bulk downloader — banyak URL / musim penuh / semua episode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""contoh:
  %(prog)s "https://framextv.xyz/tv/1399" --season 1
  %(prog)s "https://framextv.xyz/tv/1399" --season 1 --episodes 1-5,8
  %(prog)s "https://framextv.xyz/anime/182255" --all-episodes --jobs 2
  %(prog)s -f senarai.txt --subs en,ms,id
  %(prog)s 550 1399 --season 2 --quality 1080p
  %(prog)s -f senarai.txt --dry-run
  %(prog)s -f senarai.txt --list-sources
""",
    )
    parser.add_argument("urls", nargs="*", help="Link FrameX atau TMDB/AniList ID")
    parser.add_argument("-f", "--file", action="append", default=[],
                        metavar="FILE",
                        help="Fail senarai URL (satu per baris, '#' untuk komen, '-' = stdin)")
    parser.add_argument("--season", type=int, help="Musim TV (lalai: dari URL / season 1)")
    parser.add_argument("--episodes", metavar="SPEC",
                        help="Julat episode, cth: 1-5,8,10-12")
    parser.add_argument("--all-episodes", action="store_true",
                        help="Muat semua episode (abaikan nombor episode dalam URL)")
    parser.add_argument("--jobs", type=int, default=3,
                        help="Bilangan muat turun serentak (lalai: 3)")
    parser.add_argument("--quality", metavar="RES",
                        help="Kualiti diutamakan, cth: 1080p / 720")
    parser.add_argument("--audio", choices=["sub", "dub"],
                        help="Pilih audio anime (sub/dub)")
    parser.add_argument("--prefer", metavar="TEXT",
                        help="Utamakan sumber yang labelnya mengandungi teks ini")
    parser.add_argument("--source", type=int,
                        help="Indeks sumber tetap (lihat --list-sources)")
    parser.add_argument("--duration", type=parse_duration, metavar="N[s|m|h]",
                        help="Had kepada N saat pertama sahaja, cth: 60 / 1m")
    parser.add_argument("--subs", default="all",
                        help="none | all | en,ms,id (lalai: all)")
    parser.add_argument("--out", default="downloads", help="Folder output")
    parser.add_argument("--overwrite", action="store_true",
                        help="Muat semula walaupun fail sudah ada")
    parser.add_argument("--retries", type=int, default=2,
                        help="Cubaan semula per episode (lalai: 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Tunjuk pelan sahaja, tanpa muat turun")
    parser.add_argument("--list-sources", action="store_true",
                        help="Senarai sumber stream bagi setiap fail dalam pelan")
    return parser


async def async_main(args) -> int:
    if args.jobs < 1:
        sys.exit("--jobs mesti sekurang-kurangnya 1.")

    urls = read_urls(args)
    print(f"  Membaca {len(urls)} URL…")
    jobs = await plan_jobs(urls, args)
    if not jobs:
        print("  Tiada fail untuk dimuat turun.", file=sys.stderr)
        return 1

    print_plan(jobs, args)

    if args.list_sources:
        await list_sources(jobs, args)
    if args.dry_run or args.list_sources:
        return 0

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tty = sys.stderr.isatty()
    board = Board(jobs, enabled=tty)
    results = Results()
    sem = asyncio.Semaphore(args.jobs)
    stop = asyncio.Event()
    renderer = asyncio.create_task(render_loop(board, stop))

    started = time.monotonic()
    try:
        await asyncio.gather(
            *(run_job(job, args, sem, board, results, out_dir) for job in jobs)
        )
    except asyncio.CancelledError:
        pass
    finally:
        stop.set()
        renderer.cancel()
        try:
            await renderer
        except asyncio.CancelledError:
            pass
        board.render(force=True)
        if board.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()

    elapsed = time.monotonic() - started
    print(f"  Selesai dalam {ftv.fmt_eta(elapsed)}")
    print(f"  Berjaya: {results.done} · Dilangkau: {results.skipped} · "
          f"Gagal: {len(results.failed)}")
    if results.failed:
        print("\n  Gagal:")
        for job, message in results.failed:
            print(f"    - {job.base}: {message[:300]}")
    return 0 if not results.failed else 1


def main() -> None:
    args = build_parser().parse_args()
    try:
        code = asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n  Dibatalkan.", file=sys.stderr)
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
