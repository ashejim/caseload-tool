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

from collections import Counter


def _first(name: str) -> str:
    n = (name or "").strip()
    if "," in n:                      # "Last, First"
        n = n.split(",", 1)[1].strip()
    return n.split()[0].lower() if n else ""


def _nsid(s) -> str:
    return str(s or "").strip().lstrip("0")


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
