"""Build caseload rows from the intercepted getCaseLoadMainGridData JSON.

The caseload page fetches its whole grid from a Salesforce Aura endpoint; that
response is a SUPERSET of the CSV export (see the `griddiff:` diagnostic, which
proved 230/230 row coverage and 100% value parity on every field the app uses).
This module turns those grid rows into the same row-dict shape the app gets from
the CSV — same column names, values stringified to match the CSV's format — so
the caseload can be sourced from the JSON instead of a manually-configured CSV
export. The CSV stays the automatic fallback (see the launcher's health gate).

Design notes proven by griddiff:
- Grid field names are IDENTICAL to the CSV column names for everything the app
  reads (AcademicStanding, CourseCode, Name, Task1..Task15, Momentum, …), so the
  mapping is plain identity — no per-field rename table.
- Values need only TYPE coercion to match the CSV strings: bool -> 'false'/'true'
  (lowercase, as the CSV serializes them), integral float -> int ('23.0'->'23'),
  None -> ''.
- Nested objects/lists (SMFollowupNote{}, caseload{}, taskStreamAssessmentList[])
  are NOT flat columns — skipped here. CSV-only columns the app still needs
  (e.g. CourseFollowupNote, currently always empty) are overlaid from the CSV by
  the caller, so nothing regresses.
- `Icenddate` and other SPARSE fields exist only on some rows, so callers/indexes
  must take the UNION of field names across all rows, never a sample.
"""
from __future__ import annotations

import re

# Task cells (Task1..Task15 + the LatestTask summary) carry a status glyph the
# CSV export strips — the grid puts it INSIDE the cell (e.g. '2026-05-26✓ (1)')
# or leading (e.g. '⌛06/30/2026 (0)'). The cell is otherwise just a date +
# attempt count, so strip anything that isn't part of that date/count format to
# match the CSV byte-for-byte, then re-collapse whitespace.
_TASK_FIELD = re.compile(r"^(Task\d+|LatestTask)$")
_TASK_JUNK = re.compile(r"[^\d/()\s-]")
_WS = re.compile(r"\s+")


def grid_val_to_csv_str(field: str, v) -> str:
    """Coerce one grid value to the string form the CSV export would carry."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    if isinstance(v, (dict, list)):
        return ""            # nested — not a flat caseload column
    s = str(v)
    if _TASK_FIELD.match(field):
        s = _WS.sub(" ", _TASK_JUNK.sub("", s)).strip()
    return s


def all_grid_field_names(grid_by_key) -> set:
    """UNION of flat field names across every grid row (catches sparse fields
    like Icenddate that aren't present on all students)."""
    names: set = set()
    for g in grid_by_key.values():
        if isinstance(g, dict):
            names.update(
                f for f, v in g.items() if not isinstance(v, (dict, list)))
    return names


def build_caseload_rows(grid_by_key) -> list:
    """Convert accumulated grid rows ({(StudentID, CourseCode): row}) into a list
    of caseload row dicts keyed by CSV column names, values stringified to the
    CSV format. Every flat grid field is carried (a superset of the CSV). Nested
    objects/lists are skipped; the caller overlays any CSV-only columns it needs.
    """
    rows = []
    for g in grid_by_key.values():
        if not isinstance(g, dict):
            continue
        row = {}
        for f, v in g.items():
            if isinstance(v, (dict, list)):
                continue
            row[f] = grid_val_to_csv_str(f, v)
        rows.append(row)
    return rows
