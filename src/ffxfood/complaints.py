"""Health complaints (HCOMP records) from the county's PLUS portal, matched to establishments.

What the portal exposes publicly (checked Oct 2026):
  * Global search results (plain GET): submitted date, record number, type, inspection areas
    ("short notes"), project name (usually the business), status.
  * Record detail (reached by an ASP.NET postback from the results page, which plain `requests` can
    replay; the resulting CapDetail URL is stable): Work Location address in the county's Master
    Address Repository form plus its ADDRESS GIS ID, and the application description.
  * NOT public: complaint category, outcome, substantiation, link to the HFOOD permit.
    So a complaint is an allegation, and HCOMP covers all Environmental Health complaints, not
    only food (hotels, pools, ...): unmatched records are expected.

Broad searches are capped ("100+"), so we query 4-digit prefixes (HCOMP-2026-0050 -> 00500..00509),
which always fit on one 10-row results page and need no pagination postbacks.
"""
from __future__ import annotations

import html as htmllib
import re
import sqlite3
import time
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import urljoin

import requests

from . import config
from .db import now_iso

SEARCH_URL = config.PLUS_BASE + "/Cap/GlobalSearchResults.aspx?QueryText={q}"
FIRST_YEAR = 2023                    # earliest HCOMP year seen on the portal
_ROW_PREFIX = "ctl00_PlaceHolderMain_CapView_gdvPermitList_"
_ROW_FIELDS = {                       # span/link id suffix -> column
    "lblUpdatedTime": "submitted_date", "lnkPermitNumber": "record_number", "lblType": "record_type",
    "lblDescription": "short_notes", "lblProjectName": "project_name", "lblCapClass": "status",
}


# ----------------------------------------------------------------------------- HTML parsing

def _text(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_search_rows(page: str) -> list[dict]:
    """Rows of a GlobalSearchResults page: list fields plus the postback target of the record link."""
    rows: dict[str, dict] = {}
    for m in re.finditer(rf'id="{_ROW_PREFIX}(ctl\d+)_(\w+)"[^>]*>(.*?)</(?:span|a)>', page, re.S):
        row, field, val = m.groups()
        if field in _ROW_FIELDS:
            rows.setdefault(row, {})[_ROW_FIELDS[field]] = _text(val)
    out = []
    for row, r in rows.items():
        if not r.get("record_number", "").startswith("HCOMP-"):
            continue
        if r.get("submitted_date"):
            r["submitted_date"] = datetime.strptime(r["submitted_date"], "%m/%d/%Y").date().isoformat()
        r["event_target"] = f"ctl00$PlaceHolderMain$CapView$gdvPermitList${row}$lnkPermitNumber"
        out.append(r)
    return out


def form_fields(page: str) -> dict[str, str]:
    """All non-button <input> name/value pairs (viewstate etc.) for replaying a postback."""
    form: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", page):
        n = re.search(r'\bname="([^"]*)"', tag)
        t = re.search(r'\btype="([^"]*)"', tag)
        if not n or (t and t.group(1).lower() in ("submit", "button", "image", "checkbox", "radio")):
            continue
        v = re.search(r'\bvalue="([^"]*)"', tag)
        form[htmllib.unescape(n.group(1))] = htmllib.unescape(v.group(1)) if v else ""
    return form


def parse_detail(page: str) -> dict:
    """Work Location + description from a CapDetail page."""
    d: dict = {"work_location": None, "address_gis_id": None, "description": None, "status": None}
    m = re.search(r"id='tbl_worklocation'.*?<td class='NotBreakWord'>(.*?)</td>", page, re.S)
    if m:
        cell = m.group(1)
        first = re.split(r"<br\s*/?>", cell, maxsplit=1)[0]
        d["work_location"] = _text(re.sub(r"<span[^>]*>\s*\*\s*</span>", "", first, flags=re.I)) or None
        g = re.search(r"ADDRESS GIS ID:\s*(\d+)", cell)
        d["address_gis_id"] = g.group(1) if g else None
    m = re.search(r"Application Description:.*?<td>(.*?)</td>", page, re.S)
    if m:
        d["description"] = _text(m.group(1)) or None
    m = re.search(r'id="ctl00_PlaceHolderMain_lblRecordStatus">([^<]*)<', page)
    if m:
        d["status"] = _text(m.group(1)) or None
    return d


# ----------------------------------------------------------------------------- scraping

def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    return s


def _open_detail(s: requests.Session, search_url: str, search_page: str, target: str) -> requests.Response:
    form = form_fields(search_page)
    form["__EVENTTARGET"], form["__EVENTARGUMENT"] = target, ""
    action = re.search(r'<form[^>]*\baction="([^"]*)"', search_page)
    post_url = urljoin(search_url, htmllib.unescape(action.group(1))) if action else search_url
    return s.post(post_url, data=form, timeout=60, headers={"Referer": search_url})


def scrape_complaints(
    con: sqlite3.Connection,
    *,
    years: list[int] | None = None,
    delay: float = 2.0,
    max_new: int | None = None,
    max_consecutive_errors: int = 8,
    empty_chunks_to_stop: int = 3,
) -> dict[str, int]:
    """Rescan the HCOMP list pages (cheap: updates status of known records) and fetch the detail
    page for records not seen before. Resumable: details are only fetched where detail_status != 'ok'."""
    years = years or list(range(FIRST_YEAR, date.today().year + 1))
    s = _session()
    stats = {"list_pages": 0, "seen": 0, "new": 0, "details_ok": 0, "details_error": 0}
    consecutive_errors, n_new = 0, 0
    known = {r["record_number"]: r["detail_status"] for r in con.execute("SELECT record_number, detail_status FROM complaints")}

    for year in years:
        chunk, empty = 0, 0
        while empty < empty_chunks_to_stop:
            q = f"HCOMP-{year}-{chunk:04d}"
            url = SEARCH_URL.format(q=q)
            chunk += 1
            try:
                r = s.get(url, timeout=60)
                r.raise_for_status()
                page = r.text
                consecutive_errors = 0
            except Exception as e:
                print(f"  list {q}: {e}")
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    print("stopping: too many consecutive errors; re-run later")
                    return stats
                time.sleep(delay * 3)
                continue
            stats["list_pages"] += 1
            rows = parse_search_rows(page)
            empty = 0 if rows else empty + 1
            ts = now_iso()
            with con:
                for row in rows:
                    stats["seen"] += 1
                    con.execute(
                        """INSERT INTO complaints (record_number, submitted_date, record_type, status, short_notes,
                                                   project_name, first_seen_at, last_seen_at)
                           VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT(record_number) DO UPDATE SET
                             submitted_date=excluded.submitted_date, record_type=excluded.record_type,
                             status=excluded.status, short_notes=excluded.short_notes,
                             project_name=excluded.project_name, last_seen_at=excluded.last_seen_at""",
                        (row["record_number"], row.get("submitted_date"), row.get("record_type"), row.get("status"),
                         row.get("short_notes"), row.get("project_name"), ts, ts),
                    )
            time.sleep(delay)

            for row in rows:
                rec = row["record_number"]
                if known.get(rec) == "ok":
                    continue
                if max_new is not None and n_new >= max_new:
                    continue
                n_new += 1
                stats["new"] += rec not in known
                try:
                    d = _open_detail(s, url, page, row["event_target"])
                    d.raise_for_status()
                    if "CapDetail.aspx" not in d.url:
                        raise RuntimeError(f"postback landed on {d.url}")
                    det = parse_detail(d.text)
                    if not det["work_location"] and not det["description"]:
                        raise RuntimeError("detail page had no Work Location or description")
                    with con:
                        con.execute(
                            """UPDATE complaints SET work_location=?, address_gis_id=?, description=?, detail_url=?,
                                   status=COALESCE(?, status), detail_status='ok', detail_fetched_at=?, error=NULL
                               WHERE record_number=?""",
                            (det["work_location"], det["address_gis_id"], det["description"], d.url,
                             det["status"], now_iso(), rec),
                        )
                    known[rec] = "ok"
                    stats["details_ok"] += 1
                    consecutive_errors = 0
                    print(f"  {rec}: {det['work_location']}  [{row.get('project_name')}]")
                except Exception as e:
                    with con:
                        con.execute("UPDATE complaints SET detail_status='error', error=? WHERE record_number=?",
                                    (str(e)[:500], rec))
                    known[rec] = "error"
                    stats["details_error"] += 1
                    consecutive_errors += 1
                    print(f"  {rec}: ERROR {e}")
                    if consecutive_errors >= max_consecutive_errors:
                        print("stopping: too many consecutive errors; re-run later")
                        return stats
                time.sleep(delay)
    return stats


# ----------------------------------------------------------------------------- matching

_SUFFIXES = {
    "STREET": "ST", "ROAD": "RD", "AVENUE": "AVE", "DRIVE": "DR", "BOULEVARD": "BLVD", "LANE": "LN",
    "COURT": "CT", "PLACE": "PL", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TURNPIKE": "TPKE", "CIRCLE": "CIR",
    "TERRACE": "TER", "SQUARE": "SQ", "PLAZA": "PLZ", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
_NAME_NOISE = {"THE", "INC", "LLC", "CO", "CORP", "RESTAURANT", "CAFE", "AND", "OF"}


def norm_address(s: str | None) -> str:
    """Uppercase, punctuation to spaces, common suffixes abbreviated."""
    if not s:
        return ""
    toks = re.sub(r"[^A-Z0-9 ]", " ", s.upper()).split()
    return " ".join(_SUFFIXES.get(t, t) for t in toks)


def _house_zip(addr: str) -> tuple[str, str] | None:
    """(house number, zip) of a normalized one-line address; the cheap blocking key for matching."""
    h = re.match(r"(\d+)", addr)
    z = re.search(r"\b(\d{5})(?:\s+\d{4})?$", addr)
    return (h.group(1), z.group(1)) if h and z else None


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"#\s*\d+", " ", s.upper())                  # store numbers
    s = re.sub(r"^[A-Z]{2,6}\s*-\s+", "", s)                  # agency prefixes like "DPSC- "
    toks = [t for t in re.sub(r"[^A-Z0-9 ]", " ", s).split() if t not in _NAME_NOISE]
    return " ".join(toks)


def name_similarity(a: str | None, b: str | None) -> float:
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    if ta <= tb or tb <= ta:                                  # one name contained in the other
        return max(0.9, SequenceMatcher(None, a, b).ratio())
    return max(SequenceMatcher(None, a, b).ratio(),
               SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio())


def _est_address_keys(e: sqlite3.Row) -> list[tuple[str, str]]:
    """(normalized street, zip) forms an establishment may appear under in a complaint."""
    keys = []
    if e["mar_address"]:                                      # "STREET, CITY, VA, ZIP"
        parts = [p.strip() for p in e["mar_address"].split(",")]
        if len(parts) >= 4:
            keys.append((norm_address(parts[0]), parts[-1][:5]))
    if e["address"] and e["zip"]:
        keys.append((norm_address(e["address"]), str(e["zip"])[:5]))
    return [k for k in dict.fromkeys(keys) if k[0]]


def match_complaints(con: sqlite3.Connection, *, name_threshold: float = 0.6) -> dict[str, int]:
    """(Re)compute complaint -> establishment matches. Reviewed (manual) matches are never touched.

    1. Address: the complaint's Work Location starts with an establishment's street and has its ZIP.
       - one establishment at the address: match (confidence 0.9, or 0.7 if the names disagree)
       - several (food courts, malls, hotels): best name if it clears name_threshold by a margin,
         otherwise keep all candidates as 'address_ambiguous' (confidence 0.3)
    2. Name within the same ZIP, similarity >= 0.85 (confidence 0.5)
    3. Otherwise unmatched (most are non-food Environmental Health complaints).
    """
    ests = con.execute("SELECT record_id, name, address, zip, mar_address FROM establishments").fetchall()
    by_hz: dict[tuple[str, str], list[tuple[str, sqlite3.Row]]] = {}
    by_zip: dict[str, list[sqlite3.Row]] = {}
    for e in ests:
        for street, z in _est_address_keys(e):
            h = re.match(r"(\d+)", street)
            if h:
                by_hz.setdefault((h.group(1), z), []).append((street, e))
        if e["zip"]:
            by_zip.setdefault(str(e["zip"])[:5], []).append(e)

    reviewed = {r["record_number"] for r in con.execute("SELECT DISTINCT record_number FROM complaint_matches WHERE reviewed=1")}
    rows = con.execute(
        "SELECT record_number, work_location, project_name, description FROM complaints WHERE detail_status='ok'"
    ).fetchall()

    stats = {"complaints": len(rows), "address": 0, "address+name": 0, "address_ambiguous": 0, "name_zip": 0,
             "unmatched": 0, "reviewed_kept": 0}
    out: list[tuple] = []
    for c in rows:
        rec = c["record_number"]
        if rec in reviewed:
            stats["reviewed_kept"] += 1
            continue
        cname = c["project_name"] or c["description"]
        addr = norm_address(c["work_location"])
        hz = _house_zip(addr)
        cands: dict[str, sqlite3.Row] = {}
        if hz:
            for street, e in by_hz.get(hz, []):
                if addr == street or addr.startswith(street + " "):
                    cands[e["record_id"]] = e
        scored = sorted(((name_similarity(cname, e["name"]), rid) for rid, e in cands.items()), reverse=True)

        if len(scored) == 1:
            sim, rid = scored[0]
            method, conf = ("address+name", 0.95) if sim >= name_threshold else ("address", 0.7)
            out.append((rec, rid, method, conf, round(sim, 3)))
            stats[method] += 1
        elif scored:
            best_sim, best = scored[0]
            runner_up = scored[1][0]
            exact_wins = best_sim >= 0.99 and runner_up < 0.99      # exact name beats a "contains" match
            if best_sim >= name_threshold and (best_sim - runner_up >= 0.15 or exact_wins):
                out.append((rec, best, "address+name", 0.85, round(best_sim, 3)))
                stats["address+name"] += 1
            else:
                out += [(rec, rid, "address_ambiguous", 0.3, round(sim, 3)) for sim, rid in scored]
                stats["address_ambiguous"] += 1
        else:
            z = hz[1] if hz else None
            best = max(((name_similarity(cname, e["name"]), e["record_id"]) for e in by_zip.get(z, [])), default=None)
            if best and best[0] >= 0.85:
                out.append((rec, best[1], "name_zip", 0.5, round(best[0], 3)))
                stats["name_zip"] += 1
            else:
                stats["unmatched"] += 1

    with con:
        con.execute("DELETE FROM complaint_matches WHERE reviewed=0")
        con.executemany(
            "INSERT INTO complaint_matches (record_number, record_id, method, confidence, name_similarity) VALUES (?,?,?,?,?)",
            out,
        )
    return stats
