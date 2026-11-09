#!/usr/bin/env python3
"""UI web ringkas untuk framextv.py — tampal link, tekan butang, siap.

Jalan:
    .venv/bin/python web.py
Kemudian buka http://127.0.0.1:8899

Muat turun serentak:
    FTV_JOBS=4 .venv/bin/python web.py    # lalai: 3
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

import framextv as ftv

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "downloads"
OUT_DIR.mkdir(exist_ok=True)

TASKS: dict[str, dict] = {}
ORDER: list[str] = []
QUEUE: asyncio.Queue | None = None


def jobs_from_env() -> int:
    try:
        value = int(os.getenv("FTV_JOBS", "3"))
    except ValueError:
        value = 3
    return max(1, min(16, value))


JOBS = jobs_from_env()


def new_task(url: str) -> dict:
    tid = uuid.uuid4().hex[:10]
    TASKS[tid] = {
        "id": tid,
        "url": url,
        "status": "queued",
        "message": "Dalam baris…",
        "position": 0,
        "percent": 0.0,
        "bytes": 0.0,
        "speed": 0.0,
        "eta": 0.0,
        "filename": "",
        "file": "",
        "subs": [],
        "error": "",
        "created": time.time(),
    }
    ORDER.append(tid)
    return TASKS[tid]


def set_status(task: dict, status: str, message: str = "", **extra):
    task["status"] = status
    if message:
        task["message"] = message
    task.update(extra)


TERMINAL_STATUSES = {"done", "error", "cancelled"}
MAX_TASKS = 200


def trim_tasks() -> None:
    """Forget old finished tasks so the registry cannot grow forever."""
    if len(ORDER) <= MAX_TASKS:
        return
    for tid in list(ORDER):
        if len(ORDER) <= MAX_TASKS:
            break
        if TASKS.get(tid, {}).get("status") in TERMINAL_STATUSES:
            ORDER.remove(tid)
            TASKS.pop(tid, None)


def progress_cb(task: dict):
    def _cb(info: dict):
        task["status"] = info.get("status", task["status"])
        if task["status"] == "downloading":
            task["message"] = "Memuat turun…"
        elif task["status"] == "merging":
            task["message"] = "Menggabung ke MP4…"
        task["percent"] = info.get("percent", task["percent"])
        task["bytes"] = info.get("bytes", task["bytes"])
        task["speed"] = info.get("speed", 0)
        task["eta"] = info.get("eta", 0)
        if info.get("raw_percent"):
            task["indeterminate"] = True
    return _cb


def dedupe_subs(subs: list) -> list:
    seen: set[str] = set()
    out = []
    for s in subs:
        key = (s.language or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def apply_overrides(link, season, episode):
    if season not in (None, ""):
        link.season = int(season)
        if link.media_type == "movie":
            link.media_type = "tv"
    if episode not in (None, ""):
        link.episode = int(episode)
        if link.media_type == "movie":
            link.media_type = "tv"
    return link


async def gather(session, link):
    """Metadata + sumber, dengan default season/episode yang konsisten."""
    if link.media_type == "anime":
        details = await ftv.fetch_anilist(session, link.media_id)
        link.episode = link.episode or 1
    else:
        details = await ftv.fetch_details(session, link.media_id, link.media_type)
        if link.media_type == "tv":
            link.season = link.season or 1
            link.episode = link.episode or 1
    sources, subtitles = await ftv.fetch_sources(session, link)
    sources.sort(key=lambda s: s.height, reverse=True)
    return details, sources, dedupe_subs(subtitles)


async def run_task(task: dict, url: str, season, episode,
                   source_index=None, subtitle_indices=None):
    try:
        set_status(task, "resolving", "Menyemak link & metadata…")
        link = apply_overrides(ftv.parse_link(url), season, episode)

        async with aiohttp.ClientSession(
            headers=ftv.DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            details, sources, subtitles = await gather(session, link)
            title = details.get("title") or f"{link.media_type} {link.media_id}"
            task["title"] = title

            if not sources:
                set_status(task, "error", "Tiada sumber video aktif.", error="Tiada sumber video aktif.")
                return

            if source_index is not None and 0 <= int(source_index) < len(sources):
                source = sources[int(source_index)]
            else:
                source = max(sources, key=lambda s: (s.height, 1 if s.stream_type == "hls" else 0))
            task["quality"] = source.quality
            task["server"] = source.server

            filename = ftv.default_name(link, details, source.quality)
            output = ftv.unique_path(OUT_DIR, filename, "mp4")
            task["filename"] = output.name
            task["status"] = "downloading"
            task["message"] = "Memuat turun…"

            is_mp4 = source.stream_type.lower() == "mp4" or \
                source.url.split("?")[0].endswith(".mp4")
            if is_mp4:
                await ftv.download_mp4(source, output, on_progress=progress_cb(task))
            else:
                await ftv.download_hls(source, output, ftv.find_ffmpeg(),
                                       on_progress=progress_cb(task))

            selected = []
            for idx in (subtitle_indices or []):
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(subtitles):
                    selected.append(subtitles[idx])

            if selected:
                set_status(task, "subtitles", "Memuat subtitle…")
                await ftv.download_subtitles(session, selected, output.stem, OUT_DIR)
                task["subs"] = [
                    s.language for s in selected
                    if (OUT_DIR / f"{output.stem}.{ftv.sanitize(s.language)}.srt").exists()
                ]

        set_status(task, "done", "Selesai", percent=100.0, speed=0, eta=0,
                   file=output.name, bytes=output.stat().st_size)
    except Exception as exc:
        set_status(task, "error", f"Gagal: {exc}", error=str(exc))


async def queue_worker(queue: asyncio.Queue):
    """Proses download dari baris gilir; beberapa worker berjalan serentak."""
    while True:
        task, args = await queue.get()
        try:
            task["message"] = "Bermula…"
            await run_task(task, *args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            set_status(task, "error", f"Gagal: {exc}", error=str(exc))
        finally:
            trim_tasks()
            queue.task_done()


async def on_startup(app: web.Application):
    global QUEUE
    QUEUE = asyncio.Queue()
    app["queue"] = QUEUE
    app["workers"] = [
        asyncio.create_task(queue_worker(QUEUE)) for _ in range(JOBS)
    ]


async def on_cleanup(app: web.Application):
    for worker in app.get("workers", []):
        worker.cancel()


async def index(request: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html")


async def api_inspect(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="JSON tidak sah")
    url = (data.get("url") or "").strip()
    if not url:
        raise web.HTTPBadRequest(text="Link kosong")
    try:
        link = apply_overrides(ftv.parse_link(url), data.get("season"), data.get("episode"))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))

    try:
        async with aiohttp.ClientSession(
            headers=ftv.DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            details, sources, subtitles = await gather(session, link)
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": f"Gagal semak link: {exc}"}, status=502
        )

    if not sources and not subtitles:
        return web.json_response({"ok": False, "error": "Tiada sumber stream ditemui."})

    return web.json_response({
        "ok": True,
        "title": details.get("title") or f"{link.media_type} {link.media_id}",
        "year": details.get("year", ""),
        "media_type": link.media_type,
        "season": link.season,
        "episode": link.episode,
        "sources": [
            {
                "index": i,
                "quality": s.quality,
                "height": s.height,
                "server": s.server,
                "type": s.stream_type,
                "audio": s.audio,
            }
            for i, s in enumerate(sources)
        ],
        "subtitles": [
            {"index": i, "language": s.language, "label": s.label}
            for i, s in enumerate(subtitles)
        ],
    })


async def api_download(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="JSON tidak sah")
    url = (data.get("url") or "").strip()
    if not url:
        raise web.HTTPBadRequest(text="Link kosong")
    try:
        ftv.parse_link(url)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))

    source_index = data.get("source_index")
    subtitle_indices = data.get("subtitle_indices") or []
    if not isinstance(subtitle_indices, list):
        subtitle_indices = []

    task = new_task(url)
    await request.app["queue"].put((
        task,
        (url, data.get("season") or None, data.get("episode") or None,
         source_index, subtitle_indices),
    ))
    return web.json_response({"id": task["id"]})


async def api_tasks(request: web.Request) -> web.Response:
    queued = 0
    for tid in ORDER:
        t = TASKS[tid]
        if t["status"] == "queued":
            queued += 1
            t["position"] = queued
        else:
            t["position"] = 0
    tasks = [TASKS[t] for t in reversed(ORDER[-30:])]
    return web.json_response(tasks)


async def serve_file(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    root = OUT_DIR.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}"
    })


HTML = """<!doctype html>
<html lang="ms">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FrameX Downloader</title>
<style>
  :root {
    --bg:#0b0d12; --card:#141821; --card2:#1b202c; --line:#262d3b;
    --text:#e8ecf3; --muted:#8a94a6; --accent:#5b8cff; --ok:#33c48d; --err:#ff5f6d;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; min-height:100vh; background:radial-gradient(1200px 600px at 50% -20%, #1a2440 0%, var(--bg) 60%);
    color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    display:flex; flex-direction:column; align-items:center; padding:48px 20px 80px;
  }
  .wrap { width:100%; max-width:720px; }
  h1 { font-size:26px; margin:0 0 6px; letter-spacing:-.02em; }
  .sub { color:var(--muted); margin:0 0 28px; font-size:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:20px; }
  .row { display:flex; gap:10px; }
  input, select, button { font:inherit; border-radius:10px; border:1px solid var(--line); }
  input[type=text], input[type=number], select {
    background:var(--card2); color:var(--text); padding:13px 14px; width:100%; outline:none;
    transition:border-color .15s, box-shadow .15s;
  }
  input[type=text]:focus, input[type=number]:focus, select:focus {
    border-color:var(--accent); box-shadow:0 0 0 3px rgba(91,140,255,.18);
  }
  button.go {
    background:linear-gradient(180deg,#6b97ff,#4a7df0); color:#fff; border:0; padding:0 26px;
    font-weight:600; cursor:pointer; white-space:nowrap; transition:filter .15s, transform .05s;
  }
  button.go:hover { filter:brightness(1.08); }
  button.go:active { transform:translateY(1px); }
  button.go:disabled { opacity:.55; cursor:not-allowed; filter:none; }
  details { margin-top:14px; }
  summary { color:var(--muted); cursor:pointer; font-size:13px; user-select:none; }
  .adv { display:grid; grid-template-columns:1fr 1fr 1.4fr; gap:10px; margin-top:12px; }
  label { display:block; font-size:12px; color:var(--muted); margin-bottom:5px; }
  .options { display:none; margin-top:18px; padding-top:18px; border-top:1px solid var(--line); }
  .options.show { display:block; }
  .titleline { font-weight:600; font-size:16px; }
  .yearline { color:var(--muted); font-size:13px; margin:2px 0 14px; }
  .opt-title { font-size:11.5px; color:var(--muted); text-transform:uppercase;
    letter-spacing:.07em; margin:14px 0 8px; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; }
  .chip { position:relative; margin:0; }
  .chip input { position:absolute; opacity:0; inset:0; width:100%; height:100%; cursor:pointer; }
  .chip span { display:inline-block; padding:8px 13px; border:1px solid var(--line);
    border-radius:99px; font-size:13px; cursor:pointer; background:var(--card2);
    color:var(--text); transition:background .15s, border-color .15s; }
  .chip input:checked + span { background:rgba(91,140,255,.2); border-color:var(--accent); color:#cfe0ff; }
  .chip input:focus-visible + span { outline:2px solid var(--accent); outline-offset:1px; }
  button.wide { width:100%; padding:14px; margin-top:20px; }
  .muted-small { color:var(--muted); font-size:12.5px; }
  .tasks { margin-top:26px; display:flex; flex-direction:column; gap:12px; }
  .task { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px 16px; }
  .task .top { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
  .task .name { font-weight:600; font-size:14px; overflow-wrap:anywhere; }
  .task .meta { color:var(--muted); font-size:12.5px; }
  .bar { height:7px; background:#222838; border-radius:99px; margin-top:11px; overflow:hidden; }
  .fill { height:100%; width:0%; background:linear-gradient(90deg,#4a7df0,#7aa2ff); transition:width .35s ease; }
  .fill.done { background:linear-gradient(90deg,#23a97a,#33c48d); }
  .fill.err { background:linear-gradient(90deg,#c23a45,#ff5f6d); }
  .task .state { margin-top:8px; font-size:12.5px; color:var(--muted); }
  .task .state.error { color:var(--err); }
  .task .state.done { color:var(--ok); }
  .dl { color:var(--accent); text-decoration:none; font-weight:600; }
  .dl:hover { text-decoration:underline; }
  .subs { color:var(--muted); font-size:12px; margin-left:8px; }
  .empty { color:var(--muted); text-align:center; padding:26px 0; font-size:14px; }
  @media (max-width:600px) {
    .row { flex-direction:column; }
    button.go { padding:13px; }
    .adv { grid-template-columns:1fr; }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>FrameX Downloader</h1>
  <p class="sub">Tampal link sahaja — dia cari sumber, muat turun &amp; gabung jadi MP4.</p>

  <div class="card">
    <div class="row">
      <input id="url" type="text" autofocus
             placeholder="https://framextv.xyz/watch/550  atau  550">
      <button class="go" id="go" onclick="inspectLink()">Semak</button>
    </div>

    <details>
      <summary>Pilihan lanjutan</summary>
      <div class="adv">
        <div><label>Season</label><input id="season" type="number" min="0" placeholder="auto"></div>
        <div><label>Episode</label><input id="episode" type="number" min="0" placeholder="auto"></div>
        <div><label>Subtitle</label><input type="text" value="Pilih selepas semak" disabled></div>
      </div>
    </details>

    <div class="options" id="options">
      <div class="titleline" id="optTitle"></div>
      <div class="yearline" id="optMeta"></div>

      <div class="opt-title">Resolusi</div>
      <div class="chips" id="srcList"></div>

      <div class="opt-title">Subtitle</div>
      <div class="chips" id="subList"></div>

      <button class="go wide" id="go2" onclick="download()">Muat Turun</button>
    </div>
  </div>

  <div class="tasks" id="tasks"><div class="empty" id="empty">Belum ada muat turun.</div></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let busy = false;
let info = null;

function payloadBase() {
  return {
    url: $('url').value.trim(),
    season: $('season').value || null,
    episode: $('episode').value || null
  };
}

async function inspectLink() {
  const p = payloadBase();
  if (!p.url) { $('url').focus(); return; }
  if (busy) return;
  busy = true;
  const b = $('go'); b.disabled = true; b.textContent = 'Menyemak…';
  $('options').classList.remove('show');
  try {
    const res = await fetch('/api/inspect', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(p)
    });
    const d = await res.json();
    if (!res.ok || !d.ok) {
      alert(d.error || (await res.text()) || 'Gagal semak link.');
    } else {
      info = d; renderOptions(d);
    }
  } catch (e) {
    alert('Tidak dapat hubungi server.');
  }
  busy = false; b.disabled = false; b.textContent = 'Semak';
}

function renderOptions(d) {
  const meta = [d.year,
    d.season != null ? 'S' + String(d.season).padStart(2, '0') : '',
    d.episode != null ? 'E' + String(d.episode).padStart(2, '0') : ''
  ].filter(Boolean).join(' · ');
  $('optTitle').textContent = d.title;
  $('optMeta').textContent = meta;

  let src = d.sources.map((s, i) => {
    const audio = (s.audio && s.audio.toLowerCase() !== 'sub') ? ' · ' + s.audio.toUpperCase() : '';
    const label = (s.quality || '?') + audio + ' · ' + (s.server || '');
    return '<label class="chip"><input type="radio" name="src" value="' + s.index + '"' +
      (i === 0 ? ' checked' : '') + '><span>' + esc(label) + '</span></label>';
  }).join('');
  if (!d.sources.length) src = '<span class="muted-small">Tiada sumber.</span>';
  $('srcList').innerHTML = src;

  let subs = d.subtitles.map(s =>
    '<label class="chip"><input type="checkbox" name="sub" value="' + s.index +
    '"><span>' + esc(s.label || s.language) + '</span></label>'
  ).join('');
  if (d.subtitles.length) {
    subs = '<label class="chip"><input type="checkbox" id="allSubs" onchange="toggleAll(this)">' +
      '<span>Semua</span></label>' + subs;
  } else {
    subs = '<span class="muted-small">Tiada subtitle.</span>';
  }
  $('subList').innerHTML = subs;
  $('options').classList.add('show');
}

function toggleAll(cb) {
  document.querySelectorAll('input[name=sub]').forEach(x => { x.checked = cb.checked; });
}

async function download() {
  if (!info) return;
  const srcEl = document.querySelector('input[name=src]:checked');
  const source_index = srcEl ? parseInt(srcEl.value) : null;
  const subtitle_indices = Array.from(document.querySelectorAll('input[name=sub]:checked'))
    .map(x => parseInt(x.value));

  const b = $('go2'); b.disabled = true; b.textContent = 'Menghantar…';
  try {
    const res = await fetch('/api/download', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign(payloadBase(), {source_index, subtitle_indices}))
    });
    if (!res.ok) {
      alert((await res.text()) || 'Ralat');
    } else {
      $('url').value = ''; $('season').value = ''; $('episode').value = '';
      $('options').classList.remove('show'); info = null;
    }
  } catch (e) {
    alert('Tidak dapat hubungi server.');
  }
  b.disabled = false; b.textContent = 'Muat Turun';
  refresh();
}

function fmt(n) {
  if (!n) return '0 B';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}
function eta(s) {
  if (!s || s < 0) return '--:--';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(x).padStart(2,'0');
}
const LABELS = {
  queued:'Dalam baris…', resolving:'Menyemak link…', searching:'Mencari sumber…',
  downloading:'Memuat turun…', merging:'Menggabung ke MP4…', subtitles:'Memuat subtitle…',
  done:'Selesai', error:'Gagal'
};

function render(tasks) {
  const box = $('tasks');
  if (!tasks.length) { box.innerHTML = '<div class="empty">Belum ada muat turun.</div>'; return; }
  box.innerHTML = tasks.map(t => {
    const cls = t.status === 'done' ? 'done' : (t.status === 'error' ? 'err' : '');
    const pct = t.status === 'done' ? 100
      : (t.indeterminate ? 100 : Math.min(100, t.percent || 0));
    const title = t.filename || t.title || t.url;
    let state = '<span class="state ' + (t.status === 'error' ? 'error' : t.status === 'done' ? 'done' : '') + '">';
    const label = (t.status === 'queued' && t.position)
      ? 'Menunggu giliran #' + t.position
      : (LABELS[t.status] || t.status);
    state += esc(t.status === 'error' ? (t.error || t.message) : label);
    if (t.status === 'downloading') {
      state += ' · ' + fmt(t.bytes) + ' · ' + fmt(t.speed) + '/s · ETA ' + eta(t.eta);
    }
    if (t.status === 'done' && t.file) {
      state += ' · <a class="dl" href="/file/' + encodeURIComponent(t.file) + '">Muat turun fail</a>';
      if (t.subs && t.subs.length) state += '<span class="subs">subtitle: ' + esc(t.subs.join(', ')) + '</span>';
    }
    state += '</span>';
    let meta = '';
    if (t.quality) meta = '<div class="meta">' + esc(t.quality) + ' · ' + esc(t.server || '') + '</div>';
    return '<div class="task"><div class="top"><div class="name">' + esc(title) + '</div>' +
           '<div>' + (t.status === 'done' && t.file ? '<a class="dl" href="/file/' + encodeURIComponent(t.file) + '">Buka</a>' : '') + '</div></div>' +
           meta +
           '<div class="bar"><div class="fill ' + cls + '" style="width:' + pct + '%"></div></div>' +
           state + '</div>';
  }).join('');
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

async function refresh() {
  try {
    const r = await fetch('/api/tasks');
    if (r.ok) render(await r.json());
  } catch (e) {}
}

$('url').addEventListener('keydown', e => { if (e.key === 'Enter') inspectLink(); });
setInterval(refresh, 900);
refresh();
</script>
</body>
</html>
"""


def lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/api/inspect", api_inspect)
    app.router.add_post("/api/download", api_download)
    app.router.add_get("/api/tasks", api_tasks)
    app.router.add_get("/file/{name}", serve_file)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    host = os.getenv("FTV_HOST", "0.0.0.0")
    port = int(os.getenv("FTV_PORT", "8899"))

    print("\n  FrameX Downloader UI")
    print(f"  Laptop ini  →  http://127.0.0.1:{port}")
    for ip in lan_ips():
        print(f"  Dari device →  http://{ip}:{port}")
    print(f"  Folder output: {OUT_DIR}")
    print(f"  Serentak     : {JOBS} muat turun  (FTV_JOBS)")
    print("  Henti: Ctrl+C\n")

    try:
        web.run_app(app, host=host, port=port, print=None)
    except OSError as exc:
        print(f"\n  Gagal buka port {port}: {exc}")
        print("  Cuba port lain:  FTV_PORT=8900 ./ui.sh\n")


if __name__ == "__main__":
    main()
