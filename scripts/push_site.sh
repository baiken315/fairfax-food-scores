#!/usr/bin/env bash
# Publish the built site (dist/) to the `site` branch of origin as a single fresh commit.
# Netlify deploys that branch. Replacing it each time keeps weekly-changing JSON out of the repo history.
set -euo pipefail
cd "$(dirname "$0")/.."
REMOTE_URL=$(git remote get-url origin)
[[ -f dist/index.html && -f dist/data/meta.json ]] || { echo "dist/ is missing; run: ffx publish --out dist"; exit 1; }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp -a dist/. "$tmp/"
git -C "$tmp" init -q -b site
git -C "$tmp" -c user.name="$(git config user.name)" -c user.email="$(git config user.email)" add -A
git -C "$tmp" -c user.name="$(git config user.name)" -c user.email="$(git config user.email)" \
    commit -q -m "Site build $(date -Is) from $(git rev-parse --short HEAD)"
git -C "$tmp" push -q --force "$REMOTE_URL" site:site
echo "pushed site build ($(du -sh dist | cut -f1)) to $REMOTE_URL branch 'site'"
