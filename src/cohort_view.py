"""Course-cohort rows for the caseload viewer ("👥 Course cohort" view).

Every student my Learner360 TEAM has in the chosen courses (history
l360_cohort, with their assigned CI), one row per student + course, enriched
with the Salesforce Any-Course roster (name, contact, course status, tasks)
and the assessment summary (src/assess_summary: OA attempts/scores,
pre-assessment, per-competency results). A course with no team roster
pulled yet falls back to the Any-Course roster (all CIs) — flagged in the
row's Source column.

Pure: callers pass the inputs; ``load`` gathers them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from src import assess_summary as asum

_RATING_RANK = {"Unsatisfactory": 1, "Approaching": 2, "Competent": 3,
                "Exemplary": 4}


def _nsid(s) -> str:
    return str(s or "").strip().lstrip("0")


def _short(title: str) -> str:
    t = str(title or "").strip()
    return t if len(t) <= 28 else t[:27] + "…"


def build_rows(courses, cohort: dict, roster_rows: list,
               summaries: dict) -> list:
    """``cohort`` = {course: [l360_cohort rows]}, ``roster_rows`` = Any-Course
    roster dicts, ``summaries`` = {nsid: {course: assess summary}}. →
    list of row dicts (internal keys start with '_')."""
    want = [str(c).strip().upper() for c in courses or [] if str(c).strip()]
    roster = {}
    for r in roster_rows or []:
        roster[(_nsid(r.get("StudentID")), str(r.get("CourseCode") or "").upper())] = r
    # Competency titles per course (for the per-competency columns).
    titles: dict = {}
    for per in summaries.values():
        for c, s in (per or {}).items():
            for num, t in ((s or {}).get("titles") or {}).items():
                titles.setdefault(c, {}).setdefault(num, t or num)
    rows = []
    for course in want:
        members = cohort.get(course) or []
        source = "Team (Learner360)"
        if not members:
            members = [{"student_id": _nsid(r.get("StudentID")),
                        "owner": r.get("CourseMentor") or "", "team": "",
                        "program": r.get("ProgramName") or r.get("Programcode") or "",
                        "momentum": r.get("Momentum") or "",
                        "term_end": r.get("TermEndDate") or "", "wgu_id": "",
                        "departed_at": None}
                       for (sid, c), r in roster.items()
                       if c == course and str(r.get("CourseStatus") or "") != "Passed"]
            source = "Course scan (all CIs)"
        for m in members:
            sid = _nsid(m.get("student_id"))
            if not sid:
                continue
            ro = roster.get((sid, course)) or {}
            s = (summaries.get(sid) or {}).get(course) or {}
            oa, pre = s.get("oa") or {}, s.get("pre") or {}
            ty = s.get("types") or {}
            row = {
                "Name": ro.get("Name") or m.get("name") or "",
                "StudentID": ro.get("StudentID") or m.get("student_id") or sid,
                "CourseCode": course,
                # Same key as the caseload's CI column, so 'Course
                # Instructor' filters / saved views work in both.
                "CourseMentor": m.get("owner") or ro.get("CourseMentor") or "",
                "Team": m.get("team") or "",
                "Program": m.get("program") or ro.get("ProgramName") or "",
                "Momentum": m.get("momentum") or ro.get("Momentum") or "",
                "TermEndDate": m.get("term_end") or ro.get("TermEndDate") or "",
                "Course Status": ro.get("CourseStatus") or "",
                "Last Activity": ro.get("LastAcademicActivityDate") or "",
                "Days Since Course Contact": ro.get("DaysSinceLastCourseContact") or "",
                "StudentEmail": ro.get("StudentEmail") or m.get("email") or "",
                "MobilePhone": ro.get("MobilePhone") or "",
                "Timezone": ro.get("Timezone") or "",
                "Source": source + (" · left" if m.get("departed_at") else ""),
            }
            # PA tasks (roster value already carries ✓/✗/⌛ + attempts).
            for n in range(1, 6):
                v = ro.get(f"Task{n}")
                if v or ty.get("pa"):
                    row[f"PA Task {n}"] = v or ""
            if ty.get("oa") or oa.get("used") or pre.get("sittings"):
                sits = oa.get("sittings") or []
                row["OA Used"] = (oa.get("used") or 0) if not oa.get("passed") else "passed"
                row["OA Scores"] = " / ".join(
                    "—" if t.get("score") is None else str(t["score"]) for t in sits)
                row["OA Last Score"] = next((t["score"] for t in reversed(sits)
                                             if t.get("score") is not None), "")
                for i in range(5):
                    row[f"OA {i + 1}"] = (sits[i].get("score")
                                          if i < len(sits) and sits[i].get("score")
                                          is not None else "")
                nx = oa.get("next") or {}
                row["OA Next"] = nx.get("scheduled") or ""
                row["OA Approval"] = nx.get("approval") or ""
                row["Pre-A Sittings"] = pre.get("sittings") or 0
                row["Pre-A First %"] = "" if pre.get("first_pct") is None else pre["first_pct"]
                row["Pre-A Last %"] = "" if pre.get("last_pct") is None else pre["last_pct"]
                row["Pre-A Since Last OA"] = pre.get("since_last_oa") or 0
                # Per competency: every attempt + the latest (sortable).
                worst = None
                for num, title in sorted((titles.get(course) or {}).items()):
                    vals, last = [], ""
                    for t in s.get("attempts") or []:
                        x = next((z for z in t.get("comps") or []
                                  if z.get("num") == num), None)
                        if not x:
                            vals.append("—")
                            continue
                        v = (x.get("pct") if x.get("pct") is not None
                             else (x.get("rating") or "")[:5])
                        vals.append(str(v))
                        last = v
                        rank = (x["pct"] if x.get("pct") is not None
                                else 25 * _RATING_RANK.get(x.get("rating"), 0))
                    if vals:
                        row[f"{_short(title)} (all)"] = " / ".join(vals)
                        row[f"{_short(title)} (last)"] = last
                        if last != "" and (worst is None or rank < worst[0]):
                            worst = (rank, title)
                    else:
                        row[f"{_short(title)} (all)"] = ""
                        row[f"{_short(title)} (last)"] = ""
                row["Weakest Competency"] = worst[1] if worst else ""
            row.update({"_cohort": True, "_course": course,
                        "_uuid": m.get("wgu_id") or "",
                        "Contact id": ro.get("contactID") or ""})
            rows.append(row)
    # Every row gets every column (the grid takes headers from row 0).
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return [{k: r.get(k, "") for k in cols} for r in rows]


def available_courses(db_path=None, roster_path: Optional[Path] = None) -> dict:
    """{course: {"team": n current team students, "scan": n roster rows}} —
    the courses the cohort picker offers."""
    from src import history
    from src.config import USER_CONFIG_DIR
    kw = {"db_path": db_path} if db_path else {}
    out: dict = {}
    for c, n in history.cohort_course_counts(**kw).items():
        out.setdefault(c, {"team": 0, "scan": 0})["team"] = n
    path = roster_path or (USER_CONFIG_DIR / "coursescan_roster.json")
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8")) \
            if Path(path).exists() else []
    except (OSError, ValueError):
        rows = []
    for r in rows:
        c = str(r.get("CourseCode") or "").upper()
        if c:
            out.setdefault(c, {"team": 0, "scan": 0})["scan"] += 1
    return out


def load(courses, *, db_path=None, roster_path: Optional[Path] = None) -> list:
    """Rows for the cohort view (see build_rows), gathered from history + the
    roster capture + assessment summaries."""
    from src import history
    from src.config import USER_CONFIG_DIR
    kw = {"db_path": db_path} if db_path else {}
    want = [str(c).strip().upper() for c in courses or [] if str(c).strip()]
    cohort = {c: history.load_l360_cohort(c, **kw) for c in want}
    path = roster_path or (USER_CONFIG_DIR / "coursescan_roster.json")
    try:
        roster = json.loads(Path(path).read_text(encoding="utf-8")) \
            if Path(path).exists() else []
    except (OSError, ValueError):
        roster = []
    roster = [r for r in roster if str(r.get("CourseCode") or "").upper() in want]
    ids = {_nsid(r["student_id"]) for c in want for r in cohort[c]}
    ids |= {_nsid(r.get("StudentID")) for r in roster}
    ids.discard("")
    data = asum.load(sorted(ids), only=sorted(ids), roster_path=path, **kw)
    return build_rows(want, cohort, roster, data.get("assess") or {})
