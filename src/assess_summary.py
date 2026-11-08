"""Per-student OA / pre-assessment summary — for the Learner360 student panel
and the Caseload Tool student view.

Merges two sources, keyed by (student id without leading zeros, course):
  * the assessment import (history: course_assess + preA_response) — every
    attempt's SCORE, dates, approvals, the cut score, coaching reports
    (competency ratings, suggested-study topics), pre-assessment % per sitting
  * the Salesforce Any-Course roster capture (coursescan_roster.json) — the
    attempt list with statuses/dates, counts, and LINKS to the student's
    course (COS) page, OA coaching report and pre-assessment report

Pure functions + one loader (``load``). Values stay local: the summary feeds
the app's own UI and the app's own browser, nothing else.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

OA_MAX = 4            # standard attempt limit (5-6 need special permission)
_HOVER_RE = re.compile(r"^\s*([A-Z0-9]{3,6})\s+(.+?)\s+(\d{2}/\d{2}/\d{4})\s*$")
_RATING = {"APPROACHING_COMPETENCY": "Approaching", "COMPETENT": "Competent",
           "UNSATISFACTORY": "Unsatisfactory", "EXEMPLARY": "Exemplary"}
_COACH_RE = re.compile(r"(https://my\.wgu\.edu/coaching-report/[^/]+)/")


def _iso_us(s) -> str:
    try:
        return datetime.strptime(str(s).strip()[:10], "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def _nsid(s) -> str:
    return str(s or "").strip().lstrip("0")


def parse_hover(text) -> list:
    """'Objective:\\r\\nVXO1  Not Passed   10/04/2026\\r\\n…' →
    [{code, status, date (ISO)}] oldest first."""
    out = []
    for line in str(text or "").splitlines():
        m = _HOVER_RE.match(line)
        if m:
            out.append({"code": m.group(1), "status": m.group(2).strip(),
                        "date": _iso_us(m.group(3))})
    out.sort(key=lambda a: a["date"])
    return out


def _int(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def roster_index(rows) -> dict:
    """{(nsid, course): {...}} from the Any-Course roster rows."""
    out = {}
    for r in rows or []:
        sid, course = _nsid(r.get("StudentID")), str(r.get("CourseCode") or "").upper()
        if not sid or not course:
            continue
        out[(sid, course)] = {
            "course_status": str(r.get("CourseStatus") or ""),
            "oa_count": _int(r.get("ObjectiveAssessmentCount")),
            "oa_list": parse_hover(r.get("ObjectiveAssessmentCounthoverText")),
            "pre_count": _int(r.get("PreAssessmentCount")),
            "pre_list": parse_hover(r.get("PreAssessmentCounthoverText")),
            "pidm": str(r.get("PIDM") or ""),
            "links": {
                "cos": str(r.get("CourseCodeUrl") or ""),
                "coaching": str(r.get("LastObjectiveAssessmentDateUrl")
                                or r.get("ObjectiveAssessmentCountUrl") or ""),
                "preA": str(r.get("LastPreAssessmentDateUrl")
                            or r.get("PreAssessmentCountUrl") or ""),
            },
        }
    return out


def coaching_base(rows) -> str:
    """'https://my.wgu.edu/coaching-report/v4-0' (whatever version the roster's
    links use) — to build coaching links for students the roster lacks."""
    for r in rows or []:
        m = _COACH_RE.match(str(r.get("LastObjectiveAssessmentDateUrl") or ""))
        if m:
            return m.group(1)
    return ""


def summarize(course: str, ca: Optional[dict], pre_rows: list,
              ri: Optional[dict], coach_base: str = "") -> Optional[dict]:
    """One student's summary for one course, or None when there's nothing.
    ``ca`` = history.load_course_assess(...)[sid]; ``pre_rows`` = [{n, date,
    pct, passed}] from preA_response; ``ri`` = roster_index(...) entry."""
    ca, ri = ca or {}, ri or {}
    a = ca.get("assessments") or {}
    oa = a.get("oa") or {}
    pre_a = a.get("preA") or {}
    # OA sittings with a result (import), else the roster's list.
    sit = [{"n": t.get("n"), "date": str(t.get("result") or "")[:10],
            "score": _int(t.get("score")), "passed": bool(t.get("passed"))}
           for t in oa.get("attempts") or []
           if t.get("status") in ("Not Passed", "Passed") or t.get("passed")]
    sit.sort(key=lambda t: (t["n"] or 0, t["date"]))
    if not sit and ri.get("oa_list"):
        sit = [{"n": i + 1, "date": x["date"], "score": None,
                "passed": x["status"].lower() == "passed"}
               for i, x in enumerate(ri["oa_list"])]
    pending = [t for t in oa.get("attempts") or []
               if not t.get("result") and (t.get("scheduled") or t.get("approval"))]
    nxt = None
    if pending:
        p = pending[-1]
        nxt = {"scheduled": str(p.get("scheduled") or "")[:10],
               "approval": str(p.get("approval") or "")}
    used = max(len(sit), ri.get("oa_count") or 0)
    passed = any(t["passed"] for t in sit) or \
        str(ri.get("course_status") or "").lower() == "passed"
    # Pre-assessment sittings (% from the report), else the roster's list.
    pre = sorted(pre_rows or [], key=lambda p: (p.get("n") or 0))
    if not pre and ri.get("pre_list"):
        pre = [{"n": i + 1, "date": x["date"], "pct": None,
                "passed": x["status"].lower() == "passed"}
               for i, x in enumerate(ri["pre_list"])]
    pre_n = max(len(pre), ri.get("pre_count") or 0)
    last_oa = sit[-1]["date"] if sit else ""
    # Coaching: latest attempt + (for a coming 4th attempt) attempt 2's
    # topics — attempt 4 repeats attempt 2's form.
    coach = ca.get("coaching") or []
    latest = None
    if coach:
        c = coach[-1]
        latest = {
            "date": str(c.get("date") or "")[:10],
            "minutes": c.get("minutes"),
            "ratings": [{"num": x.get("number"), "title": x.get("title") or "",
                         "rating": _RATING.get(x.get("rating"), x.get("rating") or "")}
                        for x in c.get("competencies") or []],
            "suggested": list(dict.fromkeys(
                t for x in c.get("competencies") or []
                for t in x.get("suggested") or [] if t))[:8],
        }
    prep = None
    if used == OA_MAX - 1 and not passed and len(coach) >= 2:
        c2 = coach[1]
        prep = {"attempt": 2, "suggested": list(dict.fromkeys(
            t for x in c2.get("competencies") or []
            for t in x.get("suggested") or [] if t))[:8],
                "not_competent": [x.get("title") or x.get("number")
                                  for x in c2.get("competencies") or []
                                  if x.get("rating") not in ("COMPETENT",
                                                             "EXEMPLARY")]}
    pidm, cv = str(ca.get("pidm") or ri.get("pidm") or ""), str(ca.get("cv") or "")
    links = dict(ri.get("links") or {})
    if not links.get("cos") and pidm and cv:
        links["cos"] = f"https://my.wgu.edu/courses/course/{cv}/for/{pidm}"
    if not links.get("coaching") and coach_base and pidm and cv and oa.get("id"):
        links["coaching"] = (f"{coach_base}/studentPidm/{pidm}/assessmentId/"
                             f"{oa['id']}/courseVersionId/{cv}")
    links = {k: v for k, v in links.items() if v}
    if not (sit or pre or nxt or latest or links):
        return None
    return {
        "course": course, "cut": _int(oa.get("cut")), "oa_code": oa.get("code") or "",
        "oa": {"used": used, "max": OA_MAX, "passed": passed, "sittings": sit,
               "next": nxt},
        "pre": {"sittings": pre_n, "rows": pre[-6:],
                "first_pct": next((p["pct"] for p in pre if p.get("pct") is not None), None),
                "last_pct": next((p["pct"] for p in reversed(pre)
                                  if p.get("pct") is not None), None),
                "last_date": pre[-1]["date"] if pre else "",
                "since_last_oa": sum(1 for p in pre if last_oa and p.get("date")
                                     and p["date"] > last_oa),
                "code": pre_a.get("code") or ""},
        "coaching": latest, "prep": prep,
        "engagement": ca.get("engagement") or {},
        "links": links,
    }


def load(student_ids=None, *, roster_path: Optional[Path] = None,
         cohort_days: int = 30, db_path=None, only=None) -> dict:
    """Everything the panel/student view needs, in one pass:
    {"assess": {nsid: {course: summary}}, "cohort": {nsid: [{course, owner,
    team, program, momentum, term_end, departed}]}, "uuid_map": {uuid: nsid}}
    for ``student_ids`` (my caseload) + every cohort student (current, or
    departed within ``cohort_days``)."""
    from src import history
    from src.config import USER_CONFIG_DIR
    kw = {"db_path": db_path} if db_path else {}
    raw = history.assess_raw(student_ids, cohort_days=cohort_days, **kw)
    rows = []
    path = roster_path or (USER_CONFIG_DIR / "coursescan_roster.json")
    try:
        if Path(path).exists():
            rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = []
    want = set(raw["ids"])
    if only is not None:      # just these students (the in-app student view)
        want = {_nsid(s) for s in only} - {""}
    ri = roster_index([r for r in rows if _nsid(r.get("StudentID")) in want])
    base = coaching_base(rows)
    assess = {}
    keys = set(raw["ca"]) | set(ri)
    for sid, course in keys:
        if sid not in want:
            continue
        s = summarize(course, raw["ca"].get((sid, course)),
                      raw["pre"].get((sid, course), []), ri.get((sid, course)),
                      base)
        if s:
            assess.setdefault(sid, {})[course] = s
    return {"assess": assess, "cohort": raw["cohort"],
            "uuid_map": raw["uuid_map"]}
