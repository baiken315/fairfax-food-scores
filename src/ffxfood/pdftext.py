"""PDF -> layout-preserving text.

The parser is tuned to poppler's `pdftotext -layout` output, so that is the primary path.
pdfplumber is a fallback for machines without poppler, but its layout text differs slightly;
re-run the tests against it before trusting it at scale.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def pdf_bytes_to_text(data: bytes) -> tuple[str, int]:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "report.pdf"
        p.write_bytes(data)
        return pdf_file_to_text(p)


def pdf_file_to_text(path: str | Path) -> tuple[str, int]:
    """Return (text, page_count)."""
    if shutil.which("pdftotext"):
        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, check=True,
        ).stdout
        return out, max(out.count("\f"), 1)
    try:
        import pdfplumber  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "No PDF text extractor found. Install poppler (pdftotext) or `pip install ffxfood[pdfplumber]`."
        ) from e
    with pdfplumber.open(str(path)) as pdf:
        pages = [(pg.extract_text(layout=True) or "") for pg in pdf.pages]
    return "\f".join(pages), len(pages)
