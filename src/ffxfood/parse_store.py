"""Parse stored report text and write structured rows."""
from __future__ import annotations

import sqlite3

from .db import now_iso, report_text
from .parse_report import PARSER_VERSION, parse_report


def parse_pending(con: sqlite3.Connection, *, reparse: bool = False) -> dict[str, int]:
    where = "fetch_status='ok' AND (raw_text_z IS NOT NULL OR raw_text IS NOT NULL)"
    if not reparse:
        where += " AND (parse_status='pending' OR parser_version IS NOT ?)"
    rows = con.execute(
        f"SELECT inspection_id, raw_text, raw_text_z FROM reports WHERE {where}",
        () if reparse else (PARSER_VERSION,),
    ).fetchall()

    stats = {"ok": 0, "mismatch": 0, "error": 0, "total": len(rows)}
    for row in rows:
        iid = row["inspection_id"]
        try:
            parsed = parse_report(report_text(row))
            _write(con, iid, parsed)
            stats[parsed["status"]] += 1
        except Exception as e:
            with con:
                con.execute(
                    "UPDATE reports SET parse_status='error', parse_notes=?, parsed_at=?, parser_version=? "
                    "WHERE inspection_id=?",
                    (str(e)[:500], now_iso(), PARSER_VERSION, iid),
                )
            stats["error"] += 1
    return stats


def _write(con: sqlite3.Connection, iid: str, p: dict) -> None:
    h = p["header"]
    with con:
        for t in ("report_summary", "report_items", "violations"):
            con.execute(f"DELETE FROM {t} WHERE inspection_id=?", (iid,))
        con.execute(
            """INSERT INTO report_summary
               (inspection_id,inspection_date,time_in,time_out,purpose,establishment_type,priority_level,
                rf_out,rf_repeat_out,grp_out,counts_match,follow_up_required,temperature_text,comments)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                iid, h["inspection_date"], h["time_in"], h["time_out"], h["purpose"],
                h["establishment_type"], h["priority_level"], h["rf_out"], h["rf_repeat_out"],
                h["grp_out"], None if p["counts_match"] is None else int(p["counts_match"]),
                None if h["follow_up_required"] is None else int(h["follow_up_required"]),
                p["temperature_text"], p["comments"],
            ),
        )
        con.executemany(
            "INSERT INTO report_items (inspection_id,item_no,section,status,cdi,tier,is_repeat,all_cdi,n_violations) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (iid, n, "risk_factor" if n <= 29 else "good_retail_practice", v["status"], int(v["cdi"]),
                 v["tier"], _opt_int(v["is_repeat"]), _opt_int(v["all_cdi"]), v["n_violations"])
                for n, v in sorted(p["items"].items())
            ],
        )
        con.executemany(
            """INSERT INTO violations
               (inspection_id,seq,item_no,code,title,tier,is_repeat,cdi,observation,corrective_action)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (iid, v["seq"], v["item_no"], v["code"], v["title"], v["tier"],
                 int(v["is_repeat"]), int(v["cdi"]), v["observation"], v["corrective_action"])
                for v in p["violations"]
            ],
        )
        con.execute(
            "UPDATE reports SET parse_status=?, parse_notes=?, parsed_at=?, parser_version=? WHERE inspection_id=?",
            (p["status"], p["notes"], now_iso(), p["parser_version"], iid),
        )


def _opt_int(b: bool | None) -> int | None:
    return None if b is None else int(b)
