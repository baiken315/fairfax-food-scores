"""Worksheet: one location's grade arithmetic, tabulated, cross-checked against the model's own output.

`ffx worksheet HFOOD-...` writes a self-contained HTML page: per routine inspection one row per OUT item
showing every multiplier scoring.py applies (base, tier, repeat, CDI, per-item), the violation text beside
it; then the location rollup: recency weights, weighted mean, category shrinkage, letter cut-offs.

Everything is recomputed here from the scoring.py primitives and compared against the stored
inspection_scores/scores rows, so the page shows the model's true arithmetic, not a re-explanation of it.
"""
from __future__ import annotations

import html
import sqlite3
from collections import defaultdict
from datetime import date

from .parse_report import LAST_RISK_FACTOR_ITEM
from .publish import ITEM_NAMES
from .scoring import (
    DEFAULT_WEIGHT_SET, HALF_LIFE_DAYS, LETTER_MEANING, MIN_CATEGORY_SIZE, MIN_EFFECTIVE_N, MODEL_VERSION,
    PRIOR_STRENGTH, WEIGHT_SETS, inspection_deduction, letter_for, load_inspection_data, scoreable_inspections,
)

LETTER_COLORS = {"A": "#1e7b45", "B": "#7aa832", "C": "#c9a227", "D": "#d97b29", "F": "#c0392b", "": "#888888"}


def worksheet(con: sqlite3.Connection, record_id: str, *, weight_set: str = DEFAULT_WEIGHT_SET,
              as_of: date | None = None) -> dict | None:
    """All numbers behind one location's grade, with the stored values for cross-checking.

    as_of defaults to the date of the stored scores. compute_scores() ages inspections by the *local*
    date but stamps computed_at in UTC, so the first candidate can be a day late; the candidate whose
    recomputation reproduces the stored rows wins.
    """
    ws = WEIGHT_SETS[weight_set]
    e = con.execute("SELECT * FROM establishments WHERE record_id=?", (record_id,)).fetchone()
    if e is None:
        return None
    stored = con.execute("SELECT * FROM scores WHERE record_id=? AND weight_set=?", (record_id, weight_set)).fetchone()

    items, viols = load_inspection_data(con)
    cats = {r["record_id"]: r["category"] for r in con.execute("SELECT record_id, category FROM establishments")}
    stored_ins = {r["inspection_id"]: r["score"] for r in
                  con.execute("SELECT inspection_id, score FROM inspection_scores WHERE weight_set=?", (weight_set,))}
    full_viols: dict = defaultdict(list)   # this location's violations with text, for the breakdown
    for r in con.execute(
        """SELECT v.inspection_id, v.item_no, v.code, v.title, v.tier, v.is_repeat, v.cdi, v.observation,
                  v.corrective_action
           FROM violations v JOIN inspections i USING(inspection_id)
           WHERE i.record_id=? ORDER BY v.seq""", (record_id,)):
        full_viols[r["inspection_id"]].append(dict(r))

    if as_of is not None:
        candidates = [as_of]
    elif stored:
        d0 = date.fromisoformat(stored["computed_at"][:10])
        candidates = [d0, date.fromordinal(d0.toordinal() - 1)]
    else:
        candidates = [date.today()]

    out = {}
    for cand in candidates:
        out = _at(con, record_id, ws, cand, items, viols, full_viols, cats, stored_ins, stored, e)
        checks = out["rollup"]["checks"] if out["rollup"] else {}
        if not checks or all(checks.values()):
            break
    return out


def _at(con, record_id, ws, as_of, items, viols, full_viols, cats, stored_ins, stored, e) -> dict:
    per_est: dict[str, list[tuple[float, float]]] = defaultdict(list)
    inspections: list[dict] = []
    for r in scoreable_inspections(con):
        iid, rid = r["inspection_id"], r["record_id"]
        ded = inspection_deduction(items.get(iid, {}), viols.get(iid, []), ws)
        sc = max(0.0, 100.0 - ded)
        age = max((as_of - date.fromisoformat(r["d"])).days, 0)
        w = 0.5 ** (age / HALF_LIFE_DAYS)
        per_est[rid].append((w, sc))
        if rid != record_id:
            continue
        entry = {"inspection_id": iid, "date": r["d"], "age": age, "weight": w,
                 "deduction": ded, "score": sc, "stored_score": stored_ins.get(iid)}
        if ws.unit == "item":
            entry["items"] = _item_breakdown(items.get(iid, {}), full_viols.get(iid, []), ws)
        inspections.append(entry)
    inspections.sort(key=lambda x: x["date"], reverse=True)

    scoreable_ids = {e2["inspection_id"] for e2 in inspections}
    excluded = []
    for r in con.execute(
        """SELECT i.inspection_id, COALESCE(s.inspection_date, i.inspection_date) AS d, s.purpose,
                  i.report_url, r.fetch_status, r.parse_status
           FROM inspections i
           LEFT JOIN reports r USING(inspection_id)
           LEFT JOIN report_summary s USING(inspection_id)
           WHERE i.record_id=? ORDER BY d DESC""", (record_id,)):
        if r["inspection_id"] in scoreable_ids:
            continue
        if r["fetch_status"] != "ok" or r["parse_status"] != "ok":
            reason = f"report not readable (fetch={r['fetch_status'] or 'never'}, parse={r['parse_status'] or 'never'})"
        elif r["purpose"] and not r["purpose"].startswith("Routine"):
            reason = f"not a routine inspection ({r['purpose']})"
        else:
            reason = "excluded by the scoring filter"
        excluded.append({"date": r["d"], "reason": reason, "report_url": r["report_url"]})

    return {
        "establishment": {k: e[k] for k in ("record_id", "name", "address", "city", "zip", "category")},
        "weight_set": ws.name, "description": ws.description, "unit": ws.unit, "model_version": MODEL_VERSION,
        "as_of": as_of.isoformat(), "computed_at": stored["computed_at"] if stored else None,
        "params": {"rf_points": ws.rf_points, "grp_points": ws.grp_points, "tier_mult": dict(ws.tier_mult),
                   "repeat_mult": ws.repeat_mult, "cdi_mult": ws.cdi_mult,
                   "item_mult": ({str(k): v for k, v in ws.item_mult.items()} if ws.item_mult else {}),
                   "half_life_days": HALF_LIFE_DAYS, "prior_strength": PRIOR_STRENGTH,
                   "min_effective_n": MIN_EFFECTIVE_N},
        "inspections": inspections,
        "excluded": excluded,
        "rollup": _rollup(record_id, ws, per_est, cats, stored),
        "stored": dict(stored) if stored else None,
    }


def _item_breakdown(items: dict, viols: list, ws) -> list[dict]:
    """One row per OUT item: the exact factors inspection_deduction() multiplies for unit="item"."""
    by_item: dict = defaultdict(list)
    for v in viols:
        by_item[v["item_no"]].append(v)
    out = []
    for n in sorted(items):
        it = items[n]
        if it["status"] != "OUT":
            continue
        tier = (it["tier"] or "core").lower()
        base = ws.rf_points if n <= LAST_RISK_FACTOR_ITEM else ws.grp_points
        tier_mult = ws.tier_mult.get(tier, 1.0)
        repeat_mult = ws.repeat_mult if it["is_repeat"] else 1.0
        cdi_mult = ws.cdi_mult if it["all_cdi"] else 1.0
        item_mult = ws.item_mult.get(n, 1.0)
        out.append({"item_no": n, "name": ITEM_NAMES.get(n), "risk_factor": n <= LAST_RISK_FACTOR_ITEM,
                    "base": base, "tier": tier, "tier_mult": tier_mult, "is_repeat": bool(it["is_repeat"]),
                    "repeat_mult": repeat_mult, "all_cdi": bool(it["all_cdi"]), "cdi_mult": cdi_mult,
                    "item_mult": item_mult, "points": base * tier_mult * repeat_mult * cdi_mult * item_mult,
                    "violations": by_item.get(n, [])})
    return out


def _rollup(record_id, ws, per_est, cats, stored) -> dict | None:
    """The per-location aggregation from compute_scores(), kept step by step for one location."""
    raw: dict[str, tuple[float, float, int]] = {}
    for rid, obs in per_est.items():
        wsum = sum(w for w, _ in obs)
        raw[rid] = (sum(w * s for w, s in obs) / wsum, wsum, len(obs))
    if record_id not in raw:
        return None

    def mean_of(rids) -> float:
        den = sum(raw[r][1] for r in rids)
        return sum(raw[r][0] * raw[r][1] for r in rids) / den if den else 100.0

    by_cat: dict = defaultdict(list)
    for rid in raw:
        by_cat[cats.get(rid)].append(rid)
    global_mean = mean_of(list(raw))

    def prior_for(cat) -> tuple[float, str, int]:
        group = by_cat[cat]
        return (mean_of(group), "category", len(group)) if len(group) >= MIN_CATEGORY_SIZE \
            else (global_mean, "county-wide (category too small)", len(raw))

    shrunk_all = {}
    for rid, (mean, wsum, _) in raw.items():
        prior, _, _ = prior_for(cats.get(rid))
        shrunk_all[rid] = (wsum * mean + PRIOR_STRENGTH * prior) / (wsum + PRIOR_STRENGTH)

    cat = cats.get(record_id)
    prior, prior_kind, prior_n = prior_for(cat)
    mean, wsum, n = raw[record_id]
    v = shrunk_all[record_id]
    peers = by_cat[cat] if len(by_cat[cat]) >= MIN_CATEGORY_SIZE else list(raw)
    pct = 100.0 * sum(1 for r in peers if shrunk_all[r] > v) / max(len(peers), 1)

    checks = {}
    if stored:
        checks = {"raw": round(mean, 2) == stored["raw_score"], "eff_n": round(wsum, 3) == stored["effective_n"],
                  "score": round(v, 2) == stored["score"], "letter": letter_for(v, wsum, ws) == stored["letter"],
                  "pct": round(pct, 1) == stored["category_percentile"]}
    return {"raw": mean, "wsum": wsum, "n": n, "prior": prior, "prior_kind": prior_kind, "prior_n": prior_n,
            "shrunk": v, "percentile": pct, "letter": letter_for(v, wsum, ws),
            "cutoffs": ws.letter_cutoffs(), "violation_unit": ws.violation_unit,
            "insufficient": wsum < MIN_EFFECTIVE_N, "checks": checks}


# ------------------------------------------------------------------ rendering

def _esc(s) -> str:
    return html.escape("" if s is None else str(s))


def _mult(x: float) -> str:
    return "×" + f"{x:.2f}".rstrip("0").rstrip(".")


def _chip(ok: bool | None, label_ok="matches stored", label_bad="differs from stored") -> str:
    if ok is None:
        return ""
    return f'<span class="{"ok" if ok else "bad"}">{"✓" if ok else "⚠"} {_esc(label_ok if ok else label_bad)}</span>'


_CSS = """
:root{color-scheme:light}body{font:14px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif;margin:0;background:#f6f6f4;color:#1c1c1c}
main{max-width:1100px;margin:0 auto;padding:28px 20px 80px}h1{font-size:22px;margin:0}h2{font-size:15px;margin:30px 0 8px}
.sub{color:#666;margin:2px 0 0}.sec-note{color:#666;font-size:12.5px;margin:4px 0 10px;max-width:70ch}
table{border-collapse:collapse;width:100%;background:#fff;font-variant-numeric:tabular-nums}
th,td{padding:6px 9px;border-bottom:1px solid #e4e4e0;text-align:left;vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#666;background:#fafaf8}
td.n,th.n{text-align:right;white-space:nowrap}tr.total td{font-weight:700;background:#fafaf8}
td.words{background:#fcfcfa;color:#555;font-size:12.5px}td.words b{color:#333;font-weight:600}
.insp{background:#fff;border:1px solid #e4e4e0;border-radius:8px;padding:14px 16px;margin:10px 0}
.insp .head{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.insp .head b{font-size:15px}.insp .score{margin-left:auto;font-weight:700;font-size:16px;white-space:nowrap}
.formula{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;background:#fff;border:1px solid #e4e4e0;
border-radius:6px;padding:10px 12px;margin:8px 0;overflow-x:auto;white-space:nowrap}
.params{font-size:13px;background:#fff;border:1px solid #e4e4e0;border-radius:6px;padding:10px 14px;color:#444}
.params b{color:#111}.grade{display:inline-block;min-width:44px;height:44px;border-radius:8px;color:#fff;
font-size:24px;font-weight:700;text-align:center;line-height:44px;padding:0 8px;vertical-align:middle}
.ok{color:#1e7b45}.bad{color:#c0392b}.ladder tr.hit td{background:#eef7f0;font-weight:600}
a{color:#2b7de9}footer{margin-top:36px;color:#888;font-size:12.5px}
"""


def worksheet_html(con: sqlite3.Connection, record_id: str, *, weight_set: str = DEFAULT_WEIGHT_SET) -> str | None:
    w = worksheet(con, record_id, weight_set=weight_set)
    if w is None:
        return None
    e, r, p = w["establishment"], w["rollup"], w["params"]
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Grade worksheet — {_esc(e['name'])}</title><style>{_CSS}</style></head><main>"]
    A = parts.append

    A(f"<h1>Grade worksheet: {_esc(e['name'])}</h1>")
    addr = ", ".join(x for x in (e["address"], e["city"], e["zip"]) if x)
    A(f"<p class='sub'>{_esc(addr)} · {_esc(e['category'] or '')} · {_esc(e['record_id'])}</p>")
    A(f"<p class='sec-note'>Every number below is recomputed by <code>scoring.py</code> under weight set "
      f"<b>{_esc(w['weight_set'])}</b> ({_esc(w['description'])}), as of <b>{_esc(w['as_of'])}</b> — the date of the last "
      f"<code>ffx score</code> run{'' if not w['computed_at'] else ' (' + _esc(w['computed_at']) + ')'}. "
      f"Model v{_esc(w['model_version'])}.</p>")

    tiers = " · ".join(f"{t} {_mult(m)}" for t, m in p["tier_mult"].items())
    items_over = " · ".join(f"item {k} {_mult(v)}" for k, v in p["item_mult"].items()) or "none"
    A(f"<div class='params'>base: risk factor <b>{p['rf_points']}</b>, good retail <b>{p['grp_points']}</b> "
      f"&nbsp;|&nbsp; tier: <b>{tiers}</b> &nbsp;|&nbsp; repeat <b>{_mult(p['repeat_mult'])}</b> "
      f"&nbsp;|&nbsp; CDI <b>{_mult(p['cdi_mult'])}</b> &nbsp;|&nbsp; per-item overrides: <b>{items_over}</b> "
      f"<br>half-life <b>{p['half_life_days']:.0f} d</b> &nbsp;|&nbsp; prior strength <b>{p['prior_strength']}</b> "
      f"&nbsp;|&nbsp; min effective_n <b>{p['min_effective_n']}</b> (below: no letter)")

    # ---- per-inspection tables
    A("<h2>1 · Per inspection: points off 100</h2>")
    A("<p class='sec-note'>One row per OUT item — an item costs once however many citations it carries. "
      "Points = base × tier × repeat × CDI × item. IN items are worth nothing; N/O and N/A are ignored.</p>")
    if not w["inspections"]:
        A("<p class='sec-note'>No scoreable routine inspections (see the exclusions table).</p>")
    for ins in w["inspections"]:
        A(f"<div class='insp'><div class='head'><b>{_esc(ins['date'])}</b>"
          f"<span class='sub'>routine · parsed report</span>"
          f"<span class='score'>{ins['score']:.2f}/100 {_chip(ins.get('stored_score') is None or
             round(ins['score'], 2) == ins['stored_score'])}</span></div>")
        if "items" not in ins:
            A("<p class='sec-note'>This weight set scores at "
              f"<b>{_esc(w['unit'])}</b> level, so there is no per-item breakdown; the score above is still the model's.</p>")
        elif not ins["items"]:
            A("<p class='sec-note'>No items out of compliance: nothing to subtract, score 100.</p>")
        else:
            A("<table><tr><th class='n'>#</th><th>Item</th><th>Kind</th><th class='n'>Base</th>"
              "<th>Tier</th><th class='n'>×</th><th>Repeat</th><th class='n'>×</th><th>CDI</th><th class='n'>×</th>"
              "<th class='n'>Points</th></tr>")
            for it in ins["items"]:
                A(f"<tr><td class='n'>{it['item_no']}</td><td>{_esc(it['name'])}</td>"
                  f"<td>{'Risk factor' if it['risk_factor'] else 'Good retail'}</td>"
                  f"<td class='n'>{it['base']:.1f}</td>"
                  f"<td>{_esc(it['tier'].title())}</td><td class='n'>{_mult(it['tier_mult'])}</td>"
                  f"<td>{'yes' if it['is_repeat'] else '—'}</td><td class='n'>{_mult(it['repeat_mult'])}</td>"
                  f"<td>{'all' if it['all_cdi'] else '—'}</td><td class='n'>{_mult(it['cdi_mult'])}</td>"
                  f"<td class='n'><b>{it['points']:.1f}</b></td></tr>")
                for v in it["violations"]:
                    words = [f"<b>{_esc(v['code'] or '')} {_esc(v['title'] or '')}</b>"]
                    if v.get("observation"):
                        words.append(f"Observed: {_esc(v['observation'])}")
                    if v.get("corrective_action"):
                        words.append(f"Required: {_esc(v['corrective_action'])}")
                    A(f"<tr><td class='words' colspan='11'>{' — '.join(words)}</td></tr>")
            A(f"<tr class='total'><td colspan='10'>Deduction = sum of points</td>"
              f"<td class='n'>{ins['deduction']:.1f}</td></tr></table>")
        A(f"<p class='formula'>inspection score = max(0, 100 − {ins['deduction']:.1f}) = <b>{ins['score']:.2f}</b></p>")
        A("</div>")

    # ---- rollup
    A("<h2>2 · Per location: recent inspections weigh more</h2>")
    if not r:
        A("<p class='sec-note'>No scoreable inspections, so there is no rollup and no grade.</p>")
    else:
        A("<table><tr><th>Date</th><th class='n'>Score</th><th class='n'>Age (days)</th>"
          f"<th class='n'>Weight = 0.5^(age/{p['half_life_days']:.0f})</th><th class='n'>Weight × score</th></tr>")
        for ins in w["inspections"]:
            A(f"<tr><td>{_esc(ins['date'])}</td><td class='n'>{ins['score']:.2f}</td>"
              f"<td class='n'>{ins['age']}</td><td class='n'>{ins['weight']:.3f}</td>"
              f"<td class='n'>{ins['weight'] * ins['score']:.2f}</td></tr>")
        wsum_s = sum(i["weight"] for i in w["inspections"])
        ws_s = sum(i["weight"] * i["score"] for i in w["inspections"])
        A(f"<tr class='total'><td>Totals</td><td></td><td></td><td class='n'>{wsum_s:.3f}</td>"
          f"<td class='n'>{ws_s:.2f}</td></tr></table>")
        A(f"<p class='formula'>raw score = Σ(weight×score) / Σweight = {ws_s:.2f} / {wsum_s:.3f} = <b>{r['raw']:.2f}</b>"
          f" {_chip(r['checks'].get('raw'))}</p>")
        A(f"<p class='formula'>effective_n = Σweight = <b>{r['wsum']:.3f}</b> "
          f"({_esc(r['n'])} inspections) {_chip(r['checks'].get('eff_n'))}</p>")

        A(f"<h2>3 · Shrink toward peers: one inspection can't decide a grade</h2>")
        A(f"<div class='params'>prior = weighted mean of the <b>{r['prior_n']}</b> {_esc(r['prior_kind'])} "
          f"establishments' raw scores = <b>{r['prior']:.2f}</b></div>")
        A(f"<p class='formula'>score = (effective_n × raw + prior_strength × prior) / (effective_n + prior_strength)"
          f"<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;= ({r['wsum']:.3f} × {r['raw']:.2f} + "
          f"{p['prior_strength']} × {r['prior']:.2f}) / ({r['wsum']:.3f} + {p['prior_strength']}) = <b>{r['shrunk']:.2f}</b>"
          f" {_chip(r['checks'].get('score'))}</p>")
        A(f"<p class='formula'>percentile = share of peers with a higher score = {r['percentile']:.1f}% "
          f"(0 = best in category) {_chip(r['checks'].get('pct'))}</p>")

        A("<h2>4 · Letter: cut-offs in 'serious-violation equivalents'</h2>")
        A(f"<p class='sec-note'>One serious violation = <b>{r['violation_unit']:.1f} points</b> under this weight set, "
          f"so the letter boundaries sit at 100 − k × {r['violation_unit']:.1f}. A grade is therefore a statement "
          f"about how many serious violations' worth of points a place loses per visit, on average.</p>")
        if r["insufficient"]:
            A(f"<p class='sec-note'><b>effective_n {r['wsum']:.3f} &lt; {p['min_effective_n']}</b>: too little recent "
              f"data, so the model refuses to assign a letter.</p>")
        hit = r["letter"] or ""
        A(f"<table class='ladder'><tr><th>Letter</th><th class='n'>Score from</th><th>Meaning</th></tr>")
        ladder = [(c, l) for c, l in r["cutoffs"]] + [(0.0, "F")]
        for cutoff, letter in ladder:
            A(f"<tr class='{'hit' if letter == hit else ''}'><td>"
              f"<span class='grade' style='background:{LETTER_COLORS[letter]};width:26px;height:26px;"
              f"font-size:15px;line-height:26px'>{letter}</span></td>"
              f"<td class='n'>{cutoff:.1f}</td><td>{_esc(LETTER_MEANING[letter])}</td></tr>")
        A("</table>")
        if hit:
            A(f"<p style='margin-top:14px'><span class='grade' style='background:{LETTER_COLORS[hit]}'>{hit}</span>"
              f"&nbsp; final score <b>{r['shrunk']:.2f}</b> "
              f"{_chip(r['checks'].get('letter'))}</p>")
        else:
            A("<p style='margin-top:14px'><span class='grade' style='background:#888'>—</span>&nbsp; no grade "
              f"{_chip(r['checks'].get('letter'), 'matches stored (none)', 'disagrees with stored')}</p>")

    # ---- exclusions + footer
    if w["excluded"]:
        A("<h2>Excluded from the grade</h2>")
        A("<table><tr><th>Date</th><th>Why not counted</th><th>County report</th></tr>")
        for x in w["excluded"]:
            link = f"<a href='{_esc(x['report_url'])}' target='_blank' rel='noopener'>open</a>" if x["report_url"] else "—"
            A(f"<tr><td>{_esc(x['date'])}</td><td>{_esc(x['reason'])}</td><td>{link}</td></tr>")
        A("</table>")

    all_ok = all(w["rollup"]["checks"].values()) if w["rollup"] and w["rollup"]["checks"] else w["rollup"] is None
    per_ins_ok = all(round(i["score"], 2) == i["stored_score"] for i in w["inspections"]
                     if i.get("stored_score") is not None)
    summary = ("<span class='ok'>✓ Every number above matches the rows stored by ffx score.</span>"
               if all_ok and per_ins_ok else
               "<span class='bad'>⚠ Some recomputed numbers differ from the stored rows — re-run `ffx score` "
               "or investigate drift.</span>")
    A(f"<footer>{summary}<br>Generated by <code>ffx worksheet</code> from data/ffx.sqlite. "
      "Source reports: Fairfax County Health Department.</footer></main></body></html>")
    return "".join(parts)
