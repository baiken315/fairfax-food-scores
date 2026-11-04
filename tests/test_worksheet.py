from ffxfood.db import connect
from ffxfood.scoring import compute_scores
from ffxfood.worksheet import worksheet, worksheet_html
from test_scoring import _seed


def _db(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    for i in range(28):
        _seed(con, f"E{i}", "Full Service Restaurant", [("2026-06-01", [(30 + k, "core") for k in range(i % 4)])])
    _seed(con, "DIRTY", "Full Service Restaurant",
          [("2026-06-01", [(8, "priority"), (22, "core")]), ("2026-01-01", [(8, "priority")])])
    con.execute("UPDATE report_items SET is_repeat=1 WHERE item_no=8 AND status='OUT'")
    con.execute("UPDATE report_summary SET purpose='Follow-up' WHERE inspection_id='E0-0'")
    con.commit()
    compute_scores(con)   # default as_of = today, same date stored in computed_at (what worksheet() reads back)
    return con


def test_worksheet_recomputation_matches_stored_rows(tmp_path):
    con = _db(tmp_path)
    w = worksheet(con, "DIRTY")
    assert all(w["rollup"]["checks"].values()), w["rollup"]
    stored = con.execute("SELECT * FROM scores WHERE record_id='DIRTY' AND weight_set='rf-heavy'").fetchone()
    assert round(w["rollup"]["shrunk"], 2) == stored["score"] and w["rollup"]["letter"] == stored["letter"]
    newest = w["inspections"][0]
    assert newest["date"] == "2026-06-01"
    by_no = {i["item_no"]: i for i in newest["items"]}
    assert by_no[8]["tier"] == "priority" and by_no[8]["repeat_mult"] == 2.0   # 4 x 1.5 x 2 = 12
    assert by_no[22]["points"] == 4.0
    assert by_no[8]["points"] == 12.0 and abs(newest["deduction"] - 16.0) < 1e-9
    assert abs(sum(i["points"] for i in newest["items"]) - newest["deduction"]) < 1e-9
    assert round(newest["score"], 2) == newest["stored_score"]


def test_non_routine_inspections_listed_as_excluded(tmp_path):
    con = _db(tmp_path)
    w = worksheet(con, "E0")
    assert w["inspections"] == [] and w["rollup"] is None and w["stored"] is None
    assert len(w["excluded"]) == 1 and "Follow-up" in w["excluded"][0]["reason"]


def test_html_escapes_and_shows_the_crosschecks(tmp_path):
    con = _db(tmp_path)
    con.execute("UPDATE violations SET observation='saw <b>mold</b> <script>alert(1)</script>' WHERE item_no=8")
    con.commit()
    html = worksheet_html(con, "DIRTY")
    assert "saw &lt;b&gt;mold&lt;/b&gt; &lt;script&gt;" in html and "<script>alert" not in html
    assert "×2" in html and "✓ Every number above matches" in html
    assert worksheet_html(con, "NOSUCH") is None
