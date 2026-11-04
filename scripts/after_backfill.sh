#!/usr/bin/env bash
# One-off: wait for the initial report backfill, then do the first complete pass of everything else.
set -euo pipefail
cd "$(dirname "$0")/.."
FFX=".venv/bin/ffx"
while systemctl --user is-active --quiet ffx-backfill; do sleep 300; done
exec 9>data/.refresh.lock
flock 9                                   # wait for any running weekly refresh
echo "== $(date -Is) after-backfill start"
$FFX fetch --oldest-first                 # stragglers
$FFX parse
$FFX compact --vacuum
$FFX complaints scrape --years 2023-$(date +%Y)
$FFX complaints match
$FFX score
$FFX stats
$FFX complaints stats
$FFX calibrate --out "data/calibration-$(date +%F).md"
echo "== $(date -Is) after-backfill done"
