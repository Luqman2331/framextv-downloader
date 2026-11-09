# CLI & UI Ringkas

Set tambahan yang berdiri sendiri (tidak bergantung pada `app.py`/`src/`):

| Fail | Fungsi |
|------|--------|
| `framextv.py` | CLI satu link — muat turun filem/TV/anime ke MP4 + subtitle |
| `bulk.py` / `bulk.sh` | Muat turun pukal: banyak URL, satu musim, julat episode, semua episode |
| `web.py` / `ui.sh` | UI web ringkas (aiohttp) di port `8899` |
| `desktop.sh` | Launcher UI ringkas untuk ikon desktop (auto-buka browser) |

## Keperluan

- Python 3.9+
- `aiohttp` (sudah ditambah dalam `requirements.txt`)
- FFmpeg — guna `bin/ffmpeg` (auto-download oleh `run.sh`) atau yang ada dalam `PATH`

## Penggunaan

```bash
# CLI satu link
.venv/bin/python framextv.py "https://framextv.xyz/watch/550"
.venv/bin/python framextv.py 1399 --season 1 --episode 3 --subs en,ms
.venv/bin/python framextv.py "https://framextv.xyz/anime/182255" --episode 2
.venv/bin/python framextv.py 550 --list              # senarai sumber sahaja
.venv/bin/python framextv.py 550 --subs-only --subs all

# Pukal
./bulk.sh "https://framextv.xyz/tv/1399" --season 1
./bulk.sh "https://framextv.xyz/tv/1399" --season 1 --episodes 1-5,8
./bulk.sh "https://framextv.xyz/anime/182255" --all-episodes --jobs 2
./bulk.sh -f senarai.txt --subs en,ms,id
./bulk.sh 550 --dry-run
./bulk.sh 550 --list-sources
./bulk.sh 550 --duration 1m

# UI ringkas
./ui.sh                                   # http://127.0.0.1:8899
FTV_JOBS=4 ./ui.sh                        # 4 muat turun serentak
FTV_PORT=8900 FTV_HOST=127.0.0.1 ./ui.sh
./desktop.sh                              # sama, auto-buka browser
```

## Nota

- Output lalai ke `downloads/` (`--out DIR` untuk tukar).
- Nama fail TV: `<Tajuk> S01E03 [1080p].mp4`; anime: `<Tajuk> EP03 [1080p].mp4`.
- HLS master playlist memilih variant resolusi/bandwidth tertinggi.
- Segmen HLS digabung tanpa re-encode (`-c copy`) dan di-remux ke MP4.
- Subtitle VTT ditukar ke SRT; nama subtitle mengikut nama video.
- Muat turun MP4 guna fail sementara `*.mp4.part` — tidak muncul sebagai fail siap sehingga selesai.
- `bulk.py` melangkau fail yang sudah ada (termasuk variasi kualiti); guna `--overwrite` untuk muat semula.
- Fail yang sudah ada dilangkau secara automatik, jadi arahan sama boleh dijalankan semula untuk sambung.
