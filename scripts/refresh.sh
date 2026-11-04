#!/usr/bin/env bash
# Weekly refresh: mirror the county layer, fetch + parse new reports, refresh complaints, rescore.
# Run from the project root (the systemd unit sets WorkingDirectory). Single-instance via flock.
set -euo pipefail
cd "$(dirname "$0")/.."
FFX=".venv/bin/ffx"
mkdir -p data
exec 9>data/.refresh.lock
flock -n 9 || { echo "another refresh holds the lock; exiting"; exit 0; }
if systemctl --user is-active --quiet ffx-backfill 2>/dev/null; then
    echo "backfill still running; skipping this refresh"; exit 0
fi

echo "== $(date -Is) refresh start"
$FFX ingest --all-statuses
$FFX fetch --oldest-first            # anything still pending, oldest first (they expire first)
$FFX parse
year=$(date +%Y)
$FFX complaints scrape --years "$((year - 1))-$year"   # new records + status updates for the last two years
$FFX complaints match
$FFX score
$FFX compact
$FFX publish --out dist
# Netlify deploys the `site` branch on push. Opt in with FFX_DEPLOY=1 (set in the systemd unit).
# The build is compared with the last deployed one first; a sharp drop blocks the deploy (the site keeps
# serving last week's data) and the job exits non-zero so the failure shows in `systemctl --user status`.
if [[ "${FFX_DEPLOY:-0}" == "1" ]]; then
    if .venv/bin/python scripts/check_build.py dist/data/meta.json data/last_deployed_meta.json; then
        scripts/push_site.sh
        cp dist/data/meta.json data/last_deployed_meta.json
    else
        echo "== $(date -Is) build failed sanity check; NOT deployed"
        $FFX stats
        exit 1
    fi
fi
$FFX stats
$FFX complaints stats
echo "== $(date -Is) refresh done"
