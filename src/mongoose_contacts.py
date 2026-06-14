"""Load a Mongoose segment/contacts CSV export and join it to the caseload to
get each student's Salesforce Contact Id (003…).

The export (Mongoose → Tools → Segments → a segment filtered to "contact id is
not empty" → ⋯ → Export) has columns:
    contactId, firstName, lastName, mobileNumber, optedOut, department, tags,
    Student Status, Course Instructor Name

There's no WGU Student Id in it, so we join to the caseload rows by:
  1. normalized mobile (caseload MobilePhone ↔ export mobileNumber) — unique, and
  2. full name ("firstName lastName" ↔ caseload Name) — the fallback that covers
     students with a BLANK caseload mobile (the whole point), skipping any name
     that isn't unique in the export (can't disambiguate duplicates).

Texting then searches Mongoose by the Contact Id (unique, blank-mobile-proof),
falling back to mobile when a student has no mapped id.
"""
from __future__ import annotations

import csv
from pathlib import Path

from src.text_message import normalize_phone

# Header that marks a file as a Mongoose contacts/segment export.
SIGNATURE_COLUMN = "contactId"


def is_contacts_export(path) -> bool:
    """True if `path` looks like a Mongoose contacts export (has a contactId
    column). Cheap header sniff for auto-detection."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            header = f.readline()
        return SIGNATURE_COLUMN.lower() in header.lower()
    except Exception:
        return False


def load_contacts(path) -> list[dict]:
    """Parse a Mongoose contacts export into normalized rows:
    {contact_id, first, last, mobile (10-digit or ''), opted_out, department}.
    Rows without a contactId are dropped."""
    out: list[dict] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            cid = (r.get("contactId") or "").strip()
            if not cid:
                continue
            out.append({
                "contact_id": cid,
                "first": (r.get("firstName") or "").strip(),
                "last": (r.get("lastName") or "").strip(),
                "mobile": normalize_phone(r.get("mobileNumber") or ""),
                "opted_out": (r.get("optedOut") or "").strip().lower()
                in ("true", "1", "yes", "y"),
                "department": (r.get("department") or "").strip(),
            })
    return out


def _full_name(first: str, last: str) -> str:
    return f"{first} {last}".strip().lower()


def build_contact_id_map(
    caseload_rows: list[dict], contacts: list[dict], *,
    include_opted_out: bool = False,
) -> dict[str, str]:
    """Map caseload StudentID -> Mongoose Contact Id.

    Joins by normalized mobile first, then by unique full name. Opted-out
    contacts are excluded unless `include_opted_out`. Names that occur more than
    once in the export are NOT used for the name fallback (ambiguous)."""
    usable = [c for c in contacts if include_opted_out or not c["opted_out"]]
    by_mobile: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    name_counts: dict[str, int] = {}
    for c in usable:
        if c["mobile"]:
            by_mobile.setdefault(c["mobile"], c)
        nm = _full_name(c["first"], c["last"])
        if nm:
            name_counts[nm] = name_counts.get(nm, 0) + 1
            by_name.setdefault(nm, c)

    out: dict[str, str] = {}
    for row in caseload_rows:
        sid = (row.get("StudentID") or "").strip()
        if not sid:
            continue
        mob = normalize_phone(row.get("MobilePhone") or "")
        match = by_mobile.get(mob) if mob else None
        if match is None:
            nm = (row.get("Name") or "").strip().lower()
            if nm and name_counts.get(nm, 0) == 1:
                match = by_name.get(nm)
        if match:
            out[sid] = match["contact_id"]
    return out


def load_and_map(path, caseload_rows: list[dict], **kwargs) -> dict[str, str]:
    """Convenience: load the export at `path` and return the StudentID ->
    Contact Id map. Empty dict if the file is missing/unreadable."""
    try:
        contacts = load_contacts(Path(path))
    except Exception:
        return {}
    return build_contact_id_map(caseload_rows, contacts, **kwargs)
