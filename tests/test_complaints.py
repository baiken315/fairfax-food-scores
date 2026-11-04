from pathlib import Path

from ffxfood.complaints import (
    form_fields, match_complaints, name_similarity, norm_address, parse_detail, parse_search_rows,
)
from ffxfood.db import connect

FX = Path(__file__).parent / "fixtures"


def test_search_rows():
    rows = parse_search_rows((FX / "hcomp_search.html").read_text(encoding="utf-8"))
    assert [r["record_number"] for r in rows] == ["HCOMP-2026-00712", "HCOMP-2026-00711", "HCOMP-2026-00710"]
    r = rows[0]
    assert r["submitted_date"] == "2026-10-22"
    assert r["record_type"] == "EH Complaint"
    assert r["project_name"] == "CAFE CHINA"
    assert r["short_notes"] == "CPP Area-B3,OSW Area-1"
    assert r["event_target"] == "ctl00$PlaceHolderMain$CapView$gdvPermitList$ctl02$lnkPermitNumber"


def test_form_fields_for_postback():
    f = form_fields((FX / "hcomp_search.html").read_text(encoding="utf-8"))
    assert f["__VIEWSTATE"] == "VIEWSTATE-PLACEHOLDER"


def test_detail():
    d = parse_detail((FX / "hcomp_detail.html").read_text(encoding="utf-8"))
    assert d == {"work_location": "2405 CENTREVILLE RD HERNDON, VA 20171", "address_gis_id": "100241211",
                 "description": "DPSC- STARBUCKS COFFEE #7631", "status": "Closed"}


def test_normalizers():
    assert norm_address("234 Maple Avenue East, Vienna, VA 22180") == "234 MAPLE AVE E VIENNA VA 22180"
    assert name_similarity("DPSC- STARBUCKS COFFEE #7631", "STARBUCKS COFFEE #7605") >= 0.9
    assert name_similarity("SUSHI OISHII", "TACO BELL") < 0.5


def _est(con, rid, name, address, city, z):
    con.execute(
        "INSERT INTO establishments (record_id,name,address,city,zip,mar_address,status) VALUES (?,?,?,?,?,?,?)",
        (rid, name, address, city, z, f"{address}, {city}, VA, {z}", "Active"),
    )


def _complaint(con, rec, loc, name):
    con.execute(
        "INSERT INTO complaints (record_number,work_location,project_name,detail_status) VALUES (?,?,?,'ok')",
        (rec, loc, name),
    )


def test_matching(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    _est(con, "E1", "SUSHI OISHII", "234 MAPLE AVE E", "VIENNA", "22180")
    _est(con, "E2", "PIZZA PLACE", "100 MALL RD", "FAIRFAX", "22030")      # food court: two at one address
    _est(con, "E3", "NOODLE BAR", "100 MALL RD", "FAIRFAX", "22030")
    _est(con, "E4", "BAGEL SHOP", "9 SIDE ST", "RESTON", "20190")
    _est(con, "E5", "CINEMA 14", "5 PALACE WAY", "FAIRFAX", "22030")         # exact name beats "contains"
    _est(con, "E6", "CINEMA 14 CRAVE", "5 PALACE WAY", "FAIRFAX", "22030")
    _complaint(con, "C1", "234 MAPLE AVE E VIENNA, VA 22180", "SUSHI OISHII")           # address + name
    _complaint(con, "C2", "100 MALL RD FAIRFAX, VA 22030", "NOODLE BAR #2")              # resolved by name
    _complaint(con, "C3", "100 MALL RD FAIRFAX, VA 22030", "SOMETHING ELSE")             # ambiguous
    _complaint(con, "C4", "12 OTHER WAY RESTON, VA 20190", "BAGEL SHOP")                 # name within zip
    _complaint(con, "C6", "5 PALACE WAY FAIRFAX, VA 22030", "Cinema 14")
    _complaint(con, "C5", "1 HOTEL DR HERNDON, VA 20171", "SPRINGHILL SUITES")           # unmatched
    con.commit()
    st = match_complaints(con)
    m = {(r["record_number"], r["record_id"]): r["method"] for r in con.execute("SELECT * FROM complaint_matches")}
    assert m[("C1", "E1")] == "address+name"
    assert m[("C2", "E3")] == "address+name" and ("C2", "E2") not in m
    assert m[("C3", "E2")] == m[("C3", "E3")] == "address_ambiguous"
    assert m[("C4", "E4")] == "name_zip"
    assert m[("C6", "E5")] == "address+name" and ("C6", "E6") not in m
    assert st["unmatched"] == 1

    # a reviewed (manual) match survives re-matching
    con.execute("UPDATE complaint_matches SET reviewed=1, method='manual' WHERE record_number='C3' AND record_id='E2'")
    con.execute("DELETE FROM complaint_matches WHERE record_number='C3' AND record_id='E3'")
    con.commit()
    match_complaints(con)
    assert [tuple(r) for r in con.execute("SELECT record_id, method FROM complaint_matches WHERE record_number='C3'")] == [("E2", "manual")]
