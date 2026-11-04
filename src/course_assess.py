"""Course assessment data from WGU's student-portal gateways (C949 work).

Pure parsing (no browser, no DB). The feeds (found by `l360discover:`,
routes/ids confirmed by `l360assessprobe:` 2026-11-04 — they must be called
from the student-portal course page with its token, keyed by PIDM):

  assessmentsWithUiState   every OA / pre-assessment attempt: score, passed,
                           dates, approval; cut score
  studentengagement        course-material views, last view
  pre-assessment-report    per attempt, per question: right/wrong, the
                           student's answer, competency, topic, item text
  coachingreport v4        per OA attempt: competency ratings, suggested-study
                           topics, missed items with time, total time

Pre-assessment ITEM TEXT is stored once per item (every student sees the same
items) and never leaves the local, encrypted DB.
"""
from __future__ import annotations

from typing import Optional

from src.learner360 import AI_GW, LX_GW, norm_date


# Pre-assessment REPORT codes seen in the course page's own calls (the
# assessments feed's pre-A code may differ) — tried after the feed's code.
PREA_REPORT_CODES = {"C949": ["PVXO"]}


def assessments_url(pidm: str, cv: str) -> str:
    return (f"{AI_GW}/assessments-service/v1/students/{pidm}/courses/{cv}/"
            "assessmentsWithUiState")


def engagement_url(pidm: str, cv: str) -> str:
    return f"{LX_GW}/studentengagement/v2/status/student/{pidm}/course/{cv}"


def preA_url(pidm: str, code: str) -> str:
    return f"{LX_GW}/pre-assessment-report/v2/students/{pidm}/assessments/{code}"


def coaching_url(pidm: str, oa_id: str, cv: str) -> str:
    return (f"{LX_GW}/coachingreport/v4/pidm/{pidm}/assessmentid/{oa_id}/"
            f"cv/{cv}")


def _n(v) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def parse_engagement(resp) -> dict:
    r = resp if isinstance(resp, dict) else {}
    return {
        "engaged_views": _n(r.get("engagedViews")),
        "preview_views": _n(r.get("previewViews")),
        "early_engaged_views": _n(r.get("earlyEngagedViews")),
        "last_view": norm_date(r.get("lastViewDate")),
        "start_date": norm_date(r.get("startDate")),
        "view_state": str(r.get("viewState") or ""),
    }


def _item_text(q: dict) -> dict:
    """The shared, student-independent part of a question: stem, sub-
    questions and options with the correct answer — WITHOUT the student's
    answer."""
    subs = []
    for it in q.get("items") or []:
        if not isinstance(it, dict):
            continue
        subs.append({
            "text": str(it.get("subQuestionText") or ""),
            "options": [{"choice": str(o.get("answerChoice") or ""),
                         "seq": o.get("answerSequence"),
                         "text": str(o.get("option") or ""),
                         "correct": o.get("correctAnswer")}
                        for o in it.get("options") or [] if isinstance(o, dict)],
        })
    return {"stem": str(q.get("topQuestionText") or ""), "subs": subs}


def parse_preA_report(resp) -> tuple:
    """(attempts, items) from a pre-assessment report.
    attempts: [{n, date, passed, responses: [{item, competency, topic,
               correct, answer}]}]
    items:    {item_code: {competency, topic, type, text: {...}}}"""
    attempts, items = [], {}
    for a in resp if isinstance(resp, list) else []:
        if not isinstance(a, dict):
            continue
        resps = []
        for q in a.get("questions") or []:
            if not isinstance(q, dict):
                continue
            code = str(q.get("itemCode") or q.get("itemKey") or "")
            if not code:
                continue
            resps.append({
                "item": code,
                "competency": str(q.get("competencyNumber") or ""),
                "topic": str(q.get("pamsTopicId") or ""),
                "correct": bool(q.get("isCorrect")),
                "answer": str(q.get("candidateResponse") or ""),
            })
            if code not in items:
                items[code] = {"competency": str(q.get("competencyNumber") or ""),
                               "topic": str(q.get("pamsTopicId") or ""),
                               "type": str(q.get("itemType") or ""),
                               "text": _item_text(q)}
        attempts.append({
            "n": a.get("attemptNumber"),
            "date": str(a.get("attemptDateString") or "")
            or str(a.get("attemptDate") or ""),
            "passed": bool(a.get("isPassed")),
            "code": str(a.get("assessmentCode") or ""),
            "responses": resps,
        })
    return attempts, items


def parse_coaching(resp) -> list:
    """Per OA attempt: [{arp, code, location, date, passed, rating, items,
    minutes,
    competencies: [{number, title, rating, pct, suggested: [topic titles]}],
    missed: [{item, points, seconds, type}]}]."""
    out = []
    r = resp if isinstance(resp, dict) else {}
    for a in r.get("assessments") or []:
        if not isinstance(a, dict):
            continue
        comps = []
        for c in a.get("competencies") or []:
            if not isinstance(c, dict):
                continue
            comps.append({
                "number": str(c.get("competencyNumber") or ""),
                "title": str(c.get("competencyTitle") or ""),
                "rating": str(c.get("competencyRating") or ""),
                "pct": _n(c.get("assessmentPercentage")),
                "suggested": [str(t.get("topicTitle") or "")
                              for t in c.get("topics") or []
                              if isinstance(t, dict) and t.get("isSuggestedStudy")],
            })
        out.append({
            # Per-attempt ids — may identify the OA FORM (C949 has two forms,
            # A/B: attempt 3 repeats the first form, 4 the second).
            "arp": str(a.get("arpId") or ""),
            "code": str(a.get("assessmentBannerCode") or ""),
            "location": str(a.get("locationType") or ""),
            "date": str(a.get("testDate") or ""),
            "passed": bool(a.get("isPassed")),
            "rating": str(a.get("competencyRating") or ""),
            "items": _n(a.get("numberOfItems")),
            "minutes": _n(a.get("totalTimeInMinutes")),
            "competencies": comps,
            "missed": [{"item": str(b.get("itemCode") or ""),
                        "points": _n(b.get("itemPoints")),
                        "seconds": _n(b.get("itemTimeInSeconds")),
                        "type": str(b.get("itemType") or "")}
                       for b in a.get("badItemCodes") or []
                       if isinstance(b, dict)],
        })
    return out


def parse_plan_history(v2) -> dict:
    """Prior + transferred courses from degree plan v2 (None if absent):
    {completed: [{code, title, term, status, grade_date}], transfer: [codes],
    plan_name}."""
    if not isinstance(v2, dict):
        return {}
    done = []
    for t in v2.get("completedTerms") or []:
        if not isinstance(t, dict):
            continue
        for c in t.get("courses") or []:
            if isinstance(c, dict) and c.get("courseCode"):
                done.append({"code": str(c["courseCode"]),
                             "title": str(c.get("title") or ""),
                             "term": t.get("termNumber"),
                             "status": str(c.get("courseStatus") or ""),
                             "grade_date": norm_date(c.get("gradeDate"))})
    tc = v2.get("transferCredits") if isinstance(v2.get("transferCredits"),
                                                 dict) else {}
    md = v2.get("metadata") if isinstance(v2.get("metadata"), dict) else {}
    return {"completed": done,
            "transfer": [str(c.get("courseCode")) for c in tc.get("courses") or []
                         if isinstance(c, dict) and c.get("courseCode")],
            "plan_name": str(md.get("planName") or "")}
