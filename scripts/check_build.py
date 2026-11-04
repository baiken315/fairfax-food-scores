"""Sanity-check a fresh build (dist/) against the last deployed one before an unattended deploy.

Exits non-zero, so nothing is pushed, when the new build looks broken: e.g. the county changed its report
layout (parsing collapses) or a source was down (counts drop). Thresholds are relative to the last deploy.
Usage: check_build.py <new meta.json> <last deployed meta.json>
"""
import json
import sys
from pathlib import Path

MIN_RATIO = {                         # new / previous must be at least this
    "locations": 0.90,
    "reports_parsed": 0.90,
    "graded": 0.85,
}


def graded(meta: dict) -> int:
    return sum(v for k, v in meta["letter_counts"].items() if k != "none")


def main(new_path: str, prev_path: str) -> int:
    new = json.loads(Path(new_path).read_text())
    prev_file = Path(prev_path)
    if not prev_file.exists():
        print("check_build: no previous deploy recorded; accepting this build")
        return 0
    prev = json.loads(prev_file.read_text())
    values = {
        "locations": (new["counts"]["locations"], prev["counts"]["locations"]),
        "reports_parsed": (new["counts"]["reports_parsed"], prev["counts"]["reports_parsed"]),
        "graded": (graded(new), graded(prev)),
    }
    failed = False
    for key, (n, p) in values.items():
        ok = p == 0 or n / p >= MIN_RATIO[key]
        failed |= not ok
        print(f"check_build: {key:15} {p:>7} -> {n:>7}  {'ok' if ok else f'FAIL (< {MIN_RATIO[key]:.0%} of previous)'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
