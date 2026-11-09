#!/usr/bin/env sh
# FrameX Downloader — buka UI web ringkas.
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python web.py "$@"
fi
exec python3 web.py "$@"
