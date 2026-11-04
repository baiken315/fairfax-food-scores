"""Grade model v0.2: item-level, several named weight sets, absolute score + peer percentile.

NOT validated. Each weight set is a hypothesis; `ffx calibrate` compares them. Until one is chosen,
DEFAULT_WEIGHT_SET was chosen on 2026-10-23 (see docs/scoring-research.md).

Per inspection
  * Unit = an OUT grid item, counted once however many citations it carries (unit="item").
    The "v0-tier" set instead counts every violation (unit="violation") to reproduce the old model.
  * IN items carry no deduction; N/O and N/A are ignored.
  * deduction(item) = base(risk factor 1-29 vs good retail 30-56)
                      x tier_mult[most severe tier cited] x repeat_mult (if any citation REPEAT)
                      x cdi_mult (if every citation corrected during inspection) x item_mult[item]
  * inspection score = max(0, 100 - sum of deductions)
  * The "rate" set is different in kind: 100 x (1 - (0.8 x RF out-rate + 0.2 x GRP out-rate)),
    out-rate = OUT / (IN + OUT) over applicable items.

Per location
  * Recency-weighted mean of inspection scores (half-life HALF_LIFE_DAYS); weight sum = effective_n.
  * Shrunk toward the category mean with PRIOR_STRENGTH pseudo-inspections.
  * Absolute score -> letter by LETTER_RULES (violation-equivalents lost per inspection), or no letter
    when effective_n < MIN_EFFECTIVE_N.
  * Category percentile kept as a secondary "vs similar places" figure (0 = best).
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .db import now_iso
from .parse_report import LAST_RISK_FACTOR_ITEM

MODEL_VERSION = "0.2.0"

HALF_LIFE_DAYS = 365.0
PRIOR_STRENGTH = 1.0            # best of 0/1/2/4 in the 2026-10-23 calibration
MIN_CATEGORY_SIZE = 25           # fewer establishments than this -> category prior/percentile use everyone
MIN_EFFECTIVE_N = 0.75           # below this: "insufficient data", no letter

# Letters are defined in "violation-equivalents": the points one Priority-tier risk-factor violation costs
# under the weight set (6 for rf-heavy). Each letter is then one sentence that is true by construction.
# Chosen 2026-10-23 (rf-heavy spread: A 30% / B 32% / C 29% / D 8% / F 2%).
LETTER_RULES = [(1, "A"), (2, "B"), (4, "C"), (6, "D")]     # max violation-equivalents lost per inspection
LETTER_MEANING = {
    "A": "Loses less than one serious violation's worth of points per inspection, on average",
    "B": "Loses up to 2 serious violations' worth of points per inspection, on average",
    "C": "Loses up to 4 serious violations' worth of points per inspection, on average",
    "D": "Loses up to 6 serious violations' worth of points per inspection, on average",
    "F": "Loses more than 6 serious violations' worth of points per inspection, on average",
}

# CDC's major foodborne-illness risk-factor items most tied to outbreaks.
OUTBREAK_DRIVER_ITEMS = {8: 2.0, 15: 2.0, 18: 2.0, 20: 2.0, 21: 2.0, 22: 2.0}

# Petran (2012, Univ. of Minnesota dissertation, ch. 3, Table 6): the 11 routine-inspection violations
# significantly more likely at outbreak restaurants, mapped from FDA Form 3-A to Fairfax's 56-item form.
# 7 of the 11 are good retail practices, which is why the "flat" and "petran" sets exist.
PETRAN_ITEMS = {i: 2.0 for i in (6, 9, 20, 22, 33, 37, 39, 44, 45, 49, 56)}


@dataclass(frozen=True)
class WeightSet:
    name: str
    description: str
    unit: str = "item"                                   # "item" | "violation" | "rate"
    rf_points: float = 4.0                               # base deduction for an OUT risk-factor item (1-29)
    grp_points: float = 1.0                              # base deduction for an OUT good-retail item (30-56)
    tier_mult: dict = field(default_factory=lambda: {"priority": 1.5, "priority foundation": 1.2, "core": 1.0})
    repeat_mult: float = 2.0
    cdi_mult: float = 1.0
    item_mult: dict = field(default_factory=dict)
    unit_override: float | None = None                   # for sets where "one Priority violation" isn't defined

    @property
    def violation_unit(self) -> float:
        """Points one Priority-tier risk-factor violation costs: the unit letters are defined in."""
        if self.unit_override is not None:
            return self.unit_override
        return self.rf_points * self.tier_mult.get("priority", 1.0)

    def letter_cutoffs(self) -> list[tuple[float, str]]:
        return [(100.0 - n * self.violation_unit, letter) for n, letter in LETTER_RULES]


WEIGHT_SETS: dict[str, WeightSet] = {ws.name: ws for ws in [
    WeightSet("v0-tier", "Original v0 model: every violation, Priority 3 / PF 2 / Core 1, repeat x1.5, CDI x0.6",
              unit="violation", rf_points=1.0, grp_points=1.0,
              tier_mult={"priority": 3.0, "priority foundation": 2.0, "core": 1.0}, repeat_mult=1.5, cdi_mult=0.6),
    WeightSet("rf-heavy", "Item-level; risk factors 4x good retail; repeat x2; no CDI discount"),
    WeightSet("rf-heavy-cdi", "As rf-heavy with a small CDI discount (x0.75)", cdi_mult=0.75),
    WeightSet("drivers", "As rf-heavy, outbreak-driver items (8,15,18,20,21,22) x2", item_mult=OUTBREAK_DRIVER_ITEMS),
    WeightSet("flat", "Item-level; risk factors and good retail weighted equally (tests the RF-heavy assumption)",
              rf_points=2.0, grp_points=2.0),
    WeightSet("petran", "As flat, Petran 2012 outbreak-associated items (6,9,20,22,33,37,39,44,45,49,56) x2",
              rf_points=2.0, grp_points=2.0, item_mult=PETRAN_ITEMS),
    WeightSet("rate", "Compliance rates: 100 x (1 - (0.8 RF out-rate + 0.2 GRP out-rate))", unit="rate",
              unit_override=6.0),
]}
DEFAULT_WEIGHT_SET = "rf-heavy"     # chosen 2026-10-23: predicts as well as any set, spreads grades best


def inspection_deduction(items: dict[int, dict], violations: list[dict], ws: WeightSet) -> float:
    """Points off 100 for one inspection. `items`: {item_no: report_items row}; `violations`: violation rows."""
    if ws.unit == "violation":
        total = 0.0
        for v in violations:
            w = ws.tier_mult.get((v["tier"] or "core").lower(), 1.0)
            w *= ws.repeat_mult if v["is_repeat"] else 1.0
            w *= ws.cdi_mult if v["cdi"] else 1.0
            total += w
        return total

    if ws.unit == "rate":
        def out_rate(lo: int, hi: int) -> float:
            st = [it["status"] for n, it in items.items() if lo <= n <= hi and it["status"] in ("IN", "OUT")]
            return st.count("OUT") / len(st) if st else 0.0
        return 100.0 * (0.8 * out_rate(1, LAST_RISK_FACTOR_ITEM) + 0.2 * out_rate(LAST_RISK_FACTOR_ITEM + 1, 99))

    total = 0.0
    for n, it in items.items():
        if it["status"] != "OUT":
            continue
        w = ws.rf_points if n <= LAST_RISK_FACTOR_ITEM else ws.grp_points
        w *= ws.tier_mult.get((it["tier"] or "core").lower(), 1.0)
        w *= ws.repeat_mult if it["is_repeat"] else 1.0
        w *= ws.cdi_mult if it["all_cdi"] else 1.0
        w *= ws.item_mult.get(n, 1.0)
        total += w
    return total


def inspection_score(items: dict[int, dict], violations: list[dict], ws: WeightSet) -> float:
    return max(0.0, 100.0 - inspection_deduction(items, violations, ws))


def letter_for(score: float, effective_n: float, ws: WeightSet | None = None) -> str | None:
    if effective_n < MIN_EFFECTIVE_N:
        return None
    for cutoff, letter in (ws or WEIGHT_SETS[DEFAULT_WEIGHT_SET]).letter_cutoffs():
        if score >= cutoff:
            return letter
    return "F"


def scoreable_inspections(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Routine inspections whose report parsed cleanly, with their date and location."""
    return con.execute(
        """SELECT i.record_id, i.inspection_id, COALESCE(s.inspection_date, i.inspection_date) AS d
           FROM inspections i
           JOIN reports r ON r.inspection_id = i.inspection_id
           JOIN report_summary s ON s.inspection_id = i.inspection_id
           WHERE r.parse_status = 'ok'                       -- only trust reports that passed the cross-checks
             AND i.record_id IN (SELECT record_id FROM establishments)   -- orphans have no category
             AND COALESCE(s.inspection_date, i.inspection_date) IS NOT NULL
             AND COALESCE(s.purpose, 'Routine') LIKE 'Routine%'"""   # follow-ups/complaints skew the signal
    ).fetchall()


def load_inspection_data(con: sqlite3.Connection) -> tuple[dict[str, dict[int, dict]], dict[str, list[dict]]]:
    items: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in con.execute("SELECT inspection_id, item_no, status, tier, is_repeat, all_cdi FROM report_items"):
        items[r["inspection_id"]][r["item_no"]] = dict(r)
    viols: dict[str, list[dict]] = defaultdict(list)
    for r in con.execute("SELECT inspection_id, tier, is_repeat, cdi FROM violations"):
        viols[r["inspection_id"]].append(dict(r))
    return items, viols


def compute_scores(con: sqlite3.Connection, *, as_of: date | None = None,
                   weight_sets: list[str] | None = None, prior_strength: float = PRIOR_STRENGTH) -> int:
    """Score every scoreable inspection and location under each weight set. Returns locations scored."""
    as_of = as_of or date.today()
    sets = [WEIGHT_SETS[n] for n in (weight_sets or WEIGHT_SETS)]
    rows = scoreable_inspections(con)
    items, viols = load_inspection_data(con)
    cats = {r["record_id"]: r["category"] for r in con.execute("SELECT record_id, category FROM establishments")}
    ts = now_iso()

    n_locations = 0
    with con:
        for ws in sets:
            con.execute("DELETE FROM inspection_scores WHERE weight_set=?", (ws.name,))
            con.execute("DELETE FROM scores WHERE weight_set=?", (ws.name,))

            per_est: dict[str, list[tuple[float, float]]] = defaultdict(list)   # record_id -> [(weight, score)]
            ins_rows = []
            for r in rows:
                iid = r["inspection_id"]
                ded = inspection_deduction(items.get(iid, {}), viols.get(iid, []), ws)
                sc = max(0.0, 100.0 - ded)
                ins_rows.append((iid, ws.name, MODEL_VERSION, round(ded, 3), round(sc, 2)))
                age = max((as_of - date.fromisoformat(r["d"])).days, 0)
                per_est[r["record_id"]].append((0.5 ** (age / HALF_LIFE_DAYS), sc))
            con.executemany(
                "INSERT INTO inspection_scores (inspection_id, weight_set, model_version, deduction, score) VALUES (?,?,?,?,?)",
                ins_rows,
            )

            raw: dict[str, tuple[float, float, int]] = {}
            for rid, obs in per_est.items():
                wsum = sum(w for w, _ in obs)
                raw[rid] = (sum(w * s for w, s in obs) / wsum, wsum, len(obs))

            def mean_of(rids: list[str]) -> float:
                den = sum(raw[r][1] for r in rids)
                return sum(raw[r][0] * raw[r][1] for r in rids) / den if den else 100.0

            by_cat: dict[str | None, list[str]] = defaultdict(list)
            for rid in raw:
                by_cat[cats.get(rid)].append(rid)
            global_mean = mean_of(list(raw))

            shrunk: dict[str, float] = {}
            for rid, (mean, wsum, _) in raw.items():
                group = by_cat[cats.get(rid)]
                prior = mean_of(group) if len(group) >= MIN_CATEGORY_SIZE else global_mean
                shrunk[rid] = (wsum * mean + prior_strength * prior) / (wsum + prior_strength)

            for cat, rids in by_cat.items():
                peers = rids if len(rids) >= MIN_CATEGORY_SIZE else list(raw)
                peer_vals = [shrunk[r] for r in peers]
                for rid in rids:
                    v = shrunk[rid]
                    # percentile = share of peers strictly better (higher score); 0 = best
                    pct = 100.0 * sum(1 for x in peer_vals if x > v) / max(len(peer_vals), 1)
                    mean, wsum, n = raw[rid]
                    con.execute(
                        "INSERT INTO scores (record_id, weight_set, model_version, computed_at, n_inspections, effective_n,"
                        " raw_score, score, category, category_percentile, letter) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, ws.name, MODEL_VERSION, ts, n, round(wsum, 3), round(mean, 2), round(v, 2), cat,
                         round(pct, 1), letter_for(v, wsum, ws)),
                    )
            n_locations = len(raw)
    return n_locations
