"""Learner360 team caseloads as the inbox labeler's roster source.

The labeler's roster (coursescan_roster.json) comes from a slow Salesforce
"Any Course" browser scan. Learner360's Team filter returns every caseload
under the manager in a few API pages, with each learner's courses, caseload
owner (the CI) and — since the 2026-11-01 release — ``wguEmail``. It only has
ASSIGNED students, though; unassigned roster records (blank CourseMentor) still
need the course scan.

This module is pure (no browser / no DB): the coverage comparison used by the
``l360roster: dry`` diagnostic. Output is counts only — no student values.
"""
from __future__ import annotations

import re
from collections import Counter

_COURSE_RE = re.compile(r"^[A-Za-z]\d{3}$")       # C769, D502 …


def _first(name: str) -> str:
    n = (name or "").strip()
    if "," in n:                      # "Last, First"
        n = n.split(",", 1)[1].strip()
    return n.split()[0].lower() if n else ""


def _nsid(s) -> str:
    return str(s or "").strip().lstrip("0")


def display_name(name: str) -> str:
    """'Last, First' → 'First Last' (the roster's CourseMentor style, whose
    first word becomes the CI category); other forms unchanged."""
    n = (name or "").strip()
    if "," in n:
        last, first = n.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return n


def overlay(roster: list, team_rows: list, courses=None) -> tuple:
    """Merge the Learner360 TEAM roster (history.load_l360_team_roster rows:
    student_id, course_code, email, name, owner) over the course-scan roster.
    Learner360 holds the CURRENT assignment, so for a matching student+course:
      - blank CourseMentor ("Unassigned") → filled with the Learner360 owner;
      - a different CourseMentor (by first name) → replaced (reassignment);
    and team learners in these courses missing from the roster are ADDED.
    Nothing is ever removed — the course scan still covers other teams'
    students, unassigned students and departures. Original values are kept
    as CourseMentorScan for audit. Returns (merged_roster, stats)."""
    want = {c.strip().upper() for c in (courses or []) if c.strip()}
    by_key = {}
    for r in team_rows or []:
        course = (r.get("course_code") or "").strip().upper()
        if want and course not in want:
            continue
        owner = display_name(r.get("owner") or "")
        if not owner:
            continue
        rec = dict(r, owner=owner, course_code=course)
        by_key[(_nsid(r.get("student_id")), course)] = rec
        em = (r.get("email") or "").strip().lower()
        if em:
            by_key[("@" + em, course)] = rec
    st = Counter()
    merged, seen = [], set()
    for rec in roster or []:
        course = (rec.get("CourseCode") or "").strip().upper()
        sid = _nsid(rec.get("StudentID"))
        em = (rec.get("StudentEmail") or "").strip().lower()
        hit = by_key.get((sid, course)) or (
            by_key.get(("@" + em, course)) if em else None)
        if not hit:
            merged.append(rec)
            continue
        seen.add((_nsid(hit.get("student_id")), course))
        cur = (rec.get("CourseMentor") or "").strip()
        new = dict(rec)
        if not cur:
            new["CourseMentor"] = hit["owner"]
            new["CourseMentorScan"] = ""
            st["filled"] += 1
        elif _first(cur) != _first(hit["owner"]):
            new["CourseMentor"] = hit["owner"]
            new["CourseMentorScan"] = cur
            st["corrected"] += 1
        else:
            st["agreed"] += 1
        merged.append(new)
    for (key, course), rec in by_key.items():
        if key.startswith("@"):
            continue
        if (key, course) in seen:
            continue
        seen.add((key, course))
        merged.append({"StudentID": rec.get("student_id") or "",
                       "StudentEmail": rec.get("email") or "",
                       "Name": rec.get("name") or "",
                       "CourseCode": course,
                       "CourseMentor": rec["owner"],
                       "Source": "learner360"})
        st["added"] += 1
    return merged, {k: st.get(k, 0) for k in
                    ("agreed", "corrected", "filled", "added")}


def replay_log(l360_rows: list, log_entries: list, courses=None) -> dict:
    """Replay the labeler's past decisions (inbox_label_log.jsonl) against the
    Learner360 team roster: for each distinct sender, would a Learner360
    lookup by WGU email identify them, and with the same course + CI-first-name
    categories the course-scan roster gave? Counts only.

    Returns {senders, identified_before, identified_l360, both,
    same_categories, ci_differs, course_differs, newly_identified,
    lost (identified before, not by Learner360)}."""
    want = {c.strip().upper() for c in (courses or []) if c.strip()}
    by_email = {}
    for r in l360_rows or []:
        em = (r.get("email") or "").strip().lower()
        if em:
            by_email.setdefault(em, []).append(r)
    latest = {}
    for e in log_entries or []:
        s = (e.get("sender_smtp") or "").strip().lower()
        if s:
            latest[s] = e            # last decision per sender wins
    st = Counter()
    for sender, e in latest.items():
        st["senders"] += 1
        before = e.get("method") == "email"
        hits = [h for h in by_email.get(sender, [])
                if not want or {c.strip().upper() for c in h.get("courses")
                                or []} & want]
        st["identified_before"] += before
        st["identified_l360"] += bool(hits)
        if before and not hits:
            st["lost"] += 1
        if hits and not before:
            st["newly_identified"] += 1
        if before and hits:
            st["both"] += 1
            old = {str(c).strip() for c in (e.get("categories") or [])}
            new_courses = {c.strip().upper() for h in hits
                           for c in h.get("courses") or []}
            new_cis = {_first(h.get("owner")) for h in hits} - {""}
            old_courses = {c.upper() for c in old if _COURSE_RE.match(c)}
            old_cis = {c.lower() for c in old if not _COURSE_RE.match(c)} - {
                "unassigned", "unidentified"}
            course_ok = old_courses <= new_courses if old_courses else True
            ci_ok = (old_cis <= new_cis) if old_cis else True
            st["course_differs"] += not course_ok
            st["ci_differs"] += not ci_ok
            st["same_categories"] += course_ok and ci_ok
    keys = ("senders", "identified_before", "identified_l360", "both",
            "same_categories", "ci_differs", "course_differs",
            "newly_identified", "lost")
    return {k: st.get(k, 0) for k in keys}


def compare_coverage(l360_rows: list, roster: list, courses=None) -> dict:
    """How well Learner360 team caseloads cover the course-scan roster.

    ``courses``: the labeler's course list (None = every course in the
    roster). Returns counts:
      roster_assigned / roster_unassigned — roster records for those courses
        with / without a CourseMentor;
      found_by_id / found_by_email        — assigned roster records whose
        student appears in Learner360 (by Student ID / by @wgu email);
      ci_agree / ci_compared              — among found records, how often the
        Learner360 caseload owner's first name equals the CourseMentor's;
      unassigned_found                    — unassigned roster records present
        in Learner360 anyway;
      l360_rows / l360_students / l360_with_email / l360_in_courses —
        Learner360 side sizes; l360_not_in_roster — Learner360 learners in
        those courses that the roster lacks (e.g. newly assigned);
      rows_per_student — distribution of Learner360 rows per learner.
    """
    want = {c.strip().upper() for c in (courses or []) if c.strip()}

    def in_courses(codes) -> bool:
        return not want or bool({c.strip().upper() for c in codes} & want)

    by_id, by_email = {}, {}
    per_student = Counter()
    for r in l360_rows or []:
        sid = _nsid(r.get("student_id"))
        if sid:
            by_id.setdefault(sid, []).append(r)
            per_student[sid] += 1
        em = (r.get("email") or "").strip().lower()
        if em:
            by_email.setdefault(em, []).append(r)

    st = Counter()
    roster_ids = set()
    for rec in roster or []:
        course = (rec.get("CourseCode") or "").strip().upper()
        if want and course not in want:
            continue
        sid = _nsid(rec.get("StudentID"))
        roster_ids.add(sid)
        mentor = (rec.get("CourseMentor") or "").strip()
        hits = by_id.get(sid) or by_email.get(
            (rec.get("StudentEmail") or "").strip().lower()) or []
        if not mentor:
            st["roster_unassigned"] += 1
            if hits:
                st["unassigned_found"] += 1
            continue
        st["roster_assigned"] += 1
        if by_id.get(sid):
            st["found_by_id"] += 1
        if by_email.get((rec.get("StudentEmail") or "").strip().lower()):
            st["found_by_email"] += 1
        if hits:
            st["ci_compared"] += 1
            if any(_first(h.get("owner")) == _first(mentor) for h in hits):
                st["ci_agree"] += 1

    in_c = {_nsid(r.get("student_id")) for r in l360_rows or []
            if in_courses(r.get("courses") or [])}
    in_c.discard("")
    out = {k: st.get(k, 0) for k in (
        "roster_assigned", "roster_unassigned", "found_by_id",
        "found_by_email", "ci_compared", "ci_agree", "unassigned_found")}
    out.update({
        "l360_rows": len(l360_rows or []),
        "l360_students": len(per_student),
        "l360_with_email": sum(1 for r in l360_rows or []
                               if (r.get("email") or "").strip()),
        "l360_in_courses": len(in_c),
        "l360_not_in_roster": len(in_c - roster_ids),
        "rows_per_student": dict(Counter(per_student.values())),
    })
    return out
