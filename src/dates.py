"""Date and timezone helpers — the single source of truth.

Small, pure functions with no GUI or Salesforce/Mongoose dependency, so they are
safe to import anywhere and easy to unit-test. Previously these lived (and were
duplicated) in ``scripts/launcher.py`` and ``src/text_message.py``; consolidated
here so there is one timezone map and one set of day-math helpers.
"""
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

# WGU caseload exports a bare timezone abbreviation (EST/CST/...). Map to IANA
# zones so daylight-saving is handled correctly.
TZ_ABBR_TO_IANA = {
    "EST": "America/New_York", "EDT": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "AKST": "America/Anchorage", "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",                      # Hawaii — no DST
    "ChS": "Pacific/Guam", "ChST": "Pacific/Guam",  # Chamorro — no DST
}

# Fallback timezone for students with a blank/unrecognized Timezone — schedule
# them as Mountain rather than skipping (MT is the safe default).
DEFAULT_TZ_ABBR = "MST"


def effective_tz(tz_abbr: str) -> str:
    """The student's tz abbreviation if recognized, else the MT default — so a
    student with no/unknown Timezone still gets scheduled (treated as Mountain)."""
    tz = (tz_abbr or "").strip()
    return tz if tz in TZ_ABBR_TO_IANA else DEFAULT_TZ_ABBR


def student_local_time(tz_abbr: str) -> str:
    """Current local time for a student given their CSV timezone abbreviation,
    e.g. 'EST' -> '2:14 PM'. Empty string if the tz is blank/unknown."""
    iana = TZ_ABBR_TO_IANA.get((tz_abbr or "").strip())
    if not iana:
        return ""
    try:
        now = datetime.now(ZoneInfo(iana))
        return now.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return ""


def days_until(date_str: str) -> Optional[int]:
    """Whole days from today until an ISO 'YYYY-MM-DD' date (negative if past).
    None if unparseable. Accepts a full timestamp too (only the leading
    YYYY-MM-DD is read)."""
    s = (date_str or "").strip()[:10]
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        return (d - datetime.now().date()).days
    except Exception:
        return None


def days_since(date_str: str) -> Optional[int]:
    """Whole days from an ISO date/timestamp until today (negative if the date
    is in the future). None if unparseable. The inverse of ``days_until`` — used
    for 'days since last contact / course start / last action'."""
    du = days_until(date_str)
    return None if du is None else -du


def to_iso_date(s: str) -> str:
    """Normalize a date string to ISO 'YYYY-MM-DD'. Accepts already-ISO and
    MM/DD/YYYY; unknown formats are returned unchanged (best-effort)."""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)   # MM/DD/YYYY
    if m:
        mo, d, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return s
