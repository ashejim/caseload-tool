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


def _coach_date(c) -> str:
    return _iso_us(c.get("date")) or str(c.get("date") or "")[:10]


def _comps(c) -> list:
    return [{"num": x.get("number"), "title": x.get("title") or "",
             "rating": _RATING.get(x.get("rating"), x.get("rating") or ""),
             "pct": _pct(x.get("width"))}
            for x in (c or {}).get("competencies") or []]


def _pct(v) -> Optional[int]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(f)) if 0 <= f <= 100 else None


def _topics(c) -> list:
    return list(dict.fromkeys(t for x in (c or {}).get("competencies") or []
                              for t in x.get("suggested") or [] if t))[:8]


def course_types(rows, ca_all=None) -> dict:
    """{course: {"oa": bool, "pa": bool}} — which assessment types a course
    has, from the Any-Course roster (OA fields filled → OA; Task fields
    filled → PA, across all its students) and the assessment import (an OA
    or performance-assessment entry). Courses absent from both are absent
    (unknown → callers show both)."""
    out = {}
    for r in rows or []:
        c = str(r.get("CourseCode") or "").upper()
        if not c:
            continue
        t = out.setdefault(c, {"oa": False, "pa": False})
        if r.get("ObjectiveAssessmentCount") or r.get("LastObjectiveAssessmentDate"):
            t["oa"] = True
        if r.get("Task1") or r.get("Task1Status"):
            t["pa"] = True
    for (_sid, c), d in (ca_all or {}).items():
        t = out.setdefault(str(c).upper(), {"oa": False, "pa": False})
        kinds = ((d or {}).get("assessments") or {}).keys()
        if "oa" in kinds:
            t["oa"] = True
        if any("perf" in k for k in kinds):
            t["pa"] = True
    return out


def summarize(course: str, ca: Optional[dict], pre_rows: list,
              ri: Optional[dict], coach_base: str = "",
              types: Optional[dict] = None) -> Optional[dict]:
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
    coach = sorted(ca.get("coaching") or [], key=_coach_date)
    latest = None
    if coach:
        c = coach[-1]
        latest = {"date": _coach_date(c), "minutes": c.get("minutes"),
                  "ratings": _comps(c), "suggested": _topics(c)}
    # Per-attempt detail for the OA tabs: each sitting with its coaching
    # report (both oldest first; aligned from the latest backwards, so a
    # missing early report doesn't shift the rest).
    detail = [dict(t) for t in sit]
    off = len(detail) - len(coach)
    for i, c in enumerate(coach):
        j = i + off
        if 0 <= j < len(detail):
            detail[j].update(minutes=c.get("minutes"), comps=_comps(c),
                             topics=_topics(c), report_date=_coach_date(c))
    titles = {}
    for c in coach:
        for x in c.get("competencies") or []:
            if x.get("number"):
                titles.setdefault(x["number"], x.get("title") or "")
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
        "pre": {"sittings": pre_n, "rows": pre[-8:],
                "first_pct": next((p["pct"] for p in pre if p.get("pct") is not None), None),
                "last_pct": next((p["pct"] for p in reversed(pre)
                                  if p.get("pct") is not None), None),
                "last_date": pre[-1]["date"] if pre else "",
                "since_last_oa": sum(1 for p in pre if last_oa and p.get("date")
                                     and p["date"] > last_oa),
                "code": pre_a.get("code") or ""},
        "coaching": latest, "prep": prep, "attempts": detail,
        "titles": titles,
        "types": types or {},
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
    types = course_types(rows, raw["ca"])
    assess = {}
    keys = set(raw["ca"]) | set(ri)
    for sid, course in keys:
        if sid not in want:
            continue
        s = summarize(course, raw["ca"].get((sid, course)),
                      raw["pre"].get((sid, course), []), ri.get((sid, course)),
                      base, types.get(course))
        if s:
            assess.setdefault(sid, {})[course] = s
    return {"assess": assess, "cohort": raw["cohort"],
            "uuid_map": raw["uuid_map"], "types": types}


# ---------------------------------------------------------------------------
# Layout helpers for the in-app student view (mirror the Learner360 panel's
# JS: boxFlags / gridHtml in src/l360_overlay_js.py).
# ---------------------------------------------------------------------------
def box_flags(summary: Optional[dict], has_tasks: bool) -> dict:
    """Which parts of the Assessments box apply: {"pa", "oa", "pre"}.
    Course types known → only what applies; unknown → whatever has data."""
    a = summary or {}
    ty = a.get("types") or {}
    oa = a.get("oa") or {}
    return {
        "pa": bool(has_tasks) and ty.get("pa") is not False,
        "oa": ty.get("oa") is True or (ty.get("oa") is None and bool(
            oa.get("used") or (a.get("links") or {}).get("coaching"))),
        "pre": ty.get("oa") is not False and bool((a.get("pre") or {}).get("sittings")),
    }


def _lvl_pct(p, cut) -> str:
    if p is None:
        return ""
    c = cut or 74
    return "ok" if p >= c else ("mid" if p >= c - 10 else "bad")


def _lvl_rating(r) -> str:
    return {"Competent": "ok", "Exemplary": "ok", "Unsatisfactory": "bad"}.get(
        r, "mid" if r else "")


def grid(summary: Optional[dict]) -> dict:
    """Competency × attempt grid: {"cols": [label], "rows": [(label,
    [(text, level)])]} — columns Pre-A first/last + every OA attempt; rows
    Score then each competency; level ok/mid/bad/''."""
    a = summary or {}
    cut = a.get("cut")
    pre = [p for p in (a.get("pre") or {}).get("rows") or [] if p.get("pct") is not None]
    cols = []
    if pre:
        cols.append(("Pre " + str(pre[0].get("n")), "pre", pre[0]))
    if len(pre) > 1:
        cols.append(("Pre " + str(pre[-1].get("n")), "pre", pre[-1]))
    for t in a.get("attempts") or []:
        cols.append(("OA " + str(t.get("n")), "oa", t))
    if not cols:
        return {"cols": [], "rows": []}
    nums = list((a.get("titles") or {}).keys())
    for _l, kind, d in cols:
        if kind == "pre":
            nums += [k for k in (d.get("comps") or {}) if k not in nums]
    nums.sort()
    score = []
    for _l, kind, d in cols:
        v = d.get("pct") if kind == "pre" else d.get("score")
        if kind == "oa" and d.get("passed"):
            score.append((f"{v if v is not None else '—'} ✓", "ok"))
        else:
            score.append(("—" if v is None else f"{v}{'%' if kind == 'pre' else ''}",
                          _lvl_pct(v, cut)))
    rows = [("Score", score)]
    for n in nums:
        cells = []
        for _l, kind, d in cols:
            if kind == "pre":
                p = (d.get("comps") or {}).get(n)
                cells.append(("—" if p is None else f"{p}%", _lvl_pct(p, cut)))
            else:
                x = next((z for z in d.get("comps") or [] if z.get("num") == n), None)
                if not x:
                    cells.append(("—", ""))
                else:
                    txt = f"{x['pct']}%" if x.get("pct") is not None else (
                        (x.get("rating") or "")[:5])
                    cells.append((txt, _lvl_rating(x.get("rating"))))
        rows.append(((a.get("titles") or {}).get(n) or n, cells))
    return {"cols": [c[0] for c in cols], "rows": rows}
