"""Build the static site: copy web/ and write the public JSON data under <out>/data/.

The site is fully static (Netlify/any CDN): the pipeline runs locally, and this step turns the SQLite
database into read-only files.
  data/meta.json            method, letter rules, point table, data windows, counts
  data/locations.json       one compact row per location (map + list)
  data/loc/<record_id>.json full per-location record (inspections, items, violations, complaints)
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

from . import __version__
from .db import now_iso, report_text
from .parse_report import LAST_RISK_FACTOR_ITEM, PARSER_VERSION, header_ehs_name
from .profile import location_profile
from .scoring import (
    DEFAULT_WEIGHT_SET, HALF_LIFE_DAYS, LETTER_MEANING, MIN_EFFECTIVE_N, MODEL_VERSION, PRIOR_STRENGTH, WEIGHT_SETS,
)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

# The 56 items of the county's inspection form (1-29 risk factors, 30-56 good retail practices).
ITEM_NAMES = {
    1: "Person in charge present, demonstrates knowledge, and performs duties",
    2: "Certified Food Protection Manager",
    3: "Management, food employee and conditional employee; knowledge, responsibilities and reporting",
    4: "Proper use of restriction and exclusion",
    5: "Procedures for responding to vomiting and diarrheal events",
    6: "Proper eating, tasting, drinking, or tobacco use",
    7: "No discharge from eyes, nose, and mouth",
    8: "Hands clean and properly washed",
    9: "No bare hand contact with ready-to-eat food, or a pre-approved alternative procedure properly allowed",
    10: "Adequate handwashing sinks properly supplied and accessible",
    11: "Food obtained from approved source",
    12: "Food received at proper temperature",
    13: "Food in good condition, safe, and unadulterated",
    14: "Required records available: shellstock tags, parasite destruction",
    15: "Food separated and protected",
    16: "Food-contact surfaces cleaned and sanitized",
    17: "Proper disposition of returned, previously served, reconditioned and unsafe food",
    18: "Proper cooking time and temperatures",
    19: "Proper reheating procedures for hot holding",
    20: "Proper cooling time and temperature",
    21: "Proper hot holding temperatures",
    22: "Proper cold holding temperatures",
    23: "Proper date marking and disposition",
    24: "Time as a Public Health Control; procedures and records",
    25: "Consumer advisory provided for raw and undercooked food",
    26: "Pasteurized foods used; prohibited foods not offered",
    27: "Food additives; approved and properly used",
    28: "Toxic substances properly identified, stored, and used",
    29: "Compliance with variance, specialized process, HACCP",
    30: "Pasteurized eggs used where required",
    31: "Water and ice from approved source",
    32: "Variance obtained for specialized processing methods",
    33: "Proper cooling methods used; adequate equipment for temperature control",
    34: "Plant food properly cooked for hot holding",
    35: "Approved thawing methods used",
    36: "Thermometers provided and accurate",
    37: "Food properly labeled, original container",
    38: "Insects, rodents, and animals not present",
    39: "Contamination prevented during food preparation, storage, and display",
    40: "Personal cleanliness",
    41: "Wiping cloths properly used and stored",
    42: "Washing fruits and vegetables",
    43: "In-use utensils properly stored",
    44: "Utensils, equipment and linens properly stored, dried and handled",
    45: "Single-use/single-service articles properly stored and used",
    46: "Gloves used properly",
    47: "Food and non-food contact surfaces cleanable, properly designed, constructed, and used",
    48: "Warewashing facilities installed, maintained and used; test strips",
    49: "Non-food contact surfaces clean",
    50: "Hot and cold water available, adequate pressure",
    51: "Plumbing installed, proper backflow devices",
    52: "Sewage and waste water properly disposed",
    53: "Toilet facilities properly constructed, supplied, and cleaned",
    54: "Garbage and refuse properly disposed, facilities maintained",
    55: "Physical facilities installed, maintained, and clean",
    56: "Adequate ventilation and lighting, designated areas used",
}

# Fields of the per-location record that are pipeline internals, not useful to the public.
_DROP_EST = {"mar_address", "mar_validated", "state", "district", "jurisdiction", "stamp"}
# `comments` is the inspector's free-text note: often signed, never shown on the site, so not published.
_DROP_INSP = {"fetch_status", "parse_notes", "comments"}
_COUNTY_EMAIL = re.compile(r"[\w.+-]+@fairfaxcounty\.gov", re.I)


def name_scrubber(con: sqlite3.Connection):
    """A function that redacts inspector names and county email addresses from free text.

    The site does not identify inspectors, but they sometimes write their name or email into observations and
    corrective actions. Names are read from the report headers in memory for this build only; they are not stored.
    """
    names: set[str] = set()
    for r in con.execute("SELECT raw_text, raw_text_z FROM reports WHERE fetch_status='ok'"):
        n = header_ehs_name(report_text(r) or "")
        if n and len(n.split()) >= 2:
            names.add(" ".join(n.split()))
    pat = re.compile(r"\b(?:" + "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in
                                          sorted(names, key=len, reverse=True)) + r")\b", re.I) if names else None

    def scrub(s: str | None) -> str | None:
        if not s:
            return s
        s = _COUNTY_EMAIL.sub("[county email]", s)
        return pat.sub("[inspector]", s) if pat else s
    return scrub


def _dump(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)
    path.write_text(data, encoding="utf-8")
    return len(data)


def _public_profile(p: dict, scrub=lambda s: s) -> dict:
    est = {k: v for k, v in p["establishment"].items() if k not in _DROP_EST}
    sc = p["score"]
    score = None if sc is None else {k: sc[k] for k in (
        "score", "letter", "effective_n", "n_inspections", "category_percentile", "raw_score")}
    inspections = []
    for i in p["inspections"]:
        i = {k: v for k, v in i.items() if k not in _DROP_INSP}
        # The grid says which items were OUT; attach violations to their item for display.
        by_item: dict = {}
        for v in i.pop("violations"):
            by_item.setdefault(v["item_no"], []).append(
                {**{k: v[k] for k in ("code", "title", "tier", "is_repeat", "cdi")},
                 "observation": scrub(v["observation"]), "corrective_action": scrub(v["corrective_action"])})
        i["out_items"] = [
            {**it, "name": ITEM_NAMES.get(it["item_no"]), "violations": by_item.get(it["item_no"], [])}
            for it in i["out_items"]
        ]
        inspections.append(i)
    return {"establishment": est, "score": score, "inspections": inspections,
            "complaints": p["complaints"], "history": p["history"]}


def publish(con: sqlite3.Connection, out: Path, *, weight_set: str = DEFAULT_WEIGHT_SET) -> dict:
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(WEB_DIR, out)
    data = out / "data"
    ws = WEIGHT_SETS[weight_set]

    rows = con.execute(
        """SELECT e.record_id, e.name, e.address, e.city, e.zip, e.category, e.lon, e.lat,
                  s.letter, s.score, s.effective_n, s.n_inspections, s.category_percentile,
                  (SELECT MAX(inspection_date) FROM inspections i WHERE i.record_id = e.record_id) AS last_inspection,
                  (SELECT COUNT(*) FROM complaint_matches m JOIN complaints c USING(record_number)
                    WHERE m.record_id = e.record_id AND m.confidence >= 0.7
                      AND c.submitted_date >= date('now', '-365 days')) AS complaints_12m
           FROM establishments e
           LEFT JOIN scores s ON s.record_id = e.record_id AND s.weight_set = ?
           ORDER BY e.name""",
        (weight_set,),
    ).fetchall()
    locations = [{
        "id": r["record_id"], "name": r["name"], "address": r["address"], "city": r["city"], "zip": r["zip"],
        "category": r["category"],
        "lat": None if r["lat"] is None else round(r["lat"], 6), "lon": None if r["lon"] is None else round(r["lon"], 6),
        "letter": r["letter"], "score": r["score"], "n": r["n_inspections"],
        "eff_n": r["effective_n"], "pct": r["category_percentile"],
        "last": r["last_inspection"], "complaints_12m": r["complaints_12m"],
    } for r in rows]
    size = _dump(data / "locations.json", locations)

    scrub = name_scrubber(con)
    for r in rows:
        p = location_profile(con, r["record_id"], weight_set=weight_set)
        size += _dump(data / "loc" / f"{r['record_id']}.json", _public_profile(p, scrub))

    q1 = lambda sql, *a: con.execute(sql, a).fetchone()
    letters = dict(con.execute(
        "SELECT COALESCE(letter, 'none'), COUNT(*) FROM scores WHERE weight_set=? GROUP BY 1", (weight_set,)).fetchall())
    tiers = ws.tier_mult
    meta = {
        "generated_at": now_iso(), "package_version": __version__, "model_version": MODEL_VERSION,
        "parser_version": PARSER_VERSION, "weight_set": ws.name, "weight_set_description": ws.description,
        "violation_unit": ws.violation_unit,
        "letters": [{"letter": l, "min_score": c, "meaning": LETTER_MEANING[l]} for c, l in ws.letter_cutoffs()]
                   + [{"letter": "F", "min_score": 0, "meaning": LETTER_MEANING["F"]}],
        "letter_counts": letters,
        "points": {
            "risk_factor": {t: ws.rf_points * m for t, m in tiers.items()},
            "good_retail": {t: ws.grp_points * m for t, m in tiers.items()},
            "repeat_multiplier": ws.repeat_mult, "cdi_multiplier": ws.cdi_mult,
            "last_risk_factor_item": LAST_RISK_FACTOR_ITEM,
        },
        "recency_half_life_days": HALF_LIFE_DAYS, "prior_strength": PRIOR_STRENGTH, "min_effective_n": MIN_EFFECTIVE_N,
        "counts": {
            "locations": len(locations),
            "inspections": q1("SELECT COUNT(*) FROM inspections")[0],
            "reports_parsed": q1("SELECT COUNT(*) FROM reports WHERE parse_status='ok'")[0],
            "complaints": q1("SELECT COUNT(*) FROM complaints")[0],
            "complaints_matched": q1("SELECT COUNT(DISTINCT record_number) FROM complaint_matches WHERE confidence >= 0.7")[0],
        },
        "inspection_window": list(q1("SELECT MIN(inspection_date), MAX(inspection_date) FROM inspections")),
        "complaint_window": list(q1("SELECT MIN(submitted_date), MAX(submitted_date) FROM complaints")),
        "items": ITEM_NAMES,
    }
    size += _dump(data / "meta.json", meta)
    return {"out": str(out), "locations": len(locations), "bytes": size}
