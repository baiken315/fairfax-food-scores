"""Compare weight sets (and shrinkage strengths) against outcomes they should predict.

There is no ground truth for "how safe is this restaurant", so we check proxies:

  next_priority_rf   Does the score from a location's history up to inspection t predict that
                     inspection t+1 has a Priority-tier risk-factor OUT?            (AUC)
  next_rf_out        ... the number of risk-factor items OUT at t+1?               (Spearman)
  follow_up          Does inspection t's own score agree with the county requiring a
                     follow-up visit?                                               (AUC)
  complaint_180d     Does the score at t predict a matched complaint within 180 days after t?
                     Only inspections at least 180 days before the newest complaint are used.  (AUC)

Higher AUC / Spearman = the score orders locations more like the outcome does. AUCs come with a
bootstrap 90% interval; overlapping intervals mean the data can't tell the sets apart yet.
Caveats: next_* targets are built from the same grid the item-level sets score, which favours them
somewhat; the category prior uses all data (mild leakage); complaints are allegations.
"""
from __future__ import annotations

import random
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from .parse_report import LAST_RISK_FACTOR_ITEM
from .scoring import (
    HALF_LIFE_DAYS, MIN_CATEGORY_SIZE, WEIGHT_SETS, inspection_score, load_inspection_data, scoreable_inspections,
)

COMPLAINT_HORIZON_DAYS = 180
MIN_MATCH_CONFIDENCE = 0.7


# ----------------------------------------------------------------------------- statistics

def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=xs.__getitem__)
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def auc(risk: list[float], outcome: list[bool]) -> float | None:
    """P(random positive has higher risk than random negative); ties count half."""
    n_pos = sum(outcome)
    n_neg = len(outcome) - n_pos
    if not n_pos or not n_neg:
        return None
    r = _ranks(risk)
    return (sum(rk for rk, o in zip(r, outcome) if o) - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (vx * vy) if vx and vy else None


def auc_interval(risk: list[float], outcome: list[bool], n_boot: int = 300, seed: int = 0) -> tuple[float, float] | None:
    rng = random.Random(seed)
    idx = range(len(risk))
    vals = []
    for _ in range(n_boot):
        s = [rng.choice(idx) for _ in idx]
        a = auc([risk[i] for i in s], [outcome[i] for i in s])
        if a is not None:
            vals.append(a)
    if len(vals) < n_boot // 2:
        return None
    vals.sort()
    return vals[int(0.05 * len(vals))], vals[int(0.95 * len(vals)) - 1]


# ----------------------------------------------------------------------------- harness

def _history_score(hist: list[tuple[date, float]], at: date, prior: float, prior_strength: float) -> float:
    """Recency-weighted mean of past inspection scores (as of `at`), shrunk toward `prior`."""
    ws = [(0.5 ** (max((at - d).days, 0) / HALF_LIFE_DAYS), s) for d, s in hist]
    wsum = sum(w for w, _ in ws)
    mean = sum(w * s for w, s in ws) / wsum
    return (wsum * mean + prior_strength * prior) / (wsum + prior_strength)


def calibrate(con: sqlite3.Connection, *, weight_sets: list[str] | None = None,
              prior_strengths: tuple[float, ...] = (0.0, 1.0, 2.0, 4.0)) -> dict:
    names = weight_sets or list(WEIGHT_SETS)
    rows = scoreable_inspections(con)
    items, viols = load_inspection_data(con)
    cats = {r["record_id"]: r["category"] for r in con.execute("SELECT record_id, category FROM establishments")}
    follow = {r["inspection_id"]: bool(r["follow_up_required"]) for r in con.execute(
        "SELECT inspection_id, follow_up_required FROM report_summary WHERE follow_up_required IS NOT NULL")}

    complaints: dict[str, list[date]] = defaultdict(list)
    for r in con.execute(
        """SELECT m.record_id, c.submitted_date FROM complaint_matches m JOIN complaints c USING(record_number)
           WHERE m.confidence >= ? AND c.submitted_date IS NOT NULL""", (MIN_MATCH_CONFIDENCE,)):
        complaints[r["record_id"]].append(date.fromisoformat(r["submitted_date"]))
    newest_complaint = max((d for ds in complaints.values() for d in ds), default=None)
    oldest_complaint = min((d for ds in complaints.values() for d in ds), default=None)

    by_loc: dict[str, list[tuple[date, str]]] = defaultdict(list)
    for r in rows:
        by_loc[r["record_id"]].append((date.fromisoformat(r["d"]), r["inspection_id"]))
    for v in by_loc.values():
        v.sort()

    def rf_out(iid: str) -> list[dict]:
        return [it for n, it in items.get(iid, {}).items() if n <= LAST_RISK_FACTOR_ITEM and it["status"] == "OUT"]

    report: dict = {
        "n_inspections": len(rows), "n_locations": len(by_loc),
        "n_pairs": sum(max(len(v) - 1, 0) for v in by_loc.values()),
        "complaint_window": (oldest_complaint, newest_complaint), "sets": {},
    }

    for name in names:
        ws = WEIGHT_SETS[name]
        score = {iid: inspection_score(items.get(iid, {}), viols.get(iid, []), ws) for _, iid in
                 (x for v in by_loc.values() for x in v)}

        # category priors (mean inspection score), as in scoring.compute_scores
        cat_scores: dict[str | None, list[float]] = defaultdict(list)
        for rid, v in by_loc.items():
            cat_scores[cats.get(rid)].extend(score[iid] for _, iid in v)
        all_scores = [s for v in cat_scores.values() for s in v]
        global_mean = sum(all_scores) / len(all_scores) if all_scores else 100.0
        prior = {c: (sum(v) / len(v) if len(v) >= MIN_CATEGORY_SIZE else global_mean) for c, v in cat_scores.items()}

        res: dict = {}
        # next-inspection targets, one row per (location, t -> t+1) pair, for each prior strength
        for ps in prior_strengths:
            risk, y_pri, y_cnt = [], [], []
            for rid, v in by_loc.items():
                for k in range(len(v) - 1):
                    hist = [(d, score[iid]) for d, iid in v[: k + 1]]
                    nxt_date, nxt = v[k + 1]
                    risk.append(-_history_score(hist, nxt_date, prior[cats.get(rid)], ps))
                    outs = rf_out(nxt)
                    y_pri.append(any((it["tier"] or "") == "priority" for it in outs))
                    y_cnt.append(len(outs))
            res[f"next_priority_rf@ps={ps:g}"] = (auc(risk, y_pri), auc_interval(risk, y_pri), len(risk), sum(y_pri))
            res[f"next_rf_out@ps={ps:g}"] = (spearman(risk, y_cnt), None, len(risk), None)

        # same-inspection target
        risk, y = [], []
        for v in by_loc.values():
            for _, iid in v:
                if iid in follow:
                    risk.append(-score[iid])
                    y.append(follow[iid])
        res["follow_up"] = (auc(risk, y), auc_interval(risk, y), len(risk), sum(y))

        # complaint target
        if newest_complaint and oldest_complaint:
            risk, y = [], []
            cutoff = newest_complaint - timedelta(days=COMPLAINT_HORIZON_DAYS)
            for rid, v in by_loc.items():
                for d, iid in v:
                    if oldest_complaint <= d <= cutoff:
                        risk.append(-score[iid])
                        y.append(any(d < c <= d + timedelta(days=COMPLAINT_HORIZON_DAYS) for c in complaints.get(rid, [])))
            res["complaint_180d"] = (auc(risk, y), auc_interval(risk, y), len(risk), sum(y))
        report["sets"][name] = res
    return report


def format_report(rep: dict) -> str:
    lines = [
        "# Calibration report",
        "",
        f"- Scoreable inspections: {rep['n_inspections']} at {rep['n_locations']} locations; "
        f"consecutive-inspection pairs: {rep['n_pairs']}",
        f"- Complaint window: {rep['complaint_window'][0]} to {rep['complaint_window'][1]}",
        "- AUC 0.5 = no better than chance; ranges are bootstrap 90% intervals. `ps` = shrinkage prior strength.",
        "",
    ]
    targets = list(next(iter(rep["sets"].values())).keys()) if rep["sets"] else []
    lines.append("| target (n, positives) | " + " | ".join(rep["sets"]) + " |")
    lines.append("|---|" + "---|" * len(rep["sets"]))
    for t in targets:
        first = next(iter(rep["sets"].values()))[t]
        label = f"{t} (n={first[2]}" + (f", pos={first[3]})" if first[3] is not None else ")")
        cells = []
        for res in rep["sets"].values():
            val, ci, _, _ = res[t]
            if val is None:
                cells.append("n/a")
            else:
                cells.append(f"{val:.3f}" + (f" [{ci[0]:.2f}-{ci[1]:.2f}]" if ci else ""))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
