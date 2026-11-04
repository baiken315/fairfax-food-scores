"""Parse `pdftotext -layout` text of a Fairfax "Food Establishment Inspection Report".

Report anatomy (as of 2026 reports):
  1. Header: date/time, summary counts, establishment info, purpose / type / priority level
  2. Two-column grid of 56 numbered items, each IN / OUT / N/O / N/A (1-29 risk factors,
     30-56 good retail practices)
  3. Temperature observations (free-form tables)
  4. OBSERVATIONS AND CORRECTIVE ACTIONS: violations grouped by tier (Priority,
     Priority Foundation, Core), each with Food Code citation, REPEAT / CDI flags,
     Observation and Corrective Action narrative
  5. COMMENTS: free text

Everything here works on text only so it can be unit-tested without a PDF library.
`counts_match` cross-checks the header's "Out of Compliance" totals against the grid and is
the main tripwire for layout drift: watch the rate of parse_status='mismatch'.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

PARSER_VERSION = "0.3.1"
N_ITEMS = 56
LAST_RISK_FACTOR_ITEM = 29

_STATUS_RE = re.compile(r"(?<!\S)(IN|OUT|NO|NA|N/O|N/A)(?:\s*/\s*(CDI))?(?!\S)")
_NUM_RE = re.compile(r"(?<!\S)(\d{1,2})(?!\S)")
_NORMALIZE = {"IN": "IN", "OUT": "OUT", "NO": "N/O", "N/O": "N/O", "NA": "N/A", "N/A": "N/A"}
# Citation line "<code> - <title>". Seen variants: "3-302.11(A)(2) - Title", "2-301.14(A)-(I) - Title",
# "2-201.11(A)(1)- (5) - Title", "3-203.12(C)(1)- Title" (no space before the hyphen) and
# "4-303.11(B) Title" (no hyphen) and the county typo "6-202-13(B)(1)-(2) - Title". Code = section number plus parenthesised parts; spaces and a trailing
# hyphen are stripped from it afterwards.
_CODE_RE = re.compile(r"^(\d+-\d+(?:[.-]\d+)?(?:\s*\([^)]*\)|-\s*\([^)]*\))*-?)\s*(?:-\s+|\s+)(\S.*)$")
_TIER_RE = re.compile(r"The following are (.+?) violations", re.I)


def _iso(mdy: str | None) -> str | None:
    if not mdy:
        return None
    return datetime.strptime(mdy, "%m/%d/%Y").date().isoformat()


def _search(pat: str, text: str, flags: int = 0) -> str | None:
    m = re.search(pat, text, flags)
    return m.group(1).strip() if m else None


def _int(s: str | None) -> int | None:
    return int(s) if s is not None and s.isdigit() else None


# --------------------------------------------------------------------------- grid

def parse_grid(text: str) -> dict[int, dict[str, Any]]:
    """Return {item_no: {"status": IN|OUT|N/O|N/A, "cdi": bool}}."""
    start = re.search(r"FOODBORNE ILLNESS RISK FACTORS", text)
    if not start:
        return {}
    rest = text[start.end():]
    end = re.search(r"Page\s+\d+\s+of\s+\d+|TEMPERATURE OBSERVATIONS", rest)
    lines = (rest[: end.start()] if end else rest).splitlines()

    items: dict[int, dict[str, Any]] = {}
    for i, line in enumerate(lines):
        for sm in _STATUS_RE.finditer(line):
            col = sm.start()
            num: int | None = None

            # Usual case: "<num>  <status>  <description>" on one line (two such columns per line).
            for nm in _NUM_RE.finditer(line[:col]):
                if col - nm.end() <= 12:
                    num = int(nm.group(1))

            # Wrapped-row case: status on this line, the number alone on the line above/below.
            if num is None:
                for j in (i - 1, i + 1):
                    if 0 <= j < len(lines) and re.fullmatch(r"\d{1,2}", lines[j].strip()):
                        nm = _NUM_RE.search(lines[j])
                        if nm and col - 20 <= nm.start() <= col + 4:
                            num = int(nm.group(1))
                            break

            if num is not None and 1 <= num <= N_ITEMS and num not in items:
                items[num] = {"status": _NORMALIZE[sm.group(1)], "cdi": bool(sm.group(2))}
    return items


# ------------------------------------------------------------------------ header

def parse_header(text: str) -> dict[str, Any]:
    head = text.split("FOODBORNE ILLNESS RISK FACTORS", 1)[0]
    h: dict[str, Any] = {
        "inspection_number": _search(r"Inspection Number:\s*(\d+)", head),
        "record_number": _search(r"Record Number:\s*(HFOOD-[\w-]+)", head),
        "inspection_date": _iso(_search(r"(?<!Expiration )(?<!Issue )\bDate:\s*(\d{2}/\d{2}/\d{4})", head)),
        "time_in": _search(r"Time In:\s*([\d:]+)", head),
        "time_out": _search(r"Time Out:\s*([\d:]+)", head),
        "rf_out": _int(_search(r"(?<!Repeat )Risk Factors/Interventions Out of Compliance:\s*(\d+)", head)),
        "rf_repeat_out": _int(_search(r"Repeat Risk Factors/Interventions Out of Compliance:\s*(\d+)", head)),
        "grp_out": _int(_search(r"Good Retail Practices Out of Compliance:\s*(\d+)", head)),
        "purpose": None,
        "establishment_type": None,
        "priority_level": None,
    }
    # Label row "Permit Holder:  EHS:  Purpose of Inspection:  Establishment Type:  Priority Level:" is followed
    # by a value row. Values start in their label's column; read them by position, because a long permit-holder
    # name can run into the EHS value with a single space (splitting on 2+ spaces then misaligns every column).
    # Neither the permit holder nor the inspector (EHS) name is extracted.
    lines = head.splitlines()
    labels = {"purpose": "Purpose of Inspection:",
              "establishment_type": "Establishment Type:", "priority_level": "Priority Level:"}
    for i, ln in enumerate(lines):
        if "Permit Holder:" in ln and "Purpose of Inspection:" in ln:
            nxt = next((x for x in lines[i + 1 : i + 4] if x.strip()), "")
            for key, label in labels.items():
                pos = ln.find(label)
                if pos >= 0:
                    val = (re.split(r"\s{2,}", nxt[pos:].strip()) or [""])[0]
                    h[key] = val or None
            h["priority_level"] = _int(h["priority_level"])
            break
    return h


def header_ehs_name(text: str) -> str | None:
    """The inspector (EHS) name from a report header. Used only, in memory, to redact names from published
    free text (see publish.py); never stored."""
    lines = text.split("FOODBORNE ILLNESS RISK FACTORS", 1)[0].splitlines()
    for i, ln in enumerate(lines):
        if "Permit Holder:" in ln and "EHS:" in ln:
            nxt = next((x for x in lines[i + 1 : i + 4] if x.strip()), "")
            val = (re.split(r"\s{2,}", nxt[ln.index("EHS:"):].strip()) or [""])[0]
            return val or None
    return None


# --------------------------------------------------------------------- violations

def _norm_tier(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip().lower())


def parse_violations(text: str) -> list[dict[str, Any]]:
    start = text.find("OBSERVATIONS AND CORRECTIVE ACTIONS")
    if start < 0:
        return []
    body = text[start:]
    end = re.search(r"^\s*COMMENTS\s*$", body, re.M)
    lines = (body[: end.start()] if end else body).splitlines()

    out: list[dict[str, Any]] = []
    tier: str | None = None
    cur: dict[str, Any] | None = None
    field: str | None = None

    def next_nonblank(idx: int) -> str:
        for k in range(idx + 1, len(lines)):
            if lines[k].strip():
                return lines[k].strip()
        return ""

    def flush() -> None:
        nonlocal cur
        if cur:
            for k in ("observation", "corrective_action"):
                cur[k] = re.sub(r"\s+", " ", cur[k]).strip()
            out.append(cur)
        cur = None

    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s:
            continue

        tm = _TIER_RE.search(s)
        if tm:
            flush()
            tier, field = _norm_tier(tm.group(1)), None
            continue

        # A violation starts with its item number alone on a line, then "<code> - <title>".
        if re.fullmatch(r"\d{1,2}", s) and _CODE_RE.match(next_nonblank(i)):
            flush()
            cur = {
                "seq": len(out) + 1, "item_no": int(s), "code": None, "title": None, "tier": tier,
                "is_repeat": False, "cdi": False, "observation": "", "corrective_action": "",
            }
            field = None
            continue

        if cur is None:
            continue

        cm = _CODE_RE.match(s)
        if cm and cur["code"] is None:
            cur["code"], cur["title"] = re.sub(r"\s+", "", cm.group(1)).rstrip("-"), cm.group(2).strip()
            continue

        if set(s.split()) <= {"REPEAT", "CDI", "COS"}:
            cur["is_repeat"] = cur["is_repeat"] or "REPEAT" in s
            cur["cdi"] = cur["cdi"] or "CDI" in s.split()
            continue

        if s.startswith("Observation:"):
            field = "observation"
            cur[field] += " " + s[len("Observation:"):]
        elif s.startswith("Corrective Action:"):
            field = "corrective_action"
            cur[field] += " " + s[len("Corrective Action:"):]
        elif field:
            cur[field] += " " + s
    flush()
    return out


# ------------------------------------------------------------- item-level flags

TIER_RANK = {"core": 1, "priority foundation": 2, "priority": 3}


def attach_item_flags(grid: dict[int, dict[str, Any]], violations: list[dict[str, Any]]) -> None:
    """Roll each item's violations up onto the grid item (in place).

    The grid itself only says IN/OUT; REPEAT and CDI are printed on the violations. An item can carry
    several citations, so: tier = most severe, is_repeat = any repeat, all_cdi = every citation corrected.
    Items with no parsed violations get tier/is_repeat/all_cdi = None.
    """
    by_item: dict[int, list[dict[str, Any]]] = {}
    for v in violations:
        by_item.setdefault(v["item_no"], []).append(v)
    for n, item in grid.items():
        vs = by_item.get(n, [])
        item["n_violations"] = len(vs)
        if not vs:
            item["tier"] = item["is_repeat"] = item["all_cdi"] = None
            continue
        item["tier"] = max((v["tier"] for v in vs), key=lambda t: TIER_RANK.get(t or "", 0))
        item["is_repeat"] = any(v["is_repeat"] for v in vs)
        item["all_cdi"] = all(v["cdi"] for v in vs)


# ---------------------------------------------------------------------- free text

def _section(text: str, start_pat: str, end_pat: str | None) -> str | None:
    m = re.search(start_pat, text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    if end_pat:
        e = re.search(end_pat, rest, re.M)
        if e:
            rest = rest[: e.start()]
    rest = re.sub(r"\f", "\n", rest)
    return rest.strip() or None


# -------------------------------------------------------------------------- entry

def parse_report(text: str) -> dict[str, Any]:
    header = parse_header(text)
    fu = _search(r"Follow-up Inspection Required:\s*(Yes|No)", text, re.I)
    header["follow_up_required"] = None if fu is None else fu.lower() == "yes"
    grid = parse_grid(text)
    violations = parse_violations(text)
    attach_item_flags(grid, violations)

    notes: list[str] = []
    # Risk-factor items 1-29 must always be present. Some report variants omit the Good Retail
    # Practices grid (30-56) entirely; that's acceptable only if the header says 0 are out, which
    # the counts_match cross-check below enforces. A *partial* GRP grid is always a parse problem.
    present_grp = {n for n in grid if n > LAST_RISK_FACTOR_ITEM}
    expected = set(range(1, LAST_RISK_FACTOR_ITEM + 1))
    if present_grp:
        expected |= set(range(LAST_RISK_FACTOR_ITEM + 1, N_ITEMS + 1))
    elif grid:
        notes.append("no Good Retail Practices grid in this report variant")
    missing = sorted(expected - set(grid))
    if missing:
        notes.append(f"grid missing items: {missing}")

    rf_calc = sum(1 for n, v in grid.items() if n <= LAST_RISK_FACTOR_ITEM and v["status"] == "OUT")
    grp_calc = sum(1 for n, v in grid.items() if n > LAST_RISK_FACTOR_ITEM and v["status"] == "OUT")
    counts_match = None
    if header["rf_out"] is not None and header["grp_out"] is not None:
        counts_match = (rf_calc == header["rf_out"]) and (grp_calc == header["grp_out"])
        if not counts_match:
            notes.append(
                f"header counts rf={header['rf_out']} grp={header['grp_out']} "
                f"!= grid rf={rf_calc} grp={grp_calc}"
            )
    else:
        notes.append("header counts not found")

    if not violations and (header["rf_out"] or header["grp_out"]):
        notes.append("header reports violations but none were parsed")
    # Informational: OUT items with no citation, or citations against items the grid marks IN/N/O/N/A.
    bare_out = sorted(n for n, v in grid.items() if v["status"] == "OUT" and not v["n_violations"])
    if bare_out:   # a citation the violation parser failed to pick up: not safe to score
        notes.append(f"OUT items without violations: {bare_out}")
    stray = sorted({v["item_no"] for v in violations if grid.get(v["item_no"], {}).get("status") not in (None, "OUT")})
    if stray:
        notes.append(f"violations cited on non-OUT items: {stray}")

    ok = not missing and counts_match is True and not bare_out   # "no GRP grid"/stray notes are informational
    return {
        "parser_version": PARSER_VERSION,
        "header": header,
        "items": grid,
        "violations": violations,
        "temperature_text": _section(text, r"^\s*TEMPERATURE OBSERVATIONS\s*$", r"OBSERVATIONS AND CORRECTIVE ACTIONS"),
        # Stop at SCHEDULING: everything after it is boilerplate plus the inspector signature block.
        "comments": _section(text, r"^\s*COMMENTS\s*$", r"^\s*SCHEDULING\s*$"),
        "counts_match": counts_match,
        "status": "ok" if ok else "mismatch",
        "notes": "; ".join(notes) or None,
    }
