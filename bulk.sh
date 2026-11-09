#!/usr/bin/env sh
# FrameX bulk downloader — guna venv projek jika ada.
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python bulk.py "$@"
fi
exec python3 bulk.py "$@"
