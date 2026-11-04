from pathlib import Path

import pytest

from ffxfood.parse_report import parse_grid, parse_header, parse_report, parse_violations

TEXT = (Path(__file__).parent / "fixtures" / "sample_report.txt").read_text(encoding="utf-8")


def test_header():
    h = parse_header(TEXT)
    assert h["inspection_number"] == "4238832"
    assert h["record_number"] == "HFOOD-2023-00024"
    assert h["inspection_date"] == "2026-10-09"
    assert (h["time_in"], h["time_out"]) == ("15:09:00", "16:28:00")
    assert (h["rf_out"], h["rf_repeat_out"], h["grp_out"]) == (3, 1, 0)
    assert h["purpose"] == "Routine"
    assert h["establishment_type"] == "Caterer"
    assert h["priority_level"] == 2


def test_header_when_permit_holder_runs_into_next_column():
    old = "Genesis Delight Cater Llc                    REDACTED EHS"
    holder = "Genesis Delight Catering International Food Llc"[: old.index("REDACTED") - 1]
    long_holder = TEXT.replace(old, holder + " REDACTED EHS")     # one space, next column still in place
    h = parse_header(long_holder)
    assert (h["purpose"], h["establishment_type"], h["priority_level"]) == ("Routine", "Caterer", 2)


def test_grid_complete_and_correct():
    g = parse_grid(TEXT)
    assert sorted(g) == list(range(1, 57))           # all 56 items found, incl. wrapped rows (e.g. 47)
    assert {n for n, v in g.items() if v["status"] == "OUT"} == {8, 15, 28}
    assert g[1]["status"] == "IN"
    assert g[18]["status"] == "N/O"
    assert g[24]["status"] == "N/A"
    assert g[47]["status"] == "IN"                    # number printed on the line below its status


def test_violations():
    v = parse_violations(TEXT)
    assert [x["item_no"] for x in v] == [15, 8, 28, 28]
    assert [x["code"] for x in v] == ["3-302.11(A)(2)", "2-301.14(A)-(I)", "7-201.11", "7-201.11(B)"]
    assert all(x["tier"] == "priority" for x in v)
    assert [x["is_repeat"] for x in v] == [False, False, True, True]
    assert all(x["cdi"] for x in v)
    assert "raw chicken skewers stored above beef" in v[0]["observation"]
    assert v[0]["corrective_action"].startswith("Raw animal foods shall be sorted")
    assert "\n" not in v[0]["observation"]


def test_full_report_cross_check():
    p = parse_report(TEXT)
    assert p["counts_match"] is True
    assert p["status"] == "ok", p["notes"]
    assert "Store raw chicken below beef" in p["comments"]
    assert "SCHEDULING" not in p["comments"] and "Environmental Health Specialist" not in p["comments"]
    assert "#1 WIC" in p["temperature_text"]
    assert p["header"]["follow_up_required"] is False


def test_mismatch_is_flagged():
    broken = TEXT.replace("Risk Factors/Interventions Out of Compliance: 3", "Risk Factors/Interventions Out of Compliance: 5")
    p = parse_report(broken)
    assert p["counts_match"] is False
    assert p["status"] == "mismatch"


def _without_grp_grid(text: str) -> str:
    """Simulate the report variant that has no Good Retail Practices grid (items 30-56)."""
    i = text.index("GOOD RETAIL PRACTICES")
    j = text.index("Genesis Delight Catering (Inspection Date", i)
    return text[:i] + text[j:]


def test_variant_without_grp_grid_is_ok_when_header_says_zero():
    p = parse_report(_without_grp_grid(TEXT))
    assert sorted(p["items"]) == list(range(1, 30))
    assert p["status"] == "ok", p["notes"]
    assert "no Good Retail Practices grid" in p["notes"]


def test_missing_grp_grid_with_nonzero_header_is_flagged():
    variant = _without_grp_grid(TEXT).replace("Good Retail Practices Out of Compliance: 0", "Good Retail Practices Out of Compliance: 2")
    p = parse_report(variant)
    assert p["status"] == "mismatch"


def test_partial_grp_grid_is_flagged():
    # Drop one GRP row (item 41) -> grid is present but incomplete
    lines = [ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("41 ")]
    p = parse_report("\n".join(lines))
    assert p["status"] == "mismatch" and 41 not in p["items"]


def test_no_private_names_in_output():
    p = parse_report(TEXT)
    blob = repr(p)
    # person in charge, manager certification number, permit holder and inspector are never extracted
    assert "REDACTED" not in blob and "CFM:" not in blob and "Genesis Delight Cater Llc" not in blob


def test_item_flags_rolled_up_from_violations():
    g = parse_report(TEXT)["items"]
    assert g[15] == {"status": "OUT", "cdi": False, "n_violations": 1, "tier": "priority", "is_repeat": False, "all_cdi": True}
    assert g[28]["n_violations"] == 2 and g[28]["is_repeat"] is True and g[28]["all_cdi"] is True
    assert g[1]["n_violations"] == 0 and g[1]["tier"] is None


def test_mixed_tiers_take_most_severe():
    from ffxfood.parse_report import attach_item_flags
    grid = {5: {"status": "OUT", "cdi": False}}
    attach_item_flags(grid, [
        {"item_no": 5, "tier": "core", "is_repeat": False, "cdi": True},
        {"item_no": 5, "tier": "priority foundation", "is_repeat": True, "cdi": False},
    ])
    assert grid[5]["tier"] == "priority foundation"
    assert grid[5]["is_repeat"] is True and grid[5]["all_cdi"] is False


def test_citation_code_with_internal_space():
    text = TEXT.replace("2-301.14(A)-(I) - When to Wash", "2-201.11(A)(1)- (5) - Duty of PIC, Employee Health Policy in Place")
    v = parse_violations(text)
    assert v[1]["code"] == "2-201.11(A)(1)-(5)"
    assert v[1]["title"] == "Duty of PIC, Employee Health Policy in Place"


def test_out_item_without_violation_is_flagged():
    i = TEXT.index("\n 8\n 2-301.14")
    j = TEXT.index("\n 28\n", i)
    p = parse_report(TEXT[:i] + TEXT[j:])     # drop the item-8 citation; grid still says 8 is OUT
    assert p["status"] == "mismatch" and "OUT items without violations: [8]" in p["notes"]


def test_citation_variants_without_spaced_hyphen():
    text = TEXT.replace("3-302.11(A)(2) - Separation of Raw Animal Foods Different Types",
                        "3-203.12(C)(1)- Approved Record Keeping, Tags/Labels")
    text = text.replace("2-301.14(A)-(I) - When to Wash", "4-303.11(B) Availability of Chemical Sanitizer")
    text = text.replace("7-201.11 - ", "6-202-13(B)(1)-(2) - ", 1)          # county typo: hyphen for the dot
    v = parse_violations(text)
    assert [(x["code"], x["title"]) for x in v[:2]] == [
        ("3-203.12(C)(1)", "Approved Record Keeping, Tags/Labels"),
        ("4-303.11(B)", "Availability of Chemical Sanitizer"),
    ]
    assert v[2]["code"] == "6-202-13(B)(1)-(2)"
