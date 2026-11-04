"""Runtime configuration (env-overridable)."""
from __future__ import annotations

import os
from pathlib import Path

FEATURE_SERVER = os.environ.get(
    "FFX_FEATURE_SERVER",
    "https://www.fairfaxcounty.gov/gispub2/rest/services/Health/PermittedFoodEstablishments/FeatureServer",
)
ESTABLISHMENTS_LAYER = 0
INSPECTIONS_TABLE = 1

DATA_DIR = Path(os.environ.get("FFX_DATA_DIR", "data"))
DB_PATH = Path(os.environ.get("FFX_DB", DATA_DIR / "ffx.sqlite"))
PDF_DIR = DATA_DIR / "pdfs"

# The county's Accela "PLUS" portal: hosts the report PDFs and the complaint (HCOMP) records.
PLUS_BASE = os.environ.get("FFX_PLUS_BASE", "https://plus.fairfaxcounty.gov/CitizenAccess")

# Be a good citizen toward the county's permitting portal. Put a way to reach you here.
CONTACT = os.environ.get("FFX_CONTACT", "unset-contact")
USER_AGENT = f"ffxfood/0.1 (research; contact: {CONTACT})"

# The web map the county's own viewer uses hides these; mirror that by default.
VISIBLE_STATUSES = ("Active", "Expired")

LOCAL_TZ = "America/New_York"
