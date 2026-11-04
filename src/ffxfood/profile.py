"""Per-location record: everything known about one establishment, nested and source-linked."""
from __future__ import annotations

import sqlite3

from .scoring import DEFAULT_WEIGHT_SET


def location_profile(con: sqlite3.Connection, record_id: str, *, weight_set: str = DEFAULT_WEIGHT_SET) -> dict | None:
    e = con.execute("SELECT * FROM establishments WHERE record_id=?", (record_id,)).fetchone()
    if e is None:
        return None
    est = {k: e[k] for k in e.keys() if k not in ("source_objectid", "fetched_at")}

    score = con.execute("SELECT * FROM scores WHERE record_id=? AND weight_set=?", (record_id, weight_set)).fetchone()
    inspections = []
    for i in con.execute(
        """SELECT i.inspection_id, COALESCE(s.inspection_date, i.inspection_date) AS date, i.result, i.report_url,
                  r.fetch_status, r.parse_status, r.parse_notes, s.purpose, s.rf_out, s.rf_repeat_out, s.grp_out,
                  s.follow_up_required, s.comments, x.score
           FROM inspections i
           LEFT JOIN reports r USING(inspection_id)
           LEFT JOIN report_summary s USING(inspection_id)
           LEFT JOIN inspection_scores x ON x.inspection_id = i.inspection_id AND x.weight_set = ?
           WHERE i.record_id = ? ORDER BY date DESC""",
        (weight_set, record_id),
    ):
        iid = i["inspection_id"]
        out_items = [dict(r) for r in con.execute(
            "SELECT item_no, section, tier, is_repeat, all_cdi, n_violations FROM report_items "
            "WHERE inspection_id=? AND status='OUT' ORDER BY item_no", (iid,))]
        violations = [dict(r) for r in con.execute(
            "SELECT item_no, code, title, tier, is_repeat, cdi, observation, corrective_action FROM violations "
            "WHERE inspection_id=? ORDER BY seq", (iid,))]
        inspections.append({**dict(i), "out_items": out_items, "violations": violations})

    complaints = [dict(r) for r in con.execute(
        """SELECT c.record_number, c.submitted_date, c.status, c.project_name, c.work_location, c.detail_url,
                  m.method AS match_method, m.confidence AS match_confidence
           FROM complaint_matches m JOIN complaints c USING(record_number)
           WHERE m.record_id=? ORDER BY c.submitted_date DESC""", (record_id,))]
    history = [dict(r) for r in con.execute(
        "SELECT field, old_value, new_value, changed_at FROM establishment_history WHERE record_id=? ORDER BY changed_at",
        (record_id,))]

    return {
        "establishment": est,
        "score": dict(score) if score else None,
        "inspections": inspections,
        "complaints": complaints,          # allegations: category and outcome are not public
        "history": history,
    }
