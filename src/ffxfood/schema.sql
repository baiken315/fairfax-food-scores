-- Source data mirrored from the county FeatureServer ---------------------------

CREATE TABLE IF NOT EXISTS establishments (
    record_id            TEXT PRIMARY KEY,          -- e.g. HFOOD-000009369
    name                 TEXT,
    address              TEXT,
    city                 TEXT,
    state                TEXT,
    zip                  TEXT,
    category             TEXT,                      -- CATEGORY_TYPE (Full Service Restaurant, ...)
    district             TEXT,
    jurisdiction         TEXT,
    status               TEXT,                      -- Active / Expired / ...
    stamp                TEXT,                      -- Yes / No
    permit_issue_date    TEXT,
    permit_expiration_date TEXT,
    last_inspection_date TEXT,
    lon                  REAL,
    lat                  REAL,
    source_objectid      INTEGER,
    fetched_at           TEXT,
    mar_address          TEXT,                      -- county Master Address Repository form: "STREET, CITY, VA, ZIP"
    mar_validated        TEXT                       -- YES / NO
);

CREATE TABLE IF NOT EXISTS establishment_history (  -- field changes seen between ingests (upserts overwrite)
    record_id   TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_at  TEXT NOT NULL                        -- ingest time the change was noticed
);
CREATE INDEX IF NOT EXISTS idx_est_history ON establishment_history(record_id);

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id        TEXT PRIMARY KEY,          -- matches "Inspection Number" in the PDF
    record_id            TEXT,                      -- -> establishments.record_id (not enforced: orphans exist)
    inspection_date      TEXT,                      -- ISO date, America/New_York
    result               TEXT,                      -- county's coarse Passed/... value
    report_url           TEXT,
    establishment_name   TEXT,
    source_objectid      INTEGER,
    fetched_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_inspections_record ON inspections(record_id);
CREATE INDEX IF NOT EXISTS idx_inspections_date   ON inspections(inspection_date);

-- Report pipeline state -------------------------------------------------------

CREATE TABLE IF NOT EXISTS reports (
    inspection_id  TEXT PRIMARY KEY REFERENCES inspections(inspection_id),
    fetch_status   TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | not_pdf | error
    http_status    INTEGER,
    fetched_at     TEXT,
    pdf_path       TEXT,                             -- only set with --keep-pdfs
    pdf_sha256     TEXT,
    n_pages        INTEGER,
    raw_text       TEXT,                             -- legacy uncompressed text; `ffx compact` moves it to raw_text_z
    raw_text_z     BLOB,                             -- zlib(pdftotext -layout output): the permanent record, re-parseable offline
    error          TEXT,
    parse_status   TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | mismatch | error
    parse_notes    TEXT,
    parsed_at      TEXT,
    parser_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_fetch ON reports(fetch_status);
CREATE INDEX IF NOT EXISTS idx_reports_parse ON reports(parse_status);

-- Parsed report content ---------------------------------------------------------
-- NOTE: person-in-charge, CFM numbers, permit holder and inspector (EHS) names are deliberately
-- NOT stored. They aren't needed for grading, and the site does not rate individuals.

CREATE TABLE IF NOT EXISTS report_summary (
    inspection_id       TEXT PRIMARY KEY REFERENCES inspections(inspection_id),
    inspection_date     TEXT,                        -- from the report header (authoritative)
    time_in             TEXT,
    time_out            TEXT,
    purpose             TEXT,                        -- Routine / Complaint / Follow-up ...
    establishment_type  TEXT,
    priority_level      INTEGER,                     -- facility risk level (1-4)
    rf_out              INTEGER,                     -- header: risk factors out of compliance
    rf_repeat_out       INTEGER,                     -- header: repeat risk factors out
    grp_out             INTEGER,                     -- header: good retail practices out
    counts_match        INTEGER,                     -- 1 if header counts == counts from the item grid
    follow_up_required  INTEGER,                     -- "Follow-up Inspection Required" (SCHEDULING section)
    temperature_text    TEXT,                        -- raw "TEMPERATURE OBSERVATIONS" section
    comments            TEXT                         -- raw "COMMENTS" section
);

CREATE TABLE IF NOT EXISTS report_items (            -- the 56-row IN/OUT/N/O/N/A grid
    inspection_id TEXT NOT NULL REFERENCES inspections(inspection_id),
    item_no       INTEGER NOT NULL,                  -- 1-29 risk factors, 30-56 good retail practices
    section       TEXT NOT NULL,                     -- risk_factor | good_retail_practice
    status        TEXT NOT NULL,                     -- IN | OUT | N/O | N/A
    cdi           INTEGER NOT NULL DEFAULT 0,        -- CDI printed in the grid itself (rare; see all_cdi)
    -- Rolled up from this item's violations (NULL when the item has none):
    tier          TEXT,                              -- most severe tier cited
    is_repeat     INTEGER,                           -- any citation marked REPEAT
    all_cdi       INTEGER,                           -- every citation corrected during inspection
    n_violations  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (inspection_id, item_no)
);

CREATE TABLE IF NOT EXISTS violations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id     TEXT NOT NULL REFERENCES inspections(inspection_id),
    seq               INTEGER NOT NULL,              -- order within the report
    item_no           INTEGER,
    code              TEXT,                          -- Food Code citation, e.g. 3-302.11(A)(2)
    title             TEXT,
    tier              TEXT,                          -- priority | priority foundation | core
    is_repeat         INTEGER NOT NULL DEFAULT 0,
    cdi               INTEGER NOT NULL DEFAULT 0,    -- corrected during inspection
    observation       TEXT,
    corrective_action TEXT
);
CREATE INDEX IF NOT EXISTS idx_violations_inspection ON violations(inspection_id);
CREATE INDEX IF NOT EXISTS idx_violations_code       ON violations(code);

-- Derived output --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS inspection_scores (       -- one score per inspection per weight set (trend lines)
    inspection_id  TEXT NOT NULL,
    weight_set     TEXT NOT NULL,                    -- scoring.WEIGHT_SETS key
    model_version  TEXT NOT NULL,
    deduction      REAL NOT NULL,                    -- points off 100
    score          REAL NOT NULL,                    -- 0-100, higher is better
    PRIMARY KEY (inspection_id, weight_set)
);

CREATE TABLE IF NOT EXISTS scores (                  -- per location per weight set
    record_id           TEXT NOT NULL,
    weight_set          TEXT NOT NULL,
    model_version       TEXT NOT NULL,
    computed_at         TEXT NOT NULL,
    n_inspections       INTEGER NOT NULL,            -- parsed routine inspections used
    effective_n         REAL NOT NULL,               -- recency-weighted sample size (confidence)
    raw_score           REAL NOT NULL,               -- recency-weighted mean inspection score
    score               REAL NOT NULL,               -- shrunk toward the category mean; 0-100, higher is better
    category            TEXT,
    category_percentile REAL,                        -- 0 = best, 100 = worst within category
    letter              TEXT,                        -- provisional; NULL = insufficient data
    PRIMARY KEY (record_id, weight_set)
);

-- Health complaints (HCOMP records from the PLUS portal) ------------------------
-- Public fields only. Complaint category, outcome and substantiation are not public, so a
-- complaint is an allegation, not a finding.

CREATE TABLE IF NOT EXISTS complaints (
    record_number      TEXT PRIMARY KEY,             -- HCOMP-2026-00702
    submitted_date     TEXT,                         -- ISO date
    record_type        TEXT,                         -- "EH Complaint"
    status             TEXT,                         -- Closed / Inspections Pending / ...
    short_notes        TEXT,                         -- inspection areas, e.g. "CPP Area-B2,OSW Area-2"
    project_name       TEXT,                         -- usually the business name
    work_location      TEXT,                         -- "234 MAPLE AVE E VIENNA, VA 22180"
    address_gis_id     TEXT,                         -- county address id from the detail page
    description        TEXT,                         -- "Application Description" (usually the name again)
    detail_url         TEXT,                         -- stable CapDetail URL (plain GET works)
    detail_status      TEXT NOT NULL DEFAULT 'pending',   -- pending | ok | error
    first_seen_at      TEXT,
    last_seen_at       TEXT,
    detail_fetched_at  TEXT,
    error              TEXT
);
CREATE INDEX IF NOT EXISTS idx_complaints_date ON complaints(submitted_date);

CREATE TABLE IF NOT EXISTS complaint_matches (
    record_number  TEXT NOT NULL REFERENCES complaints(record_number),
    record_id      TEXT NOT NULL,                    -- -> establishments.record_id
    method         TEXT NOT NULL,                    -- address | address+name | address_ambiguous | name_zip | manual
    confidence     REAL NOT NULL,                    -- 0-1
    name_similarity REAL,
    reviewed       INTEGER NOT NULL DEFAULT 0,       -- 1 = confirmed by a person; never overwritten
    PRIMARY KEY (record_number, record_id)
);
CREATE INDEX IF NOT EXISTS idx_complaint_matches_est ON complaint_matches(record_id);

-- Per-location summary -------------------------------------------------------------------------
-- Score columns use whichever weight set is passed via the scores table join; this view uses the
-- current default. Full nested record: `ffx profile <record_id>` / `ffx export --per-location`.
DROP VIEW IF EXISTS establishment_profile;
CREATE VIEW establishment_profile AS
SELECT e.record_id, e.name, e.address, e.city, e.zip, e.category, e.status, e.lon, e.lat,
       (SELECT COUNT(*) FROM inspections i WHERE i.record_id = e.record_id)                     AS n_inspections,
       (SELECT MIN(inspection_date) FROM inspections i WHERE i.record_id = e.record_id)         AS first_inspection,
       (SELECT MAX(inspection_date) FROM inspections i WHERE i.record_id = e.record_id)         AS last_inspection,
       (SELECT COUNT(*) FROM inspections i JOIN reports r USING(inspection_id)
         WHERE i.record_id = e.record_id AND r.parse_status = 'ok')                             AS n_parsed,
       (SELECT SUM(s.rf_out) FROM inspections i JOIN report_summary s USING(inspection_id)
         WHERE i.record_id = e.record_id)                                                       AS rf_out_total,
       (SELECT SUM(s.grp_out) FROM inspections i JOIN report_summary s USING(inspection_id)
         WHERE i.record_id = e.record_id)                                                       AS grp_out_total,
       (SELECT COUNT(*) FROM inspections i JOIN report_items ri USING(inspection_id)
         WHERE i.record_id = e.record_id AND ri.is_repeat = 1)                                  AS repeat_items_total,
       (SELECT COUNT(*) FROM complaint_matches m JOIN complaints c USING(record_number)
         WHERE m.record_id = e.record_id AND m.confidence >= 0.7)                               AS n_complaints,
       (SELECT COUNT(*) FROM complaint_matches m JOIN complaints c USING(record_number)
         WHERE m.record_id = e.record_id AND m.confidence >= 0.7
           AND c.submitted_date >= date('now', '-365 days'))                                    AS n_complaints_12m,
       sc.weight_set, sc.score, sc.letter, sc.effective_n, sc.category_percentile
FROM establishments e
LEFT JOIN scores sc ON sc.record_id = e.record_id AND sc.weight_set = 'rf-heavy';
