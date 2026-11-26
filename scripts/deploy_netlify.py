#!/usr/bin/env python3
"""Publish dist/ to Netlify ffx-restaurants with a zero-build file deploy.

Usage (repo root):  set -a; source .env; set +a; .venv/bin/python scripts/deploy_netlify.py

Uploads go through the Netlify API, so they never touch the build system and cost no
build minutes — unlike the git-push path, which can queue, skip, or fail behind account
limits. Files are addressed by SHA1, so repeat deploys only ship what changed.

A deploy still costs bandwidth, so the script first compares local digests with the
published deploy and skips entirely when the bundle is byte-identical. --force re-deploys
identical content; --check reports the comparison and never writes.
"""
import base64
import hashlib
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.netlify.com/api/v1"
SITE_NAME = os.environ.get("NETLIFY_SITE_NAME", "ffx-restaurants")

TOKEN = os.environ["NETLIFY_AUTH_TOKEN"]


def req(method, path, body=None, raw=None, content_type="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Authorization": f"Bearer {TOKEN}"})
    if content_type:
        r.add_header("Content-Type", content_type)
    with urllib.request.urlopen(r) as resp:
        out = resp.read()
        return json.loads(out) if out else {}


def collect(root):
    files = {}
    for dirpath, _dirs, names in os.walk(root):
        for n in sorted(names):
            rel = os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/")
            with open(os.path.join(dirpath, n), "rb") as f:
                content = f.read()
            files["/" + rel] = (hashlib.sha1(content).hexdigest(), content)
    return files


def _norm_sha(v):
    """Deploy file digests come back as 40-char hex or SRI 'sha1-<base64>'; None = unknown."""
    if not isinstance(v, str):
        return None
    if v.startswith("sha1-"):
        try:
            return base64.b64decode(v[5:]).hex()
        except Exception:
            return None
    v = v.lower()
    return v if len(v) == 40 and all(c in "0123456789abcdef" for c in v) else None


def published_bundle(site_id):
    """(deploy_id, {lowerpath: sha1hex}) of the live production deploy, or (pid, None) if
    the comparison can't be trusted (missing deploy, fetch failure, unexpected digest
    format) — callers must then fall through to a real deploy rather than skip on bad data.
    Netlify case-folds file paths, hence lowercase keys (caller lowercases the local map)."""
    pd = req("GET", f"/sites/{site_id}").get("published_deploy")
    pid = pd["id"] if isinstance(pd, dict) else pd
    if not pid:
        return None, None
    try:
        fl = req("GET", f"/deploys/{pid}/files")
    except Exception:
        return pid, None
    norm = {}
    for f in fl or []:
        h = _norm_sha(f.get("sha"))
        if h is None or not isinstance(f.get("path"), str):
            return pid, None
        norm[f["path"].lower()] = h
    if not norm or len(norm) != len(fl):
        return pid, None
    return pid, norm


def main():
    force = "--force" in sys.argv
    check = "--check" in sys.argv
    files = collect("dist")
    site_id = os.environ.get("NETLIFY_SITE_ID")
    if not site_id:
        site = req("POST", "/sites", {"name": SITE_NAME})
        site_id = site["id"]
        print(f"created site {site['name']} id={site_id}")
        print(f"add to .env:  NETLIFY_SITE_ID={site_id}")

    digests = {p: h for p, (h, _c) in files.items()}
    lower = {p.lower(): h for p, h in digests.items()}
    comparable = len(lower) == len(digests)   # case-only collisions would poison the compare

    pid, pub = published_bundle(site_id) if comparable else (None, None)
    same = pub is not None and pub == lower
    if check:
        if pub is None:
            print(f"published deploy: not comparable (pid={pid}) — a deploy would run")
        elif same:
            print(f"published deploy {pid}: bundle IDENTICAL — a deploy would be skipped (0 credits)")
        else:
            diff = sorted(set(lower.items()) ^ set(pub.items()))
            print(f"published deploy {pid}: {len(diff)} file(s) differ — a deploy would run")
            for path, h in diff:
                print(f"  {path} local={h[:10]} published={pub.get(path, '<absent>')[:10]}")
        return
    if same and not force:
        print(f"dist/ bundle identical to published deploy {pid} — skipping Netlify (0 credits)")
        return

    dep = req("POST", f"/sites/{site_id}/deploys", {"files": digests})
    # `required` entries are SHA1s (older API versions return paths); map both ways.
    by_hash = {}
    for p, (h, _c) in files.items():
        by_hash.setdefault(h, []).append(p)
    for need in dep.get("required", []):
        paths = [need] if need in files else by_hash.get(need, [])
        for path in paths:
            h, content = files[path]
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            req("PUT", f"/deploys/{dep['id']}/files{path}", raw=content, content_type=ctype)
            print(f"uploaded {path} ({len(content)} B)")

    for _ in range(60):
        state = req("GET", f"/deploys/{dep['id']}")["state"]
        if state == "ready":
            break
        if state == "error":
            sys.exit("deploy entered error state")
        time.sleep(2)
    else:
        sys.exit("timed out waiting for deploy")

    req("POST", f"/sites/{site_id}/deploys/{dep['id']}/restore")
    live = req("GET", f"/sites/{site_id}")
    print(f"production deploy live: https://{live['ssl_url'].split('//', 1)[1] if live.get('ssl_url') else live['default_domain']}")


if __name__ == "__main__":
    main()
