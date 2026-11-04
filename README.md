# ffxfood: restaurant grades from Fairfax County inspection reports

The county's [Retail Food Inspection Viewer](https://experience.arcgis.com/experience/0e687ef56da44ef287d20ced8cc85a3f/page/Main-Page)
only exposes a coarse Passed/Failed flag per inspection. The real signal is in the linked PDF
reports: which violations, how serious, whether repeated, whether corrected on the spot.
This project mirrors the county's public data, parses those PDFs into structured tables,
and computes a transparent, tunable grade per establishment.

```
ArcGIS FeatureServer ──ingest──▶ establishments, inspections ──fetch──▶ report PDFs ──pdftotext──▶ raw_text
                                                                                                    │ parse
          scores / letter  ◀──score── violations (code, tier, repeat, CDI, narrative) ◀──────────────┘
```

## Quickstart

```bash
pip install -e '.[dev]'          # needs poppler's `pdftotext` on PATH (apt/brew install poppler-utils),
                                 # or:  pip install -e '.[dev,pdfplumber]'  (fallback; layout text differs slightly)
export FFX_CONTACT=you@example.com   # sent in the User-Agent so the county can reach you

pytest                           # parser + scoring tests
ffx ingest --all-statuses        # ~4 s: 3.9k establishments, 13k inspections
ffx fetch --oldest-first         # resumable, 1.5 s between requests (~3 s/report in practice: ~10 h full backfill)
ffx parse
ffx complaints scrape            # HCOMP health complaints, 2023-now (plain HTTP, no browser)
ffx complaints match             # complaint -> establishment by address/name; `review` / `confirm` for edge cases
ffx score                        # every weight set; see Scoring
ffx worksheet HFOOD-000037730    # one location's arithmetic as a self-checking HTML sheet
ffx calibrate --out data/calibration.md
ffx stats; ffx complaints stats
ffx profile HFOOD-2025-00127     # one location's complete record (JSON)
ffx export --format csv --out grades.csv          # or --per-location (JSON Lines) / --weight-set NAME
```

`ffx run` does ingest/fetch/parse/score. `ffx parse-file some.pdf` prints the parsed JSON for one report (debugging).
`ffx compact` compresses report text stored by older versions.
Scheduled refresh: `scripts/refresh.sh`, run weekly by the systemd user timer in `deploy/systemd/`.
Data lands in `data/ffx.sqlite` (override with `FFX_DATA_DIR` / `FFX_DB`).

## What the county data looks like (verified Oct 2026)

- **Source:** public ArcGIS FeatureServer, no auth, 2,000-row pages:
  `https://www.fairfaxcounty.gov/gispub2/rest/services/Health/PermittedFoodEstablishments/FeatureServer`
  (layer 0 = establishments, table 1 = inspections, related by `RECORDID`).
- **Joins:** `inspections.INSPECTIONID` equals the "Inspection Number" printed in the PDF.
- **History is ~2 years.** The county keeps reports online for 2 years, so oldest inspections are Oct 2024.
  Start collecting now if you want longer history, because older reports disappear.
- **Only routine inspections** appear in the table.
- **Volume:** ~13k reports × ~0.5 MB ≈ 6.5 GB of PDFs. PDFs are not kept: the extracted text is the permanent
  record (zlib-compressed in `reports.raw_text_z`, re-parseable offline), with `pdf_sha256` + `report_url`
  as provenance. `--keep-pdfs` exists for parser debugging only.
- **Expiry is real:** reports age out at ~2 years (one dated 2024-10-30 was already gone on 2026-10-22),
  so backfills run `--oldest-first`.
- **Locations** carry `MAR_ADDRESSES` (county Master Address Repository form), used to match complaints.
- **Update cadence:** the county says the establishment layer refreshes monthly.

## Report anatomy and parser

Each report has: header (date, times, purpose, type, risk level, out-of-compliance counts) → a
numbered grid of 56 Food Code items (1–29 risk factors, 30–56 good retail practices; IN/OUT/N/O/N/A) →
temperature tables → **violations** (grouped Priority / Priority Foundation / Core, each with Food Code
citation, REPEAT and CDI flags, Observation and Corrective Action text) → comments → scheduling.

`parse_report.py` works on `pdftotext -layout` text and is checked against the header itself:

- `counts_match`: header "Out of Compliance" totals must equal the OUT counts in the parsed grid.
  Anything else is stored with `parse_status='mismatch'` and a note. **Watch that rate**; it is your
  tripwire for layout drift. Only `ok` reports feed scoring.
- Known variant: ~17% of reports omit the Good Retail Practices grid entirely. That is accepted only when
  the header says 0 are out (otherwise it's flagged). A partial grid is always flagged.
- Validation so far: 140 live reports (60 newest + 80 random across the 2-year window), 140/140 `ok`.
  That is a smoke test, not proof. Run `ffx fetch --random --limit 300 && ffx parse` and read the mismatches
  before trusting a full backfill.
- Parsed violation *text* is stored raw. Severity beyond the tier (e.g. "live roaches" vs "label missing")
  is the main upside of NLP here; see roadmap.

### Privacy decisions
Person-in-charge, certified-manager numbers, permit-holder and inspector names are never extracted or stored.
Tests assert it. The site rates establishments, not individuals.

## Scoring (v0.2, `scoring.py`)

1. Per inspection: each OUT grid item counts once (however many citations it has). Deduction =
   base (risk factor vs good retail) × tier multiplier (most severe citation) × repeat × CDI × per-item multiplier.
   IN items have no deduction; N/O and N/A are ignored. Inspection score = 100 − deductions.
2. Several **named weight sets** are scored side by side (`v0-tier`, `rf-heavy`, `rf-heavy-cdi`, `drivers`,
   `flat`, `petran`, `rate`). They are hypotheses; see `docs/scoring-research.md` for where each comes from.
3. Per location: recency-weighted mean (1-year half-life; weight sum = `effective_n`), shrunk toward the
   category mean (1 pseudo-inspection), provisional letter from fixed cut-offs (90/80/70/60), no letter when `effective_n` < 0.75.
   The category percentile is kept as a secondary figure.
4. `ffx calibrate` compares the sets (and shrinkage strengths) on proxies: next-inspection risk-factor OUTs,
   inspector follow-up, and complaints within 180 days.

**Default `rf-heavy`** (chosen 2026-10-23: all sets predict about equally, AUC ≈ 0.75; this one spreads grades best).
Letter cut-offs are still provisional. Complaints are shown per
location and used for calibration only, since their category and outcome are not public.

## Website (static, Netlify)
`ffx publish --out dist` copies `web/` (vanilla HTML/JS + Leaflet) and writes the public data:
`data/locations.json` (map/list), `data/loc/<record_id>.json` (one per location), `data/meta.json`
(method, letter rules, point table). About 52 MB uncompressed, ~13 MB gzipped; no backend.

```bash
ffx publish --out dist && python -m http.server 8765 --directory dist     # preview at http://localhost:8765
scripts/push_site.sh                                                        # deploy: replaces the `site` branch
```
Netlify is connected to the GitHub repo's **`site` branch** (publish directory `.`, no build command; see
`web/netlify.toml`). `main` holds the code only. The weekly refresh pushes a new build when `FFX_DEPLOY=1`.
Map tiles come from openstreetmap.org (fine for modest traffic under its usage policy); switch to a keyed
provider if traffic grows. A database (e.g. Supabase) is only needed if the site gains write features.

## Before you publish grades
The county's own viewer warns that a single inspection should not be used to evaluate an establishment, and
the data license is only a pointer to the [county GIS disclaimer](https://www.fairfaxcounty.gov/maps/disclaimer).
A public rating of named businesses has reputational and legal exposure: read the disclaimer and the permit
portal's terms, check any scraping/rate limits, link every grade back to the source reports, and show the
method. Be polite to the portal (the fetcher rate-limits, backs off on 429/503, and stops after repeated errors).

## Layout
```
src/ffxfood/
  config.py       endpoints, paths, UA
  schema.sql      tables: establishments, inspections, reports, report_summary, report_items, violations, scores
  ingest.py       FeatureServer → SQLite (paged, upserts, ET date conversion)
  fetch_reports.py  resumable, rate-limited PDF download + text extraction
  pdftext.py      pdftotext -layout (pdfplumber fallback)
  parse_report.py header / grid / violations / comments parser (pure text → dict)
  parse_store.py  parsed dict → tables
  scoring.py      grade model (weight sets, inspection + location scores)
  worksheet.py    HTML audit sheet: one location's grid items -> letter, cross-checked against the model
  calibrate.py    weight-set comparison against outcome proxies
  complaints.py   HCOMP complaint scraper (PLUS portal) + matching to establishments
  profile.py      per-location nested record
  cli.py          `ffx` command
scripts/          refresh.sh (weekly), after_backfill.sh (one-off)
deploy/systemd/   user timer + service for the weekly refresh
docs/             scoring-research.md
tests/            parser, scoring, calibration, complaints (trimmed real HTML fixtures)
```

## Roadmap
1. Run a larger random QA sample; add fixtures for any new layout variants found.
2. Parse temperature tables (equipment/food temp, process step) into rows.
3. Severity from narrative text (rules first; an LLM pass over `observation` is the likely upgrade).
4. Calibrate the score; add confidence handling and an "insufficient data" state.
5. Serve it: read-only API (FastAPI over the SQLite/Postgres) + map UI keyed on `lon/lat`.
6. Scheduled refresh (monthly establishments; weekly new inspections) and change tracking via the layer's `last_edited_date`.
