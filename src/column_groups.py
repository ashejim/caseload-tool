"""Grouping + search aliases for the column picker (action-editor filters and
the caseload viewer): turns ~150 caseload columns into labelled groups and lets
the search box match a group name or a common nickname ("EA", "ACI", "pre").

Pure helpers, no Tk. Columns are display names (caseload_csv.display_for_column).
"""
from __future__ import annotations

import re

# Ordered (group, keywords) — the FIRST group with a matching keyword wins, so
# specific groups come before broad ones ("course" appears everywhere).
_RULES = [
    ("Tasks & assessments", ("task", "pre-a", "pre-assess", "preassess", "oa ",
                             "oa:", "objective assess", "performance assess",
                             "pa ", "pa:", "assessment", "competenc", "score",
                             "coaching", "attempt", "approval", "ema ", "cpt")),
    ("Momentum & risk", ("momentum", "risk", "never attempted", "momi",
                         "stalled", "trend")),
    ("Engagement & signals", ("activity", "signal", "engagement", "active elsewhere",
                              "finished other", "faculty discussion", "portal",
                              "login")),
    ("Degree plan & program", ("degree", "plan", "program", "courses left",
                               "cus", "next planned", "last course in term",
                               "program mentor", "mentoremail", "pm ",
                               "college", "sap", "standing")),
    ("Outreach & follow-up", ("last contact", "last assigned ci", "last action",
                              "follow", "note", "essential action", "contact pref",
                              "days since last", "coursecontact", "texting",
                              "opt")),
    ("Student & contact", ("name", "student id", "studentid", "email", "phone",
                           "mobile", "timezone", "time zone", "pronoun", "city",
                           "state", "address", "pidm", "contact id")),
    ("Course & term", ("course", "term", "start", "end date", "enroll", "ic ",
                       "icend", "status", "effective end", "days until",
                       "registration", "instructor", "mentor", "ci ")),
]
GROUP_ORDER = [g for g, _ in _RULES] + ["Other"]

# Extra words a column answers to in search (lower-case substrings of the
# display name → aliases).
_ALIASES = [
    ("essential action", "ea"),
    ("last assigned ci contact", "aci last contact ci"),
    ("mycoursecontact", "aci last contact ci"),
    ("course mentor", "ci aci instructor"),
    ("instructor", "ci aci"),
    ("pre-a", "pre preassessment pre-assessment"),
    ("pre-assess", "pre preassessment pre-a"),
    ("objective assess", "oa"),
    ("oa ", "objective assessment"),
    ("performance assess", "pa task"),
    ("program mentor", "pm"),
    ("momentum risk", "risk"),
    ("risk %", "risk"),
]


def group_for(label: str) -> str:
    """The picker group for a column display name ('Other' when nothing
    matches)."""
    s = f" {str(label or '').lower()} "
    for group, keys in _RULES:
        if any(k in s for k in keys):
            return group
    return "Other"


def search_text(label: str, group: str = "") -> str:
    """What the search box matches: label (+ its camelCase words) + group +
    nicknames (lower-case)."""
    raw = str(label or "")
    s = f" {raw.lower()} "
    camel = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw).lower()
    extra = [a for k, a in _ALIASES if k in s]
    return " ".join([s.strip(), camel, (group or group_for(label)).lower()] + extra)


def matches(query: str, text: str) -> bool:
    """Search-box match: every word of ``query`` must start a word of
    ``text`` — or, for 3+ letters, appear anywhere in it. (So "ea" finds the
    EA nickname but not the "ea" inside "preassessment".)"""
    words = re.findall(r"[a-z0-9%]+", str(text or "").lower())
    low = str(text or "").lower()
    for tok in re.findall(r"[a-z0-9%]+", str(query or "").lower()):
        if not any(w.startswith(tok) for w in words) and not (
                len(tok) >= 3 and tok in low):
            return False
    return True


def sections(all_cols, view_cols=None, sp_opts=None) -> list:
    """Picker sections [(header, [(label, key, search_text)])]: ★ the current
    view's columns, the success-path steps by course, then every column under
    its group (groups in GROUP_ORDER, columns A–Z inside)."""
    out = []
    allc = list(all_cols or [])
    vc = [c for c in (view_cols or []) if c in allc]
    if vc:
        out.append(("★ In current view",
                    [(c, c, search_text(c)) for c in vc]))
    by_course: dict = {}
    for o in sp_opts or []:
        by_course.setdefault(o["course"], []).append(
            (o["label"], o["column"],
             f"{str(o['label']).lower()} success path step {o['course'].lower()}"))
    for course in sorted(by_course):
        out.append((f"Success path · {course}", by_course[course]))
    grouped: dict = {}
    for c in allc:
        grouped.setdefault(group_for(c), []).append(c)
    for g in GROUP_ORDER:
        cols = sorted(grouped.get(g) or [], key=str.lower)
        if cols:
            out.append((g, [(c, c, search_text(c, g)) for c in cols]))
    return out
