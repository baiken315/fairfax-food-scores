from datetime import date

from ffxfood.db import connect
from ffxfood.scoring import WEIGHT_SETS, compute_scores, inspection_deduction, letter_for


def _item(status, tier=None, rep=False, cdi=False):
    return {"status": status, "tier": tier, "is_repeat": rep, "all_cdi": cdi}


def test_v0_violation_weights_unchanged():
    v = [
        {"tier": "priority", "is_repeat": 0, "cdi": 0},             # 3
        {"tier": "priority foundation", "is_repeat": 1, "cdi": 0},   # 2 * 1.5 = 3
        {"tier": "core", "is_repeat": 0, "cdi": 1},                  # 1 * 0.6 = 0.6
    ]
    assert abs(inspection_deduction({}, v, WEIGHT_SETS["v0-tier"]) - 6.6) < 1e-9
    assert inspection_deduction({}, [], WEIGHT_SETS["v0-tier"]) == 0


def test_item_level_weights():
    ws = WEIGHT_SETS["rf-heavy"]
    items = {
        8: _item("OUT", "priority", rep=True),          # 4 * 1.5 * 2 = 12
        30: _item("OUT", "core"),                       # 1
        31: _item("IN"), 12: _item("N/O"), 14: _item("N/A"),
    }
    assert inspection_deduction(items, [], ws) == 13
    items[8]["all_cdi"] = True
    assert inspection_deduction(items, [], ws) == 13                                  # no CDI discount
    assert inspection_deduction(items, [], WEIGHT_SETS["rf-heavy-cdi"]) == 12 * 0.75 + 1
    assert inspection_deduction(items, [], WEIGHT_SETS["drivers"]) == 12 * 2 + 1     # item 8 is a driver


def test_item_counted_once_however_many_citations():
    ws = WEIGHT_SETS["rf-heavy"]
    two_citations = [{"tier": "priority", "is_repeat": 0, "cdi": 0}] * 2
    assert inspection_deduction({28: _item("OUT", "priority")}, two_citations, ws) == 6


def test_rate_ignores_not_observed_and_not_applicable():
    ws = WEIGHT_SETS["rate"]
    items = {1: _item("OUT"), 2: _item("IN"), 3: _item("IN"), 4: _item("IN"), 5: _item("N/A"), 6: _item("N/O"),
             30: _item("IN")}
    assert abs(inspection_deduction(items, [], ws) - 100 * 0.8 * 0.25) < 1e-9


def test_letters_and_insufficient_data():
    # rf-heavy: one Priority risk-factor violation = 4 x 1.5 = 6 points -> A >= 94, B >= 88, C >= 76, D >= 64
    assert WEIGHT_SETS["rf-heavy"].letter_cutoffs() == [(94.0, "A"), (88.0, "B"), (76.0, "C"), (64.0, "D")]
    assert [letter_for(s, 1.0) for s in (94, 93.9, 88, 76, 64, 63.9)] == ["A", "B", "B", "C", "D", "F"]
    assert letter_for(95, 0.5) is None
    assert WEIGHT_SETS["v0-tier"].violation_unit == 3.0


def _seed(con, rid, cat, inspections):
    """inspections: [(date, [(item_no, tier), ...OUT items])]"""
    con.execute("INSERT INTO establishments (record_id,name,category,status) VALUES (?,?,?,?)", (rid, rid, cat, "Active"))
    for n, (d, outs) in enumerate(inspections):
        iid = f"{rid}-{n}"
        con.execute("INSERT INTO inspections (inspection_id,record_id,inspection_date) VALUES (?,?,?)", (iid, rid, d))
        con.execute("INSERT INTO reports (inspection_id,fetch_status,parse_status) VALUES (?,?,?)", (iid, "ok", "ok"))
        con.execute("INSERT INTO report_summary (inspection_id,inspection_date,purpose) VALUES (?,?,?)", (iid, d, "Routine"))
        out_items = dict(outs)
        for item in range(1, 57):
            tier = out_items.get(item)
            con.execute(
                "INSERT INTO report_items (inspection_id,item_no,section,status,tier,is_repeat,all_cdi,n_violations) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (iid, item, "risk_factor" if item <= 29 else "good_retail_practice",
                 "OUT" if tier else "IN", tier, 0 if tier else None, 0 if tier else None, 1 if tier else 0),
            )
            if tier:
                con.execute("INSERT INTO violations (inspection_id,seq,item_no,tier,is_repeat,cdi) VALUES (?,?,?,?,0,0)",
                            (iid, item, item, tier))


def test_cleaner_ranks_better_and_shrinkage_applies(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    # 30 establishments in one category so category ranking is used (MIN_CATEGORY_SIZE=25)
    for i in range(28):
        _seed(con, f"E{i}", "Full Service Restaurant", [("2026-06-01", [(30 + k, "core") for k in range(i % 4)])])
    _seed(con, "CLEAN", "Full Service Restaurant", [("2026-06-01", []), ("2026-01-01", [])])
    _seed(con, "DIRTY", "Full Service Restaurant",
          [("2026-06-01", [(k, "priority") for k in (8, 15, 18, 21, 22, 23)]), ("2026-01-01", [(k, "priority") for k in (8, 15, 22)])])
    con.commit()

    assert compute_scores(con, as_of=date(2026, 10, 22)) == 30
    for ws in WEIGHT_SETS:
        s = {r["record_id"]: r for r in con.execute("SELECT * FROM scores WHERE weight_set=?", (ws,))}
        assert s["CLEAN"]["score"] > s["DIRTY"]["score"], ws
        assert s["CLEAN"]["category_percentile"] == 0.0, ws
        # shrinkage: shrunk score sits between the raw value and the category mean
        assert s["DIRTY"]["raw_score"] < s["DIRTY"]["score"], ws
    s = {r["record_id"]: r for r in con.execute("SELECT * FROM scores WHERE weight_set='rf-heavy'")}
    # NB: DIRTY's raw score is ~72 but shrinkage (PRIOR_STRENGTH=2 vs effective_n ~1.4) lifts it to ~87.
    # Whether that is too forgiving is a calibration question (see `ffx calibrate --prior-strength`).
    assert s["CLEAN"]["letter"] == "A" and s["DIRTY"]["letter"] not in ("A", None)
    assert s["DIRTY"]["raw_score"] < 75
    assert con.execute("SELECT COUNT(*) FROM inspection_scores").fetchone()[0] == 32 * len(WEIGHT_SETS)


def test_compact_and_reparse_from_compressed(tmp_path):
    from pathlib import Path
    from ffxfood.db import compact_reports
    from ffxfood.parse_store import parse_pending
    text = (Path(__file__).parent / "fixtures" / "sample_report.txt").read_text(encoding="utf-8")
    con = connect(tmp_path / "t.sqlite")
    con.execute("INSERT INTO inspections (inspection_id, record_id) VALUES ('4238832','E1')")
    con.execute("INSERT INTO reports (inspection_id, fetch_status, raw_text) VALUES ('4238832','ok',?)", (text,))
    con.commit()
    assert compact_reports(con) == 1
    r = con.execute("SELECT raw_text, length(raw_text_z) z FROM reports").fetchone()
    assert r["raw_text"] is None and 0 < r["z"] < len(text) / 3
    assert parse_pending(con) == {"ok": 1, "mismatch": 0, "error": 0, "total": 1}
    assert con.execute("SELECT COUNT(*) FROM violations").fetchone()[0] == 4


def test_ingest_records_field_changes(tmp_path):
    from ffxfood.ingest import _record_changes
    con = connect(tmp_path / "t.sqlite")
    con.execute("INSERT INTO establishments (record_id,name,status,zip) VALUES ('E1','OLD NAME','Active','22030')")
    old = con.execute("SELECT * FROM establishments").fetchone()
    _record_changes(con, old, {"ESTABLISHMENT_NAME": "NEW NAME", "ESTABLISHMENT_STATUS": "Active", "PERMIT_ZIP": "22030"}, "t")
    assert [tuple(r) for r in con.execute("SELECT field, old_value, new_value FROM establishment_history")] == [("name", "OLD NAME", "NEW NAME")]
