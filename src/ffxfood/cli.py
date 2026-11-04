"""ffx: mirror, parse and grade Fairfax County retail food inspection reports."""
from __future__ import annotations

import argparse
import csv
import json
import sys

from . import config
from .calibrate import calibrate, format_report
from .complaints import match_complaints, scrape_complaints
from .db import compact_reports, connect
from .fetch_reports import fetch_pending
from .ingest import ingest_establishments, ingest_inspections
from .parse_report import parse_report
from .parse_store import parse_pending
from .profile import location_profile
from .publish import publish
from .pdftext import pdf_file_to_text
from .scoring import DEFAULT_WEIGHT_SET, WEIGHT_SETS, compute_scores
from .worksheet import worksheet_html


def cmd_ingest(a, con):
    print("establishments:", ingest_establishments(con, all_statuses=a.all_statuses))
    print("inspections:   ", ingest_inspections(con))


def cmd_fetch(a, con):
    print(fetch_pending(con, limit=a.limit, since=a.since, delay=a.delay, keep_pdfs=a.keep_pdfs,
                        retry_errors=a.retry_errors, random_order=a.random,
                        oldest_first=a.oldest_first))


def cmd_parse(a, con):
    print(parse_pending(con, reparse=a.reparse))


def cmd_parse_file(a, _con):
    """Debug a single PDF or pre-extracted text file without touching the DB."""
    text = open(a.path, encoding="utf-8").read() if a.path.endswith(".txt") else pdf_file_to_text(a.path)[0]
    p = parse_report(text)
    p["items"] = {str(k): v for k, v in p["items"].items()}
    print(json.dumps(p, indent=2, default=str))


def cmd_score(a, con):
    print("scored establishments:", compute_scores(con, weight_sets=a.weight_set or None))


def cmd_stats(a, con):
    q = lambda sql: con.execute(sql).fetchall()
    print("establishments:", q("SELECT COUNT(*) c FROM establishments")[0]["c"])
    print("inspections:   ", q("SELECT COUNT(*) c FROM inspections")[0]["c"])
    print("fetch status:  ", {r["fetch_status"]: r["c"] for r in q("SELECT fetch_status, COUNT(*) c FROM reports GROUP BY 1")})
    print("parse status:  ", {r["parse_status"]: r["c"] for r in q("SELECT parse_status, COUNT(*) c FROM reports GROUP BY 1")})
    print("scored:        ", {r["weight_set"]: r["c"] for r in q("SELECT weight_set, COUNT(*) c FROM scores GROUP BY 1")})
    print("parse notes:   ", {r["n"]: r["c"] for r in q(
        "SELECT COALESCE(parse_notes,'-') n, COUNT(*) c FROM reports WHERE parse_status!='pending' GROUP BY 1 ORDER BY 2 DESC LIMIT 8")})


def cmd_profile(a, con):
    p = location_profile(con, a.record_id, weight_set=a.weight_set)
    if p is None:
        sys.exit(f"no establishment {a.record_id}")
    print(json.dumps(p, indent=2, default=str))


def cmd_worksheet(a, con):
    h = worksheet_html(con, a.record_id, weight_set=a.weight_set)
    if h is None:
        sys.exit(f"no establishment {a.record_id}")
    out = a.out or f"worksheet_{a.record_id}.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(h)
    print(out)


def cmd_export(a, con):
    if a.per_location:
        rids = [r["record_id"] for r in con.execute("SELECT record_id FROM establishments ORDER BY record_id")]
        out = open(a.out, "w", encoding="utf-8") if a.out else sys.stdout
        for rid in rids:                       # JSON Lines: one complete location record per line
            out.write(json.dumps(location_profile(con, rid, weight_set=a.weight_set), default=str) + "\n")
        return
    rows = con.execute(
        """SELECT e.record_id, e.name, e.address, e.city, e.zip, e.category, e.status, e.stamp, e.lon, e.lat,
                  s.letter, s.score, s.category_percentile, s.n_inspections, s.effective_n,
                  s.weight_set, s.model_version
           FROM scores s JOIN establishments e USING(record_id) WHERE s.weight_set=? ORDER BY s.score DESC""",
        (a.weight_set,),
    ).fetchall()
    out = open(a.out, "w", newline="", encoding="utf-8") if a.out else sys.stdout
    if a.format == "json":
        json.dump([dict(r) for r in rows], out, indent=2)
    else:
        w = csv.writer(out)
        if rows:
            w.writerow(rows[0].keys())
        w.writerows([tuple(r) for r in rows])


def cmd_complaints(a, con):
    if a.action == "scrape":
        years = None
        if a.years:
            lo, _, hi = a.years.partition("-")
            years = list(range(int(lo), int(hi or lo) + 1))
        print(scrape_complaints(con, years=years, delay=a.delay, max_new=a.limit))
    elif a.action == "match":
        print(match_complaints(con))
    elif a.action == "review":
        rows = con.execute(
            """SELECT m.record_number, c.submitted_date, c.project_name, c.work_location, m.record_id,
                      e.name, e.address, m.method, m.confidence, m.name_similarity
               FROM complaint_matches m JOIN complaints c USING(record_number)
               LEFT JOIN establishments e USING(record_id)
               WHERE m.reviewed=0 AND m.confidence < ? ORDER BY m.record_number, m.name_similarity DESC""",
            (a.below,),
        ).fetchall()
        for r in rows:
            print(f"{r['record_number']}  {r['submitted_date']}  {r['project_name']!r} @ {r['work_location']}\n"
                  f"    -> {r['record_id']} {r['name']!r} @ {r['address']}  [{r['method']} conf={r['confidence']} "
                  f"name_sim={r['name_similarity']}]")
        print(f"{len(rows)} candidate matches below confidence {a.below}. "
              "Confirm one with: ffx complaints confirm HCOMP-... HFOOD-...")
    elif a.action == "confirm":
        rec, rid = a.args
        with con:
            con.execute("DELETE FROM complaint_matches WHERE record_number=?", (rec,))
            con.execute("INSERT INTO complaint_matches (record_number, record_id, method, confidence, reviewed) "
                        "VALUES (?,?,'manual',1.0,1)", (rec, rid))
        print(f"{rec} -> {rid} (manual)")
    elif a.action == "stats":
        q = lambda sql: con.execute(sql).fetchall()
        print("complaints:    ", q("SELECT COUNT(*) c FROM complaints")[0]["c"])
        print("detail status: ", {r["detail_status"]: r["c"] for r in q("SELECT detail_status, COUNT(*) c FROM complaints GROUP BY 1")})
        print("by year:       ", {r["y"]: r["c"] for r in q("SELECT substr(submitted_date,1,4) y, COUNT(*) c FROM complaints GROUP BY 1")})
        print("match methods: ", {r["method"]: r["c"] for r in q("SELECT method, COUNT(DISTINCT record_number) c FROM complaint_matches GROUP BY 1")})
        print("unmatched:     ", q("SELECT COUNT(*) c FROM complaints WHERE detail_status='ok' AND record_number NOT IN (SELECT record_number FROM complaint_matches)")[0]["c"])


def cmd_publish(a, con):
    print(publish(con, a.out, weight_set=a.weight_set))


def cmd_compact(a, con):
    print("reports compressed:", compact_reports(con))
    if a.vacuum:
        con.execute("VACUUM")
        print("vacuumed")


def cmd_calibrate(a, con):
    ps = tuple(float(x) for x in a.prior_strengths.split(","))
    text = format_report(calibrate(con, weight_sets=a.weight_set or None, prior_strengths=ps))
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)


def cmd_run(a, con):
    cmd_ingest(a, con)
    cmd_fetch(a, con)
    cmd_parse(a, con)
    a.weight_set = None
    cmd_score(a, con)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ffx", description=__doc__)
    ap.add_argument("--db", default=None, help=f"SQLite path (default {config.DB_PATH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("ingest", cmd_ingest, "Mirror establishments + inspection index from the county FeatureServer")
    sp.add_argument("--all-statuses", action="store_true", help="Include permits the county viewer hides")

    sp = add("fetch", cmd_fetch, "Download report PDFs and extract text (resumable, newest first)")
    sp.add_argument("--limit", type=int, help="Max reports this run")
    sp.add_argument("--since", help="Only inspections on/after YYYY-MM-DD")
    sp.add_argument("--delay", type=float, default=1.5, help="Seconds between requests (default 1.5)")
    sp.add_argument("--keep-pdfs", action="store_true", help="Also save the PDFs under data/pdfs/")
    sp.add_argument("--retry-errors", action="store_true")
    sp.add_argument("--random", action="store_true", help="Random order instead of newest-first (parser QA)")
    sp.add_argument("--oldest-first", action="store_true", help="Oldest first (rescue reports about to expire)")

    sp = add("parse", cmd_parse, "Parse fetched report text into structured tables")
    sp.add_argument("--reparse", action="store_true", help="Re-parse everything (after parser changes)")

    sp = add("parse-file", cmd_parse_file, "Parse one local PDF/.txt and print JSON (no DB writes)")
    sp.add_argument("path")

    sp = add("score", cmd_score, "Compute inspection and establishment scores under each weight set")
    sp.add_argument("--weight-set", action="append", choices=sorted(WEIGHT_SETS), help="Only these (repeatable)")
    add("stats", cmd_stats, "Pipeline progress")

    sp = add("export", cmd_export, "Export scored establishments")
    sp.add_argument("--format", choices=["csv", "json"], default="csv")
    sp.add_argument("--weight-set", default=DEFAULT_WEIGHT_SET, choices=sorted(WEIGHT_SETS))
    sp.add_argument("--per-location", action="store_true", help="Nested per-location records as JSON Lines")

    sp = add("profile", cmd_profile, "Print the full record for one location (HFOOD-...) as JSON")
    sp.add_argument("record_id")
    sp.add_argument("--weight-set", default=DEFAULT_WEIGHT_SET, choices=sorted(WEIGHT_SETS))
    sp.add_argument("--out")

    sp = add("worksheet", cmd_worksheet, "HTML sheet showing every step from grid items to one location's letter")
    sp.add_argument("record_id")
    sp.add_argument("--weight-set", default=DEFAULT_WEIGHT_SET, choices=sorted(WEIGHT_SETS))
    sp.add_argument("--out", help="HTML path (default worksheet_<record_id>.html)")

    sp = add("calibrate", cmd_calibrate, "Compare weight sets against next-inspection, follow-up and complaint outcomes")
    sp.add_argument("--weight-set", action="append", choices=sorted(WEIGHT_SETS), help="Only these (repeatable)")
    sp.add_argument("--prior-strengths", default="0,1,2,4", help="Shrinkage strengths to compare (default 0,1,2,4)")
    sp.add_argument("--out", help="Also write the markdown report here")

    sp = add("publish", cmd_publish, "Build the static site (web/ + JSON data) into --out")
    sp.add_argument("--out", default="dist")
    sp.add_argument("--weight-set", default=DEFAULT_WEIGHT_SET, choices=sorted(WEIGHT_SETS))

    sp = add("compact", cmd_compact, "Compress legacy uncompressed report text (raw_text -> raw_text_z)")
    sp.add_argument("--vacuum", action="store_true", help="Also VACUUM to return freed space to disk")

    sp = add("complaints", cmd_complaints, "Health complaints (HCOMP) from the PLUS portal")
    sp.add_argument("action", choices=["scrape", "match", "review", "confirm", "stats"])
    sp.add_argument("args", nargs="*", help="confirm: HCOMP-... HFOOD-...")
    sp.add_argument("--years", help="scrape: e.g. 2026 or 2023-2026 (default: 2023 to this year)")
    sp.add_argument("--delay", type=float, default=2.0, help="scrape: seconds between requests (default 2)")
    sp.add_argument("--limit", type=int, help="scrape: max detail pages to fetch this run")
    sp.add_argument("--below", type=float, default=0.75, help="review: show matches below this confidence")

    sp = add("run", cmd_run, "ingest -> fetch -> parse -> score")
    sp.add_argument("--all-statuses", action="store_true")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--since")
    sp.add_argument("--delay", type=float, default=1.5)
    sp.add_argument("--keep-pdfs", action="store_true")
    sp.add_argument("--retry-errors", action="store_true")
    sp.add_argument("--random", action="store_true")
    sp.add_argument("--oldest-first", action="store_true")
    sp.add_argument("--reparse", action="store_true")

    a = ap.parse_args(argv)
    con = None if a.cmd == "parse-file" else connect(a.db)
    a.fn(a, con)


if __name__ == "__main__":
    main()
