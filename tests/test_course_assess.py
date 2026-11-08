"""Tests for src/course_assess.py + the history course_assess / preA tables.
Synthetic data only; temp DB. Run: python tests/test_course_assess.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _real_db_guard  # noqa: E402,F401
from src import course_assess as ca, history  # noqa: E402

REPORT = [
    {"arpId": 1, "assessmentCode": "PVXO", "attemptNumber": 1,
     "attemptDateString": "10/01/2026", "isPassed": False, "questions": [
         {"itemCode": "I1", "competencyNumber": "1.1", "pamsTopicId": 7,
          "isCorrect": True, "candidateResponse": "B", "itemType": "MC",
          "topQuestionText": "Which is fastest?", "items": [
              {"subQuestionText": "", "options": [
                  {"answerChoice": "A", "option": "x", "correctAnswer": False,
                   "studentAnswer": False},
                  {"answerChoice": "B", "option": "y", "correctAnswer": True,
                   "studentAnswer": True}]}]},
         {"itemCode": "I2", "competencyNumber": "1.2", "isCorrect": False,
          "candidateResponse": "A", "topQuestionText": "Q2", "items": []}]},
    {"arpId": 2, "assessmentCode": "PVXO", "attemptNumber": 2,
     "attemptDateString": "10/09/2026", "isPassed": True, "questions": [
         {"itemCode": "I1", "competencyNumber": "1.1", "isCorrect": True,
          "candidateResponse": "B", "topQuestionText": "Which is fastest?"}]},
]

COACHING = {"assessments": [
    {"arpId": "A77", "assessmentBannerCode": "KVO1", "testDate": "2026-10-20",
     "isPassed": False, "competencyRating": "Low",
     "numberOfItems": 60, "totalTimeInMinutes": 95,
     "competencies": [{"competencyNumber": "1.1", "competencyTitle": "Sorting",
                       "competencyRating": "Low", "assessmentPercentage": 40,
                       "percentWidth": 62,
                       "topics": [{"topicTitle": "Heaps", "isSuggestedStudy": True},
                                  {"topicTitle": "Lists",
                                   "isSuggestedStudy": False}]}],
     "badItemCodes": [{"itemCode": "Z9", "itemPoints": 1,
                       "itemTimeInSeconds": 12, "itemType": "MC"}]}]}


def test_parse_preA_report_items_and_answers():
    atts, items = ca.parse_preA_report(REPORT)
    assert [a["n"] for a in atts] == [1, 2] and not atts[0]["passed"]
    assert atts[0]["responses"][1] == {"item": "I2", "competency": "1.2",
                                       "topic": "", "correct": False,
                                       "answer": "A"}
    assert set(items) == {"I1", "I2"}
    opts = items["I1"]["text"]["subs"][0]["options"]
    assert [o["correct"] for o in opts] == [False, True]
    # The shared item text never carries a student's own answer.
    assert "studentAnswer" not in str(items) and "candidateResponse" not in str(items)
    assert ca.parse_preA_report(None) == ([], {})


def test_parse_coaching_and_engagement_and_history():
    c = ca.parse_coaching(COACHING)
    assert c[0]["arp"] == "A77" and c[0]["code"] == "KVO1"
    assert c[0]["minutes"] == 95 and c[0]["competencies"][0]["width"] == 62 and c[0]["competencies"][0]["suggested"] == ["Heaps"]
    assert c[0]["missed"][0] == {"item": "Z9", "points": 1, "seconds": 12,
                                 "type": "MC"}
    e = ca.parse_engagement({"engagedViews": 14, "previewViews": 2,
                             "lastViewDate": "10-30-2026"})
    assert e["engaged_views"] == 14 and e["last_view"] == "2026-10-30"
    h = ca.parse_plan_history({
        "completedTerms": [{"termNumber": 1, "courses": [
            {"courseCode": "C959", "title": "Discrete", "courseStatus": "Passed",
             "gradeDate": "05-01-2026"}]}],
        "transferCredits": {"courses": [{"courseCode": "C182"}]},
        "metadata": {"planName": "BSCS"}})
    assert h["completed"][0]["code"] == "C959" and h["transfer"] == ["C182"]
    assert ca.parse_plan_history(None) == {}


def test_persist_and_load_course_assess():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        atts, items = ca.parse_preA_report(REPORT)
        by = {"000123": {"pidm": "9", "cv": "777",
                         "assessments": {"oa": {"attempts": [{"n": 3}]}},
                         "engagement": {"engaged_views": 14},
                         "coaching": ca.parse_coaching(COACHING),
                         "plan_history": {"transfer": ["C182"]},
                         "preA_code": "PVXO", "preA_attempts": atts,
                         "preA_items": items},
              "456": {"pidm": "8", "cv": "", "plan_history": {}}}
        r = history.persist_course_assess("c949", by, db_path=path)
        assert r == {"students": 2, "items": 2, "responses": 3}
        got = history.load_course_assess("C949", db_path=path)
        assert got["123"]["coaching"][0]["minutes"] == 95
        assert got["456"]["assessments"] == {}
        # Re-import: items keep one row each; responses replace, not duplicate.
        r = history.persist_course_assess("C949", by, db_path=path)
        conn = history._connect(path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM preA_item").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM preA_response"
                                ).fetchone()[0] == 3
        finally:
            conn.close()
        assert history.course_assess_recent("c949", db_path=path) == {"123", "456"}
        assert history.course_assess_recent("D502", db_path=path) == set()
        history.mark_course_assess_imported("C949", db_path=path)
        assert history.course_assess_last_import("c949", db_path=path)
    finally:
        os.remove(path)


def test_cohort_upsert_keeps_departed_and_watch_targets():
    import json as _json
    from datetime import datetime as dt
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        row = lambda sid: {"student_id": sid, "wgu_id": "u" + sid, "pidm": "p" + sid}
        r = history.upsert_l360_cohort("C949", [row("1"), row("2"), row("3")],
                                       db_path=path, now=dt(2026, 11, 1))
        assert r == {"present": 3, "arrived": 3, "departed": 0, "returned": 0}
        r = history.upsert_l360_cohort("C949", [row("1"), row("2"), row("4")],
                                       db_path=path, now=dt(2026, 11, 6))
        assert (r["arrived"], r["departed"]) == (1, 1)          # 4 in, 3 out
        cur = {x["student_id"] for x in history.load_l360_cohort("C949", db_path=path)}
        allr = {x["student_id"]: x for x in history.load_l360_cohort(
            "C949", include_departed=True, db_path=path)}
        assert cur == {"1", "2", "4"} and allr["3"]["departed_at"].startswith("2026-11-06")
        assert allr["1"]["first_seen_at"].startswith("2026-11-01")
        r = history.upsert_l360_cohort("C949", [row("1"), row("2"), row("3"), row("4")],
                                       db_path=path, now=dt(2026, 11, 7))
        assert r["returned"] == 1 and not history.load_l360_cohort(
            "C949", include_departed=True, db_path=path)[2]["departed_at"]
        # Watch targets: 1 has an OA scheduled yesterday (near), 2 stored
        # long ago (stale), 3 fresh (skip), 4 never stored (new).
        oa_soon = {"oa": {"attempts": [{"n": 1, "scheduled": "2026-11-06",
                                        "result": ""}]}}
        history.persist_course_assess("C949", {
            "1": {"pidm": "p1", "assessments": oa_soon},
            "2": {"pidm": "p2", "assessments": {}}}, db_path=path,
            now=dt(2026, 10, 1))
        history.persist_course_assess("C949", {"3": {"pidm": "p3"}},
                                      db_path=path, now=dt(2026, 11, 7))
        t = history.course_watch_targets("C949", db_path=path,
                                         today=dt(2026, 11, 7, 9))
        assert [x[1] for x in t["near"]] == ["1"]
        assert [x[1] for x in t["new"]] == ["4"]
        assert [x[1] for x in t["stale"]] == ["2"]
        t = history.course_watch_targets("C949", cap=1, db_path=path,
                                         today=dt(2026, 11, 7, 9))
        assert t["stale"] == [] and len(t["new"]) == 1          # cap shared
        history.mark_course_watch(db_path=path)
        assert history.course_watch_last(db_path=path)
    finally:
        os.remove(path)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
