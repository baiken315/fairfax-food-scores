import json

from ffxfood.db import connect
from ffxfood.publish import ITEM_NAMES, publish
from ffxfood.scoring import compute_scores
from test_scoring import _seed


def test_publish_writes_site_and_public_data_only(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    _seed(con, "E1", "Fast Food Restaurant", [("2026-06-01", [(8, "priority")])])
    con.execute("UPDATE establishments SET mar_address='1 MAIN ST, FAIRFAX, VA, 22030'")
    con.commit()
    compute_scores(con)
    out = tmp_path / "dist"
    res = publish(con, out)
    assert res["locations"] == 1
    for f in ("index.html", "app.js", "method.html", "data/meta.json", "data/locations.json"):
        assert (out / f).exists(), f
    loc = json.loads((out / "data/loc/E1.json").read_text())
    assert "mar_address" not in loc["establishment"]
    insp = loc["inspections"][0]
    assert "inspector" not in insp and not (out / "data/inspectors.json").exists()
    assert insp["out_items"][0]["name"] == ITEM_NAMES[8] and insp["out_items"][0]["tier"] == "priority"
    meta = json.loads((out / "data/meta.json").read_text())
    assert [l["letter"] for l in meta["letters"]] == ["A", "B", "C", "D", "F"]
    assert len(ITEM_NAMES) == 56


def test_inspector_names_and_county_emails_are_redacted(tmp_path):
    from pathlib import Path
    from ffxfood.db import pack_text
    text = (Path(__file__).parent / "fixtures" / "sample_report.txt").read_text(encoding="utf-8")
    con = connect(tmp_path / "t.sqlite")
    _seed(con, "E1", "Fast Food Restaurant", [("2026-06-01", [(8, "priority")])])
    con.execute("UPDATE reports SET raw_text_z=?", (pack_text(text),))        # header EHS: "REDACTED EHS"
    con.execute("UPDATE violations SET observation='Seen by redacted  ehs today.', "
                "corrective_action='Email Pat.Doe@FairfaxCounty.gov with proof.'")
    con.execute("UPDATE report_summary SET comments='INSPECTION CONDUCTED BY REDACTED EHS'")
    con.commit()
    compute_scores(con)
    publish(con, tmp_path / "dist")
    blob = (tmp_path / "dist/data/loc/E1.json").read_text()
    v = json.loads(blob)["inspections"][0]["out_items"][0]["violations"][0]
    assert v["observation"] == "Seen by [inspector] today."
    assert v["corrective_action"] == "Email [county email] with proof."
    assert "REDACTED" not in blob.upper() and "comments" not in blob
