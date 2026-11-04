"""Mirror the county FeatureServer (establishments + inspection index) into SQLite."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

import requests

from . import config
from .db import now_iso

PAGE = 2000
# Fields whose changes are recorded in establishment_history (a rename, closure, move, recategorization).
TRACKED_FIELDS = ("name", "address", "city", "zip", "category", "status", "stamp", "mar_address")
_TZ = ZoneInfo(config.LOCAL_TZ)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    return s


def _ms_to_local_date(ms: int | None) -> str | None:
    """Esri dates are epoch-ms UTC. Reports use local (ET) dates, so convert before truncating."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(_TZ).date().isoformat()


def query_all(layer: int, *, where: str = "1=1", geometry: bool = False,
              session: requests.Session | None = None) -> Iterator[dict]:
    """Page through a FeatureServer layer/table (max 2000 rows per request)."""
    s = session or _session()
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": str(geometry).lower(),
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE,
            "f": "json",
        }
        if geometry:
            params["outSR"] = 4326
        r = s.get(f"{config.FEATURE_SERVER}/{layer}/query", params=params, timeout=90)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"FeatureServer error: {body['error']}")
        feats = body.get("features", [])
        yield from feats
        if not feats or (len(feats) < PAGE and not body.get("exceededTransferLimit")):
            return
        offset += len(feats)


def ingest_establishments(con: sqlite3.Connection, *, all_statuses: bool = False) -> int:
    where = "1=1"
    if not all_statuses:
        quoted = " OR ".join(f"ESTABLISHMENT_STATUS = '{s}'" for s in config.VISIBLE_STATUSES)
        where = f"({quoted})"
    n, ts = 0, now_iso()
    cols = ", ".join(TRACKED_FIELDS)
    before = {r["record_id"]: r for r in con.execute(f"SELECT record_id, {cols} FROM establishments")}
    with con:
        for f in query_all(config.ESTABLISHMENTS_LAYER, where=where, geometry=True):
            a, g = f["attributes"], f.get("geometry") or {}
            _record_changes(con, before.get(a.get("RECORDID")), a, ts)
            con.execute(
                """INSERT INTO establishments
                   (record_id,name,address,city,state,zip,category,district,jurisdiction,status,stamp,
                    permit_issue_date,permit_expiration_date,last_inspection_date,lon,lat,source_objectid,fetched_at,
                    mar_address,mar_validated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(record_id) DO UPDATE SET
                     name=excluded.name, address=excluded.address, city=excluded.city, state=excluded.state,
                     zip=excluded.zip, category=excluded.category, district=excluded.district,
                     jurisdiction=excluded.jurisdiction, status=excluded.status, stamp=excluded.stamp,
                     permit_issue_date=excluded.permit_issue_date,
                     permit_expiration_date=excluded.permit_expiration_date,
                     last_inspection_date=excluded.last_inspection_date,
                     lon=excluded.lon, lat=excluded.lat, source_objectid=excluded.source_objectid,
                     fetched_at=excluded.fetched_at, mar_address=excluded.mar_address,
                     mar_validated=excluded.mar_validated""",
                (
                    a.get("RECORDID"), a.get("ESTABLISHMENT_NAME"), a.get("PERMIT_ADDRESS"),
                    a.get("PERMIT_CITY"), a.get("PERMIT_STATE"), a.get("PERMIT_ZIP"),
                    a.get("CATEGORY_TYPE"), a.get("DISTRICT"), a.get("JURISDICTION"),
                    a.get("ESTABLISHMENT_STATUS"), a.get("STAMP"),
                    _ms_to_local_date(a.get("PERMIT_ISSUE_DATE")),
                    _ms_to_local_date(a.get("PERMIT_EXPIRATION_DATE")),
                    _ms_to_local_date(a.get("LAST_INSPECTION_DATE")),
                    g.get("x"), g.get("y"), a.get("OBJECTID"), ts,
                    a.get("MAR_ADDRESSES"), a.get("MAR_VALIDATED"),
                ),
            )
            n += 1
    return n


_SOURCE_FIELD = {
    "name": "ESTABLISHMENT_NAME", "address": "PERMIT_ADDRESS", "city": "PERMIT_CITY", "zip": "PERMIT_ZIP",
    "category": "CATEGORY_TYPE", "status": "ESTABLISHMENT_STATUS", "stamp": "STAMP", "mar_address": "MAR_ADDRESSES",
}


def _record_changes(con: sqlite3.Connection, old: sqlite3.Row | None, a: dict, ts: str) -> None:
    if old is None:
        return
    for field in TRACKED_FIELDS:
        new = a.get(_SOURCE_FIELD[field])
        new = None if new is None else str(new)
        if old[field] is not None and new != (None if old[field] is None else str(old[field])):
            con.execute(
                "INSERT INTO establishment_history (record_id, field, old_value, new_value, changed_at) VALUES (?,?,?,?,?)",
                (old["record_id"], field, old[field], new, ts),
            )


def ingest_inspections(con: sqlite3.Connection) -> int:
    n, ts = 0, now_iso()
    with con:
        for f in query_all(config.INSPECTIONS_TABLE):
            a = f["attributes"]
            if not a.get("INSPECTIONID"):
                continue
            con.execute(
                """INSERT INTO inspections
                   (inspection_id,record_id,inspection_date,result,report_url,establishment_name,source_objectid,fetched_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(inspection_id) DO UPDATE SET
                     record_id=excluded.record_id, inspection_date=excluded.inspection_date,
                     result=excluded.result, report_url=excluded.report_url,
                     establishment_name=excluded.establishment_name,
                     source_objectid=excluded.source_objectid, fetched_at=excluded.fetched_at""",
                (
                    str(a["INSPECTIONID"]), a.get("RECORDID"),
                    _ms_to_local_date(a.get("INSPECTIONDATE")), a.get("INSPECTIONRESULT"),
                    a.get("WEBSITE_LINK"), a.get("ESTABLISHMENT_NAME"), a.get("OBJECTID"), ts,
                ),
            )
            if a.get("WEBSITE_LINK"):
                con.execute("INSERT OR IGNORE INTO reports (inspection_id) VALUES (?)", (str(a["INSPECTIONID"]),))
            n += 1
    return n
