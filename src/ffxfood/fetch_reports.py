"""Download report PDFs listed in `reports` (resumable, rate limited) and extract text.

Storage note: reports are ~0.5 MB each, so a full backfill of ~13k reports is ~6.5 GB of PDFs.
By default we keep only the extracted text (~16 KB each, zlib-compressed in SQLite) and discard the PDF;
pass --keep-pdfs to retain originals (useful while the parser is still evolving, or for audit).
"""
from __future__ import annotations

import hashlib
import sqlite3
import time

import requests

from . import config
from .db import now_iso, pack_text
from .pdftext import pdf_bytes_to_text


def fetch_pending(
    con: sqlite3.Connection,
    *,
    limit: int | None = None,
    since: str | None = None,
    delay: float = 1.5,
    keep_pdfs: bool = False,
    retry_errors: bool = False,
    random_order: bool = False,
    oldest_first: bool = False,
    max_consecutive_errors: int = 8,
) -> dict[str, int]:
    statuses = ["pending"] + (["error"] if retry_errors else [])
    q = (
        "SELECT i.inspection_id, i.report_url FROM reports r JOIN inspections i USING(inspection_id) "
        f"WHERE r.fetch_status IN ({','.join('?' * len(statuses))}) AND i.report_url IS NOT NULL"
    )
    args: list = list(statuses)
    if since:
        q += " AND i.inspection_date >= ?"
        args.append(since)
    # Newest first is most valuable for grading; --random samples across eras (good for parser QA);
    # --oldest-first rescues reports about to age out of the county's ~2-year retention window.
    if random_order:
        q += " ORDER BY RANDOM()"
    else:
        q += " ORDER BY i.inspection_date ASC" if oldest_first else " ORDER BY i.inspection_date DESC"
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    todo = con.execute(q, args).fetchall()

    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    stats = {"ok": 0, "not_pdf": 0, "error": 0, "throttled": 0, "total": len(todo)}
    consecutive = 0

    for n, row in enumerate(todo):
        iid, url = row["inspection_id"], row["report_url"]
        try:
            r = s.get(url, timeout=60, allow_redirects=True)
            if r.status_code in (429, 503):
                # Throttled: back off and leave the report 'pending' so the next run picks it up.
                stats["throttled"] += 1
                consecutive += 1
                time.sleep(min(60, 5 * consecutive))
            elif r.status_code != 200:
                _save(con, iid, "error", http_status=r.status_code, error=f"HTTP {r.status_code}")
                stats["error"] += 1
                consecutive += 1
            elif not r.content.startswith(b"%PDF"):
                _save(con, iid, "not_pdf", http_status=200, error=r.headers.get("Content-Type"))
                stats["not_pdf"] += 1
                consecutive = 0
            else:
                text, pages = pdf_bytes_to_text(r.content)
                pdf_path = None
                if keep_pdfs:
                    config.PDF_DIR.mkdir(parents=True, exist_ok=True)
                    p = config.PDF_DIR / f"{iid}.pdf"
                    p.write_bytes(r.content)
                    pdf_path = str(p)
                _save(
                    con, iid, "ok", http_status=200, raw_text_z=pack_text(text), n_pages=pages,
                    pdf_sha256=hashlib.sha256(r.content).hexdigest(), pdf_path=pdf_path,
                )
                stats["ok"] += 1
                consecutive = 0
        except Exception as e:  # network error, poppler failure, ...
            _save(con, iid, "error", error=str(e)[:500])
            stats["error"] += 1
            consecutive += 1

        if consecutive >= max_consecutive_errors:
            print(f"stopping: {consecutive} consecutive errors (last: {iid}). Re-run later.")
            break
        if n < len(todo) - 1:
            time.sleep(delay)
    return stats


def _save(con: sqlite3.Connection, iid: str, status: str, **cols) -> None:
    cols = {"fetch_status": status, "fetched_at": now_iso(), **cols}
    sets = ", ".join(f"{k}=?" for k in cols)
    with con:
        con.execute(f"UPDATE reports SET {sets}, parse_status='pending' WHERE inspection_id=?", [*cols.values(), iid])
