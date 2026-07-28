#!/bin/sh
set -eu

display="${DISPLAY:-:99}"
display_number="${display#:}"
socket="/tmp/.X11-unix/X${display_number}"

mkdir -p /tmp/.X11-unix
Xvfb "$display" -screen 0 1365x900x24 -nolisten tcp &
xvfb_pid=$!

cleanup() {
    kill "$xvfb_pid" 2>/dev/null || true
    wait "$xvfb_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

attempt=0
while [ ! -S "$socket" ]; do
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        echo "Xvfb s'est arrete avant de creer $socket" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 50 ]; then
        echo "Xvfb indisponible apres 10 secondes: $socket absent" >&2
        exit 1
    fi
    sleep 0.2
done

export DISPLAY="$display"
python scraper_nautiljon.py "$@"
