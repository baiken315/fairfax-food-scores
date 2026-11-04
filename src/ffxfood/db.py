from __future__ import annotations

import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import config

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30)       # a long fetch may hold the write lock briefly
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    _drop_replaced_derived_tables(con)
    con.executescript(_SCHEMA)
    _migrate(con)
    return con


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS won't add them to an
# existing database, so add any that are missing. Append-only; keep in sync with schema.sql.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "establishments": [("mar_address", "TEXT"), ("mar_validated", "TEXT")],
    "reports": [("raw_text_z", "BLOB")],
    "report_items": [
        ("tier", "TEXT"), ("is_repeat", "INTEGER"), ("all_cdi", "INTEGER"),
        ("n_violations", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _drop_replaced_derived_tables(con: sqlite3.Connection) -> None:
    """Derived tables whose key changed are rebuilt rather than migrated (re-run `ffx score`)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(scores)")}
    if cols and "weight_set" not in cols:       # v0.1 scores: one row per location
        con.execute("DROP TABLE scores")


# Columns removed on purpose; dropped from existing databases so the data doesn't linger.
_DROPPED_COLUMNS: dict[str, list[str]] = {
    "report_summary": ["inspector"],          # inspector names: removed 2026-10-23
}


def _migrate(con: sqlite3.Connection) -> None:
    for table, cols in _DROPPED_COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name in cols:
            if name in have:
                con.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    con.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Report text is the permanent record once the county deletes the PDF, so it is kept, zlib-compressed.
def pack_text(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), 9)


def report_text(row: sqlite3.Row) -> str | None:
    """Text of a reports row, from raw_text_z or (rows fetched before compression) raw_text."""
    if row["raw_text_z"] is not None:
        return zlib.decompress(row["raw_text_z"]).decode("utf-8")
    return row["raw_text"]


def compact_reports(con: sqlite3.Connection, batch: int = 500) -> int:
    """Move legacy uncompressed raw_text into raw_text_z. Returns rows converted."""
    n = 0
    while True:
        rows = con.execute(
            "SELECT inspection_id, raw_text FROM reports WHERE raw_text IS NOT NULL LIMIT ?", (batch,)
        ).fetchall()
        if not rows:
            break
        with con:
            con.executemany(
                "UPDATE reports SET raw_text_z=?, raw_text=NULL WHERE inspection_id=?",
                [(pack_text(r["raw_text"]), r["inspection_id"]) for r in rows],
            )
        n += len(rows)
    return n
