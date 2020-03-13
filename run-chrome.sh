#!/bin/bash
BASE_TEMP_DIR="/tmp"
TEMP_DIR=$(mktemp -d "$BASE_TEMP_DIR/chromium.XXXXXXXX")

echo "Running chromium with temporary profile in: $TEMP_DIR"

# https://peter.sh/experiments/chromium-command-line-switches/
#/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
/Applications/Chromium.app/Contents/MacOS/Chromium \
    --remote-debugging-port=9222 --enable-automation \
    --user-data-dir="$TEMP_DIR" --no-first-run \
    --disk-cache-size=0  \
    --window-size=1400,950 --window-position=0,0 \
    --disable-features=IsolateOrigins,site-per-process # https://stackoverflow.com/questions/53280678/why-arent-network-requests-for-iframes-showing-in-the-chrome-developer-tools-un
    # --headless

rm -rf "$TEMP_DIR"
