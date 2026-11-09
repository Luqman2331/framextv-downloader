#!/usr/bin/env sh
# FrameX Downloader — buka UI web dari ikon desktop/menu.
cd "$(dirname "$0")" || exit 1

PORT="${FTV_PORT:-8899}"
URL="http://127.0.0.1:${PORT}/"
LOG="${TMPDIR:-/tmp}/framextv-web.log"

if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY=python3
fi

open_browser() {
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$1" >/dev/null 2>&1 &
    elif command -v sensible-browser >/dev/null 2>&1; then
        sensible-browser "$1" >/dev/null 2>&1 &
    fi
}

# Server dah hidup? Terus buka browser sahaja.
if curl -fsS -o /dev/null --max-time 1 "$URL"; then
    open_browser "$URL"
    exit 0
fi

"$PY" web.py >"$LOG" 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null' EXIT INT TERM

# Tunggu server betul-betul sedia (maksimum ~10s).
i=0
while [ "$i" -lt 50 ]; do
    if curl -fsS -o /dev/null --max-time 1 "$URL"; then
        open_browser "$URL"
        wait "$PID"
        exit 0
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "FrameX UI gagal dimulakan. Log: $LOG" >&2
        exit 1
    fi
    i=$((i + 1))
    sleep 0.2
done

echo "Server tidak sedia selepas 10s. Log: $LOG" >&2
exit 1
