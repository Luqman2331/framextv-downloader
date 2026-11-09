#!/usr/bin/env python3
"""FrameX downloader — satu file CLI.

Guna:
    python framextv.py "https://framextv.xyz/watch/550"
    python framextv.py 1399 --season 1 --episode 3
    python framextv.py "https://framextv.xyz/anime/182255" --episode 2 --subs all
    python framextv.py <url> --list          # senarai sumber sahaja
    python framextv.py <url> --source 2      # pilih sumber tertentu
    python framextv.py <url> --pick          # pilih sumber secara interaktif
    python framextv.py <url> --video-only    # video sahaja
    python framextv.py <url> --subs-only --subs all   # subtitle sahaja

Kebergantungan: Python 3.9+, aiohttp, ffmpeg (bin/ffmpeg atau dalam PATH).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

import aiohttp

# ── Konfigurasi ─────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
API = "https://api.framextv.tech"
STREAM_ENDPOINT = f"{API}/api/stream"
DETAILS_ENDPOINT = f"{API}/api/details"
ANIME_STREAM_ENDPOINT = f"{API}/anime/api/stream"
ANILIST_ENDPOINT = "https://graphql.anilist.co"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.rivestream.app/",
    "Origin": "https://www.rivestream.app",
}

SEGMENT_CONCURRENCY = 10
SEGMENT_RETRY_LIMIT = 5
SEGMENT_RETRY_BACKOFF = 1.5
SEGMENT_TIMEOUT = 60

MOVIE_PROVIDERS = ["", "bowler", "super_barbarian", "goblin"]
ANIME_PROVIDERS = [
    ("grand_warden", "sub"),
    ("wizard", "sub"),
    ("healer", "sub"),
    ("archer", "sub"),
    ("grand_warden_dub", "dub"),
    ("wizard_dub", "dub"),
]


def find_ffmpeg() -> str:
    local = BASE_DIR / "bin" / "ffmpeg"
    if local.is_file():
        return str(local)
    found = shutil.which("ffmpeg")
    if found:
        return found
    sys.exit("ffmpeg tidak dijumpai. Letak di bin/ffmpeg atau install dalam PATH.")


# ── Parser link ─────────────────────────────────────────────────────────────

@dataclass
class Link:
    media_type: str                 # movie | tv | anime
    media_id: str                   # TMDB ID / AniList ID
    season: Optional[int] = None
    episode: Optional[int] = None


_HOST_RE = re.compile(r"^https?://(?:www\.)?framextv\.(?:xyz|tech)")
_PATH_PATTERNS = [
    (re.compile(r"/(?:watch|embed)/anime/(\d+)/(\d+)"), "anime"),
    (re.compile(r"/(?:watch|embed|anime)/anime/(\d+)$"), "anime"),
    (re.compile(r"/anime/(\d+)$"), "anime"),
    (re.compile(r"/(?:watch|embed|tv)/(?:tv/)?(\d+)/(\d+)/(\d+)"), "tv"),
    (re.compile(r"/(?:watch|embed|tv)/(?:tv/)?(\d+)$"), "tv_or_movie"),
    (re.compile(r"/(?:watch|embed)/movie/(\d+)"), "movie"),
    (re.compile(r"/movie/(\d+)"), "movie"),
    (re.compile(r"/(?:watch|embed)/(\d+)$"), "movie"),
]


def parse_link(raw: str) -> Link:
    raw = raw.strip()
    if not raw:
        raise ValueError("Input kosong.")
    if re.fullmatch(r"\d+", raw):
        return Link("movie", raw)
    if not _HOST_RE.match(raw):
        raise ValueError("URL tidak dikenali. Guna link framextv.xyz atau TMDB ID terus.")

    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    qs = parse_qs(parsed.query)
    qs_ep = int(qs["episode"][0]) if qs.get("episode", [""])[0].isdigit() else None
    qs_season = int(qs["season"][0]) if qs.get("season", [""])[0].isdigit() else None

    for pattern, kind in _PATH_PATTERNS:
        m = pattern.search(path)
        if not m:
            continue
        groups = m.groups()
        if kind == "anime":
            return Link("anime", groups[0], episode=int(groups[1]) if len(groups) > 1 else qs_ep)
        if kind == "tv":
            return Link(
                "tv", groups[0],
                season=int(groups[1]) if len(groups) > 1 else qs_season,
                episode=int(groups[2]) if len(groups) > 2 else qs_ep,
            )
        if kind == "tv_or_movie":
            if "/tv" in path.lower() or qs_season is not None or qs_ep is not None:
                return Link("tv", groups[0], season=qs_season, episode=qs_ep)
            return Link("movie", groups[0])
        return Link("movie", groups[0])

    raise ValueError(f"Tidak dapat ekstrak ID dari URL: {raw}")


# ── Metadata ────────────────────────────────────────────────────────────────

def clean_html(raw: str) -> str:
    return re.sub(r"<[^>]+>", "", raw or "").replace("&quot;", '"').replace("&#039;", "'").strip()


async def fetch_details(session, media_id: str, media_type: str) -> dict:
    params = {"id": media_id, "type": "tv" if media_type == "tv" else "movie"}
    try:
        async with session.get(DETAILS_ENDPOINT, params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("success"):
                    return data
    except Exception as exc:
        print(f"  ! metadata gagal: {exc}", file=sys.stderr)
    return {}


ANILIST_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    title { english romaji native }
    episodes
    coverImage { large }
    description
    seasonYear
    startDate { year }
    averageScore
    genres
    format
  }
}
"""


async def fetch_anilist(session, anime_id: str) -> dict:
    try:
        payload = {"query": ANILIST_QUERY, "variables": {"id": int(anime_id)}}
        async with session.post(
            ANILIST_ENDPOINT, json=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            body = await r.json()
        media = body.get("data", {}).get("Media")
        if not media:
            return {}
        t = media.get("title", {})
        return {
            "title": t.get("english") or t.get("romaji") or t.get("native") or f"Anime {anime_id}",
            "year": str(media.get("seasonYear") or media.get("startDate", {}).get("year") or ""),
            "poster": media.get("coverImage", {}).get("large", ""),
            "overview": clean_html(media.get("description", "")),
            "rating": f"{media['averageScore'] / 10:.1f}" if media.get("averageScore") else "",
            "genres": media.get("genres") or [],
            "total_episodes": media.get("episodes") or 1,
            "format": media.get("format") or "TV",
        }
    except Exception:
        return {}


# ── Sumber stream ───────────────────────────────────────────────────────────

@dataclass
class Source:
    url: str
    quality: str
    stream_type: str                # hls | mp4
    server: str
    provider: str = ""
    audio: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def height(self) -> int:
        m = re.search(r"(\d{3,4})p", self.quality or "")
        return int(m.group(1)) if m else 0

    @property
    def label(self) -> str:
        audio = f" ({self.audio.upper()})" if self.audio else ""
        return f"{self.quality}{audio} — {self.server} [{self.stream_type}]"


@dataclass
class Subtitle:
    url: str
    label: str
    language: str
    fmt: str = "vtt"


def _absolutize(url: str) -> str:
    return f"{API}{url}" if url.startswith("/") else url


async def _query_movie_provider(session, params: dict, provider: str, sem):
    async with sem:
        p = dict(params)
        if provider:
            p["provider"] = provider
        try:
            async with session.get(STREAM_ENDPOINT, params=p,
                                   timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    d = await r.json()
                    if d.get("success") and d.get("sources"):
                        return d
        except Exception:
            pass
    return None


async def _query_anime_provider(session, anime_id, episode, provider, sub_type, sem):
    async with sem:
        url = (f"{ANIME_STREAM_ENDPOINT}?id={anime_id}&type=anime"
               f"&sub_type={sub_type}&season=1&episode={episode}")
        if provider:
            url += f"&provider={provider}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    d = await r.json()
                    if d.get("success") and d.get("sources"):
                        return d
        except Exception:
            pass
    return None


async def fetch_sources(session, link: Link) -> tuple[list[Source], list[Subtitle]]:
    sources: list[Source] = []
    subtitles: list[Subtitle] = []
    seen: set[str] = set()

    if link.media_type == "anime":
        sem = asyncio.Semaphore(3)
        targets = [("", "sub")] + ANIME_PROVIDERS
        results = await asyncio.gather(
            *[_query_anime_provider(session, link.media_id, link.episode or 1, p, st, sem)
              for p, st in targets],
            return_exceptions=True,
        )
        for r in results:
            if not isinstance(r, dict):
                continue
            server = r.get("server") or r.get("provider", "Anime")
            audio = r.get("audio", "sub")
            top_headers = r.get("headers", {})
            for s in r.get("sources", []):
                url = _absolutize(s.get("url", ""))
                if not url or url in seen:
                    continue
                seen.add(url)
                headers = dict(top_headers)
                headers.update(s.get("headers") or {})
                sources.append(Source(
                    url=url,
                    quality=s.get("quality", "1080p"),
                    stream_type=s.get("type", "hls"),
                    server=s.get("server") or server,
                    provider=r.get("provider", ""),
                    audio=s.get("audio") or audio,
                    headers=headers,
                ))
                for sub in s.get("subtitles", []):
                    surl = _absolutize(sub.get("url", ""))
                    if surl and surl not in seen:
                        seen.add(surl)
                        lang = sub.get("lang") or sub.get("label") or "sub"
                        subtitles.append(Subtitle(surl, lang, lang))
            for sub in r.get("subtitles", []):
                surl = _absolutize(sub.get("url", ""))
                if surl and surl not in seen:
                    seen.add(surl)
                    lang = sub.get("lang") or sub.get("label") or "sub"
                    subtitles.append(Subtitle(surl, lang, lang))
        return sources, subtitles

    params = {"id": link.media_id, "type": link.media_type}
    if link.media_type == "tv":
        params["season"] = str(link.season or 1)
        params["episode"] = str(link.episode or 1)

    sem = asyncio.Semaphore(2)
    results = await asyncio.gather(
        *[_query_movie_provider(session, params, p, sem) for p in MOVIE_PROVIDERS],
        return_exceptions=True,
    )
    for r in results:
        if not isinstance(r, dict):
            continue
        prov = r.get("provider", "default")
        for s in r.get("sources", []):
            url = _absolutize(s.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(Source(
                url=url,
                quality=s.get("quality", "unknown"),
                stream_type=s.get("type", "hls"),
                server=s.get("server", prov),
                provider=prov,
                headers=s.get("headers") or {},
            ))
        for sub in r.get("subtitles", []):
            surl = _absolutize(sub.get("url", ""))
            if not surl or surl in seen:
                continue
            seen.add(surl)
            lang = sub.get("language") or sub.get("lang") or sub.get("label") or "unknown"
            subtitles.append(Subtitle(
                surl,
                sub.get("label", lang),
                lang,
                sub.get("format", "vtt"),
            ))
    return sources, subtitles


# ── Utiliti ─────────────────────────────────────────────────────────────────

def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip(". ")[:200] or "video"


def unique_path(directory: Path, base: str, ext: str = "mp4") -> Path:
    path = directory / f"{base}.{ext}"
    i = 1
    while path.exists():
        path = directory / f"{base}_{i}.{ext}"
        i += 1
    return path


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_eta(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


_last_draw = 0.0


def draw_progress(percent, size, speed, eta, force=False):
    global _last_draw
    now = time.monotonic()
    if not force and now - _last_draw < 0.2:
        return
    _last_draw = now
    line = (f"\r  {percent:5.1f}%  {fmt_size(size):>9}  "
            f"{fmt_size(speed)}/s  ETA {fmt_eta(eta)}   ")
    sys.stderr.write(line)
    sys.stderr.flush()


def build_headers(source: Source) -> dict:
    headers = dict(DEFAULT_HEADERS)
    if "framextv.tech" in source.url:
        headers["Referer"] = "https://vidlink.pro/"
        headers["Origin"] = "https://vidlink.pro"
    headers.update(source.headers or {})
    return headers


def parse_attrs(line: str) -> dict:
    return {
        m.group(1): m.group(2).strip('"')
        for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line)
    }


async def resolve_playlist(session, url: str) -> tuple[str, str]:
    """Turunkan master playlist ke media playlist (pilih variant terbaik)."""
    current, text = url, ""
    for _ in range(3):
        async with session.get(current, timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()
            text = await r.text()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        variants = []
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                attrs = parse_attrs(line)
                res = attrs.get("RESOLUTION", "")
                height = int(res.split("x")[1]) if "x" in res else 0
                bandwidth = int(attrs.get("BANDWIDTH", 0) or 0)
                uri = next((l for l in lines[i + 1:] if not l.startswith("#")), None)
                if uri:
                    variants.append((height, bandwidth, uri))
        if not variants:
            return current, text
        _, _, uri = max(variants, key=lambda v: (v[0], v[1]))
        current = urljoin(current, uri)
    return current, text


def m3u8_parts(text: str, playlist_url: str,
               max_duration: Optional[float] = None) -> tuple[Optional[str], list[str]]:
    init = None
    m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)
    if m:
        init = urljoin(playlist_url, m.group(1))

    segments: list[str] = []
    elapsed = 0.0
    pending = 0.0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pending = 0.0
            continue
        if line.startswith("#"):
            continue
        if max_duration is not None and segments and elapsed >= max_duration:
            break
        segments.append(urljoin(playlist_url, line))
        elapsed += pending
        pending = 0.0
    return init, segments


def vtt_to_srt(vtt: str) -> str:
    lines = vtt.split("\n")
    out, counter, i = [], 0, 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith(("WEBVTT", "NOTE", "STYLE")):
            while i < len(lines) and lines[i].strip():
                i += 1
            i += 1
            continue
        break
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            counter += 1
            ts = re.sub(r"\s+(?:position|align|line|size):[^\s]+", "", line.replace(".", ","))
            out.extend([str(counter), ts])
            i += 1
            while i < len(lines) and lines[i].strip():
                out.append(lines[i].rstrip())
                i += 1
            out.append("")
        else:
            i += 1
    return "\n".join(out)


# ── Downloader ──────────────────────────────────────────────────────────────

def _is_encrypted_playlist(text: str) -> bool:
    return bool(re.search(r"#EXT-X-KEY:[^\n]*METHOD=(?!NONE)", text))


async def download_hls(source: Source, output: Path, ffmpeg: str,
                       on_progress=None, verbose: bool = True,
                       max_duration: Optional[float] = None) -> None:
    headers = build_headers(source)
    async with aiohttp.ClientSession(headers=headers) as session:
        media_url, text = await resolve_playlist(session, source.url)

        if _is_encrypted_playlist(text):
            raise RuntimeError(
                "Playlist HLS disulitkan (EXT-X-KEY) — penyulitan tidak disokong."
            )

        init_url, segment_urls = m3u8_parts(text, media_url, max_duration)
        if not segment_urls:
            raise RuntimeError("Playlist kosong — tiada segmen video.")

        is_fmp4 = init_url is not None or any(".m4s" in u for u in segment_urls)
        ext = "m4s" if is_fmp4 else "ts"
        total = len(segment_urls)
        if verbose:
            print(f"  {total} segmen, {SEGMENT_CONCURRENCY} selari")

        tmp = Path(tempfile.mkdtemp(prefix="framextv_"))
        sem = asyncio.Semaphore(SEGMENT_CONCURRENCY)
        lock = asyncio.Lock()
        state = {"done": 0, "bytes": 0, "start": time.monotonic()}

        async def fetch(url: str, dest: Path) -> int:
            async with sem:
                for attempt in range(1, SEGMENT_RETRY_LIMIT + 1):
                    try:
                        timeout = aiohttp.ClientTimeout(total=SEGMENT_TIMEOUT)
                        async with session.get(url, timeout=timeout) as r:
                            r.raise_for_status()
                            data = await r.read()
                        dest.write_bytes(data)
                        return len(data)
                    except Exception as exc:
                        if attempt == SEGMENT_RETRY_LIMIT:
                            raise RuntimeError(
                                f"Segmen gagal: {url[:90]} — {exc}"
                            ) from exc
                        await asyncio.sleep(SEGMENT_RETRY_BACKOFF ** attempt)
            return 0

        async def worker(idx: int, url: str):
            size = await fetch(url, tmp / f"seg_{idx:06d}.{ext}")
            async with lock:
                state["done"] += 1
                state["bytes"] += size
                elapsed = time.monotonic() - state["start"]
                speed = state["bytes"] / elapsed if elapsed else 0
                remaining = total - state["done"]
                avg = elapsed / state["done"] if state["done"] else 0
                percent = state["done"] / total * 100
                if on_progress:
                    on_progress({
                        "status": "downloading",
                        "percent": percent,
                        "bytes": state["bytes"],
                        "speed": speed,
                        "eta": remaining * avg,
                        "segments_done": state["done"],
                        "segments_total": total,
                    })
                else:
                    draw_progress(percent, state["bytes"], speed, remaining * avg)

        try:
            if init_url:
                init_path = tmp / "init.mp4"
                await fetch(init_url, init_path)
            await asyncio.gather(*(worker(i, u) for i, u in enumerate(segment_urls)))
            if not on_progress:
                draw_progress(100, state["bytes"], state["bytes"] / max(time.monotonic() - state["start"], 0.1), 0, force=True)
                sys.stderr.write("\n")
            else:
                on_progress({
                    "status": "merging",
                    "percent": 100,
                    "bytes": state["bytes"],
                    "speed": 0,
                    "eta": 0,
                    "segments_done": total,
                    "segments_total": total,
                })

            concat = tmp / "concat.txt"
            entries = []
            if init_url:
                entries.append(tmp / "init.mp4")
            entries.extend(tmp / f"seg_{i:06d}.{ext}" for i in range(total))
            concat.write_text(
                "\n".join(f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
                          for p in entries if p.exists()),
                encoding="utf-8",
            )

            if verbose:
                print("  Remux ke MP4...")
            cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy"]
            if not is_fmp4:
                cmd += ["-bsf:a", "aac_adtstoasc"]
            if max_duration is not None:
                cmd += ["-t", f"{max_duration:.3f}"]
            cmd += ["-movflags", "+faststart", str(output)]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                output.unlink(missing_ok=True)
                raise RuntimeError(
                    f"ffmpeg gagal: {err.decode(errors='replace')[-500:]}"
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def download_mp4(source: Source, output: Path, on_progress=None) -> None:
    headers = build_headers(source)
    part = output.with_suffix(output.suffix + ".part")
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
    chunk_size = 256 * 1024
    total_bytes = 0
    start = time.monotonic()

    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(source.url) as r:
                r.raise_for_status()
                length = int(r.headers.get("Content-Length", 0))
                with part.open("wb") as f:
                    async for chunk in r.content.iter_chunked(chunk_size):
                        f.write(chunk)
                        total_bytes += len(chunk)
                        elapsed = time.monotonic() - start
                        speed = total_bytes / elapsed if elapsed else 0
                        percent = min(100.0, total_bytes / length * 100) if length else 0
                        eta = max(0.0, (length - total_bytes) / speed) if length and speed else 0
                        if on_progress:
                            on_progress({
                                "status": "downloading",
                                "percent": percent,
                                "bytes": total_bytes,
                                "speed": speed,
                                "eta": eta,
                                "raw_percent": not length,
                            })
                        else:
                            draw_progress(percent, total_bytes, speed, eta)
            if not on_progress:
                draw_progress(100, total_bytes, total_bytes / max(time.monotonic() - start, 0.1), 0, force=True)
                sys.stderr.write("\n")
        part.replace(output)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


async def download_subtitles(session, subs: list[Subtitle], base: Path, directory: Path):
    for sub in subs:
        try:
            async with session.get(sub.url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    continue
                text = await r.text(errors="replace")
            lang = sanitize(sub.language or sub.label or "sub")
            if sub.fmt == "vtt" or text.lstrip().startswith("WEBVTT"):
                text = vtt_to_srt(text)
            (directory / f"{base}.{lang}.srt").write_text(text, encoding="utf-8")
        except Exception as exc:
            print(f"  ! subtitle {sub.language} gagal: {exc}", file=sys.stderr)


# ── Aliran utama ────────────────────────────────────────────────────────────

def pick_source(sources: list[Source], args) -> Source:
    if args.source is not None:
        if not 0 <= args.source < len(sources):
            sys.exit(f"--source mesti antara 0 dan {len(sources) - 1}.")
        return sources[args.source]
    if args.pick:
        try:
            choice = input(f"Pilih sumber [0-{len(sources) - 1}] (Enter = auto): ").strip()
            if choice:
                idx = int(choice)
                if 0 <= idx < len(sources):
                    return sources[idx]
        except (ValueError, EOFError):
            pass
    return max(sources, key=lambda s: (s.height, 1 if s.stream_type == "hls" else 0))


def quality_tag(quality: str) -> str:
    m = re.search(r"\d{3,4}p", quality or "")
    return m.group(0) if m else sanitize(quality or "video")


def default_name(link: Link, details: dict, quality: str) -> str:
    title = details.get("title") or f"{link.media_type.title()} {link.media_id}"
    year = details.get("year", "")
    tag = quality_tag(quality)
    if link.media_type == "tv":
        return sanitize(f"{title} S{(link.season or 1):02d}E{(link.episode or 1):02d} [{tag}]")
    if link.media_type == "anime":
        return sanitize(f"{title} EP{(link.episode or 1):02d} [{tag}]")
    return sanitize(f"{title} ({year}) [{tag}]" if year else f"{title} [{tag}]")


async def run(args) -> None:
    link = parse_link(args.url)
    if args.season is not None:
        link.season = args.season
        if link.media_type == "movie":
            link.media_type = "tv"
    if args.episode is not None:
        link.episode = args.episode
        if link.media_type == "movie":
            link.media_type = "tv"

    print("  Semak data...")
    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS,
                                     timeout=aiohttp.ClientTimeout(total=30)) as session:
        if link.media_type == "anime":
            details = await fetch_anilist(session, link.media_id)
            total_eps = details.get("total_episodes", 1)
            if link.episode is None and total_eps > 1 and details.get("format") != "MOVIE":
                print(f"  Anime ini ada {total_eps} episode. Guna --episode N.")
                sys.exit(2)
            link.episode = link.episode or 1
        else:
            details = await fetch_details(session, link.media_id, link.media_type)
            if link.media_type == "tv":
                link.season = link.season or 1
                link.episode = link.episode or 1

        title = details.get("title") or f"{link.media_type} {link.media_id}"
        year = f" ({details['year']})" if details.get("year") else ""
        print(f"  {title}{year}")

        print("  Cari sumber stream...")
        sources, subtitles = await fetch_sources(session, link)

        if not sources and not (args.subs_only and subtitles):
            print("  Tiada sumber aktif dijumpai.", file=sys.stderr)
            sys.exit(3)

        sources.sort(key=lambda s: s.height, reverse=True)
        if sources:
            print(f"\n  {len(sources)} sumber:")
            for i, s in enumerate(sources):
                print(f"    [{i}] {s.label}")
        if subtitles:
            langs = ", ".join(dict.fromkeys(sub.language for sub in subtitles))
            print(f"  Subtitles: {langs}")

        if args.list:
            return

        out_dir = Path(args.out).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        base_name = sanitize(args.name) if args.name else media_base(link, details)

        # ── Mod subtitle sahaja ─────────────────────────────────────────────
        if args.subs_only:
            if not subtitles:
                print("  Tiada subtitle dijumpai.", file=sys.stderr)
                sys.exit(3)
            spec = args.subs if args.subs != "none" else "all"
            selected = select_subtitles(subtitles, spec)
            if not selected:
                print(f"  Tiada subtitle padan dengan '{spec}'.", file=sys.stderr)
                sys.exit(3)
            print(f"  Muat turun {len(selected)} subtitle...")
            await download_subtitles(session, selected, base_name, out_dir)
            print(f"  Siap di: {out_dir}\n")
            return

        source = pick_source(sources, args)
        print(f"\n  Pilih: {source.label}")
        print(f"  URL: {source.url[:120]}{'…' if len(source.url) > 120 else ''}")

        filename = sanitize(args.name) if args.name else default_name(link, details, source.quality)
        output = unique_path(out_dir, filename, "mp4")

        ffmpeg = find_ffmpeg()
        start = time.monotonic()
        is_mp4 = source.stream_type.lower() == "mp4" or \
            source.url.split("?")[0].endswith(".mp4")
        try:
            if is_mp4:
                await download_mp4(source, output)
            else:
                await download_hls(source, output, ffmpeg)
        except KeyboardInterrupt:
            print("\n  Dibatalkan.", file=sys.stderr)
            output.unlink(missing_ok=True)
            sys.exit(130)
        except Exception as exc:
            print(f"\n  Gagal: {exc}", file=sys.stderr)
            sys.exit(4)

        elapsed = time.monotonic() - start
        size = output.stat().st_size
        print(f"  Siap: {output}")
        print(f"  {fmt_size(size)} dalam {fmt_eta(elapsed)}\n")

        if args.video_only:
            return

        selected = select_subtitles(subtitles, args.subs)
        if selected:
            print(f"  Muat turun {len(selected)} subtitle...")
            await download_subtitles(session, selected, output.stem, out_dir)


def media_base(link: Link, details: dict) -> str:
    """Nama asas tanpa tag kualiti — untuk video & subtitle kekal sepadan."""
    title = details.get("title") or f"{link.media_type.title()} {link.media_id}"
    if link.media_type == "tv":
        return sanitize(f"{title} S{(link.season or 1):02d}E{(link.episode or 1):02d}")
    if link.media_type == "anime":
        return sanitize(f"{title} EP{(link.episode or 1):02d}")
    year = details.get("year", "")
    return sanitize(f"{title} ({year})" if year else title)


LANGUAGE_ALIASES = {
    "en": {"english", "eng"},
    "ms": {"malay", "malaysian", "melayu", "msa", "may"},
    "id": {"indonesian", "indonesia", "ind"},
    "zh": {"chinese", "mandarin", "zho", "chi", "chs", "cht"},
    "ja": {"japanese", "jpn", "jp"},
    "ko": {"korean", "kor"},
    "es": {"spanish", "spa"},
    "fr": {"french", "fra", "fre"},
    "de": {"german", "germany", "deu", "ger"},
    "ar": {"arabic", "ara"},
    "hi": {"hindi", "hin"},
    "ta": {"tamil", "tam"},
    "te": {"telugu", "tel"},
    "th": {"thai", "tha"},
    "vi": {"vietnamese", "vie"},
    "pt": {"portuguese", "por"},
    "ru": {"russian", "rus"},
    "tr": {"turkish", "tur"},
    "bn": {"bengali", "ben"},
    "ur": {"urdu", "urd"},
}


def canonical_language(value: str) -> str:
    value = (value or "").strip().lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", value) if t]
    if not tokens:
        return ""
    if value in LANGUAGE_ALIASES:
        return value
    for code, aliases in LANGUAGE_ALIASES.items():
        if any(token in aliases for token in tokens):
            return code
    return tokens[0]


def language_matches(wanted: str, language: str) -> bool:
    wanted = (wanted or "").strip().lower()
    language = (language or "").strip().lower()
    if not wanted or not language:
        return False
    tokens = [t for t in re.split(r"[^a-z0-9]+", language) if t]
    if wanted == language or wanted in tokens:
        return True
    if len(wanted) <= 3:
        aliases = LANGUAGE_ALIASES.get(wanted)
        if aliases and any(a in tokens or a == language for a in aliases):
            return True
        return False
    return wanted in language


def select_subtitles(subtitles: list[Subtitle], spec: str) -> list[Subtitle]:
    if not spec or spec == "none":
        return []
    if spec == "all":
        return subtitles
    wanted = [w.strip().lower() for w in spec.split(",") if w.strip()]
    return [
        sub for sub in subtitles
        if any(language_matches(word, sub.language) for word in wanted)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FrameX downloader — movie / TV / anime ke MP4.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""contoh:
  %(prog)s "https://framextv.xyz/watch/550"
  %(prog)s 1399 --season 1 --episode 3
  %(prog)s "https://framextv.xyz/anime/182255" --episode 2 --subs all
  %(prog)s 550 --list
  %(prog)s 550 --video-only
  %(prog)s 1399 --season 1 --episode 3 --subs-only --subs en,ms
  %(prog)s 550 --source 2 --name "Fight Club" --out ~/Videos""",
    )
    parser.add_argument("url", help="Link FrameX atau TMDB/AniList ID")
    parser.add_argument("--season", type=int, help="Nombor season (TV)")
    parser.add_argument("--episode", type=int, help="Nombor episode")
    parser.add_argument("--source", type=int, help="Indeks sumber (lihat --list)")
    parser.add_argument("--pick", action="store_true", help="Pilih sumber secara interaktif")
    parser.add_argument("--list", action="store_true", help="Senarai sumber sahaja")
    parser.add_argument("--subs", default="none", help="none | all | en,ms,id")
    parser.add_argument("--video-only", action="store_true",
                        help="Muat video sahaja, tanpa subtitle")
    parser.add_argument("--subs-only", action="store_true",
                        help="Muat subtitle sahaja, tanpa video")
    parser.add_argument("--name", default="", help="Nama fail output")
    parser.add_argument("--out", default="downloads", help="Folder output (default: downloads)")
    args = parser.parse_args()
    if args.video_only and args.subs_only:
        parser.error("--video-only dan --subs-only tidak boleh digunakan serentak.")

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  Berhenti.", file=sys.stderr)
        sys.exit(130)
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
