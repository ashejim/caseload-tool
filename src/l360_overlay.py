"""CaseloadNotes overlay for Learner360 — data side (Stage 2, option A).

The app's own browser (Edge/Chromium, driven by browser_worker) shows
Learner360; an injected script (OVERLAY_JS in l360_overlay_js) decorates the
caseload list with CaseloadNotes badges. This module builds the per-student
SNAPSHOT the worker pushes into the page (push model — see the design doc
docs_private/LEARNER360_STAGE2_DESIGN.md, "Bridge mechanics").

Pure: no browser, no DB. Read-only by design (P0 shows data; changes nothing).
"""
from __future__ import annotations

from typing import Optional

# Task status labels as the caseload rows carry them (TASK_STATE_LABELS).
_TASK_KIND = {"passed": "pass", "returned": "return", "submitted": "pending",
              "in process": "pending", "pending": "pending"}


def _num(v) -> Optional[float]:
    try:
        return float(v) if str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _nsid(s) -> str:
    return str(s or "").strip().lstrip("0")


def _next_step(row: dict, steps: list) -> str:
    """Label of the first 'due' success-path step for this row's course (else
    the first 'blocked'); '' when none / no path. ``steps`` = [(col, label)]."""
    for want in ("due", "blocked"):
        for col, label in steps:
            if row.get(col) == want:
                return label + (" (blocked)" if want == "blocked" else "")
    return ""


def build_snapshot(rows: list, step_cols: Optional[dict] = None,
                   version: int = 0, extras: Optional[dict] = None) -> dict:
    """{"v": version, "students": {student id without leading zeros:
    [entry per course]}, "uuid_map", "notes", "momi"} from the caseload rows
    (after derived columns, task status and EAs are applied). ``step_cols`` =
    {course: [(sp column, step label)]} in path order. ``extras`` =
    history.overlay_extras(...) for the student panel (uuid → student map,
    recent notes, MOMI trend); omitted → empty."""
    step_cols = step_cols or {}
    out: dict = {}
    for r in rows or []:
        sid = _nsid(r.get("StudentID") or r.get("Student ID"))
        if not sid:
            continue
        course = str(r.get("CourseCode") or "").strip().upper()
        tasks = []
        for n in range(1, 16):
            status = str(r.get(f"Task{n}Status") or "").strip()
            date = str(r.get(f"Task{n}Date") or "").strip()
            if not status and not date:
                if n > 3:
                    continue
            kind = _TASK_KIND.get(status.lower(), "none" if not date
                                  else "pending")
            if n > 3 and kind == "none":
                continue
            tasks.append({"n": n, "status": status, "kind": kind,
                          "date": date,
                          "count": str(r.get(f"Task{n}Count") or "")})
        risk = _num(r.get("MomentumRisk"))
        out.setdefault(sid, []).append({
            "course": course,
            "name": str(r.get("Name") or ""),
            "tasks": tasks,
            "risk": risk,
            "momentum": str(r.get("Momentum") or ""),
            "next_step": _next_step(r, step_cols.get(course, [])),
            "days_contact": _num(r.get("DaysSinceLastContact")),
            "days_action": _num(r.get("DaysSinceLastAction")),
            "last_action": str(r.get("LastActionType") or ""),
            "ea": str(r.get("EssentialAction") or ""),
            "contact_pref": str(r.get("ContactPref") or ""),
            "pronouns": str(r.get("Pronouns") or ""),
            "followup_date": str(r.get("CourseFollowupDate") or ""),
            "followup_note": str(r.get("CourseFollowupNote") or "")[:200],
            "days_course_activity": _num(r.get("DaysSinceCourseActivity")),
            "signals": str(r.get("Signals", "") or ""),
            "term_left": str(r.get("TermCoursesLeft", "") or ""),
            "term_cus_left": _num(r.get("TermCUsLeft")),
            "next_course": str(r.get("NextPlannedCourse", "") or ""),
            "degree_pct": _num(r.get("DegreePctComplete")),
            "pm_email": str(r.get("ProgramMentorEmail", "") or ""),
            "pa_returns": _num(r.get("PAReturns")),
            "pa_passes": _num(r.get("PAPasses")),
            "steps": [{"label": lab, "status": str(r.get(col) or "")}
                      for col, lab in step_cols.get(course, [])
                      if str(r.get(col) or "") not in ("", "off-path")],
            "term_end": str(r.get("EffectiveEndDate")
                            or r.get("TermEndDate") or ""),
        })
    ex = extras or {}
    # OA / pre-assessment summaries (src/assess_summary) on caseload entries,
    # and entries for COURSE-COHORT students (e.g. C949 on my team, not my
    # caseload) so their Learner360 page gets a panel too.
    assess = ex.get("assess") or {}
    for sid, ents in out.items():
        for e in ents:
            a = (assess.get(sid) or {}).get(e["course"])
            if a:
                e["assess"] = a
    for sid, cl in (ex.get("cohort") or {}).items():
        have = {e["course"] for e in out.get(sid, [])}
        for c in cl:
            if c["course"] in have:
                continue
            e = {"course": c["course"], "name": "", "tasks": [], "risk": None,
                 "momentum": c.get("momentum") or "", "next_step": "",
                 "steps": [], "term_end": c.get("term_end") or "",
                 "cohort": c}
            a = (assess.get(sid) or {}).get(c["course"])
            if a:
                e["assess"] = a
            out.setdefault(sid, []).append(e)
    return {"v": version, "students": out,
            "uuid_map": dict(ex.get("uuid_map") or {}),
            "notes": dict(ex.get("notes") or {}),
            "momi": dict(ex.get("momi") or {})}
