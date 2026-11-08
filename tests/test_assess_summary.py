"""Tests for src/assess_summary.py (OA / pre-assessment summary) and
history.assess_raw. Synthetic data only; temp DB + temp roster file.
Run: python tests/test_assess_summary.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _real_db_guard  # noqa: E402,F401
from src import assess_summary as asum, history  # noqa: E402

HOVER_OA = ("Objective:\r\nVXO1  Not Passed   08/01/2026\r\n"
            "VXO1  Not Passed   09/01/2026\r\nVXO1  Not Passed   10/01/2026")
HOVER_PRE = "Pre-Assessment:\r\nPVXO  Passed          07/20/2026"
ROSTER = [{"StudentID": "000555555", "CourseCode": "C949", "PIDM": "77",
           "CourseStatus": "Registered", "ObjectiveAssessmentCount": "3",
           "ObjectiveAssessmentCounthoverText": HOVER_OA,
           "PreAssessmentCount": "1", "PreAssessmentCounthoverText": HOVER_PRE,
           "CourseCodeUrl": "https://my.wgu.edu/courses/course/40160025/for/77",
           "LastObjectiveAssessmentDateUrl":
               "https://my.wgu.edu/coaching-report/v4-0/studentPidm/77/"
               "assessmentId/5/courseVersionId/40160025"}]


def test_parse_hover_and_roster_index():
    got = asum.parse_hover(HOVER_OA)
    assert [a["date"] for a in got] == ["2026-08-01", "2026-09-01", "2026-10-01"]
    assert got[0] == {"code": "VXO1", "status": "Not Passed", "date": "2026-08-01"}
    assert asum.parse_hover("") == [] and asum.parse_hover(None) == []
    ri = asum.roster_index(ROSTER)[("555555", "C949")]
    assert ri["oa_count"] == 3 and len(ri["pre_list"]) == 1
    assert ri["links"]["cos"].endswith("/for/77")
    assert asum.coaching_base(ROSTER) == "https://my.wgu.edu/coaching-report/v4-0"


def _coach(rating2, topics):
    return {"date": "x", "minutes": 50, "competencies": [
        {"number": "4048.4.4", "title": "Explains Algorithms",
         "rating": rating2, "suggested": topics}]}


def test_summarize_merges_and_hints_attempt_four():
    ca = {"pidm": "77", "cv": "40160025",
          "assessments": {"oa": {"code": "VXO1", "id": "5", "cut": "74",
                                 "attempts": [
                                     {"n": 1, "status": "Not Passed", "score": "61",
                                      "result": "2026-08-01"},
                                     {"n": 2, "status": "Not Passed", "score": "66",
                                      "result": "2026-09-01"},
                                     {"n": 3, "status": "Not Passed", "score": "70",
                                      "result": "2026-10-01"},
                                     {"n": 4, "status": "", "scheduled": "2026-11-20",
                                      "approval": "Mentor Pre-Approved"}]},
                          "preA": {"code": "PVXO"}},
          "coaching": [_coach("APPROACHING_COMPETENCY", ["Heaps"]),
                       _coach("UNSATISFACTORY", ["Hash tables"]),
                       _coach("COMPETENT", [])]}
    pre = [{"n": 1, "date": "2026-07-20", "pct": 58, "passed": False},
           {"n": 2, "date": "2026-10-10", "pct": 80, "passed": True}]
    ri = asum.roster_index(ROSTER)[("555555", "C949")]
    s = asum.summarize("C949", ca, pre, ri)
    assert s["cut"] == 74 and s["oa"]["used"] == 3 and not s["oa"]["passed"]
    assert [t["score"] for t in s["oa"]["sittings"]] == [61, 66, 70]
    assert s["oa"]["next"] == {"scheduled": "2026-11-20",
                               "approval": "Mentor Pre-Approved"}
    assert (s["pre"]["sittings"], s["pre"]["first_pct"], s["pre"]["last_pct"]) == (2, 58, 80)
    assert s["pre"]["since_last_oa"] == 1               # 10-10 > 10-01
    assert s["coaching"]["ratings"][0]["rating"] == "Competent"
    assert s["prep"]["suggested"] == ["Hash tables"]    # attempt 2's topics
    assert s["prep"]["not_competent"] == ["Explains Algorithms"]
    assert s["links"]["cos"].endswith("/for/77")
    # Roster only (no import): attempts from the hover list, no scores.
    s = asum.summarize("C949", None, [], ri)
    assert s["oa"]["used"] == 3 and s["oa"]["sittings"][0]["score"] is None
    assert s["prep"] is None and s["pre"]["sittings"] == 1
    # Import only (no roster): links are built.
    s = asum.summarize("C949", ca, [], None,
                       "https://my.wgu.edu/coaching-report/v4-0")
    assert s["links"]["cos"] == "https://my.wgu.edu/courses/course/40160025/for/77"
    assert s["links"]["coaching"].endswith("assessmentId/5/courseVersionId/40160025")
    assert asum.summarize("C949", None, [], None) is None


def test_load_from_temp_db_and_roster():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    rfd, rpath = tempfile.mkstemp(suffix=".json")
    os.close(rfd)
    try:
        with open(rpath, "w", encoding="utf-8") as f:
            json.dump(ROSTER, f)
        history.upsert_l360_cohort("C949", [{"student_id": "000555555",
                                             "wgu_id": "U-1", "pidm": "77",
                                             "owner": "A CI", "team": "T",
                                             "program": "BSCS"}],
                                   db_path=path, now=dt(2026, 11, 1))
        history.persist_course_assess("C949", {"555555": {
            "pidm": "77", "cv": "40160025",
            "assessments": {"oa": {"cut": "74", "attempts": [
                {"n": 1, "status": "Not Passed", "score": "61",
                 "result": "2026-08-01"}]}},
            "preA_code": "PVXO",
            "preA_attempts": [{"n": 1, "date": "07/20/2026 10:00:00 AM -07:00",
                               "passed": False, "responses": [
                                   {"item": "I1", "correct": True},
                                   {"item": "I2", "correct": False}]}]}},
            db_path=path)
        out = asum.load(["000123"], roster_path=rpath, db_path=path)
        assert out["uuid_map"] == {"u-1": "555555"}
        assert out["cohort"]["555555"][0]["owner"] == "A CI"
        s = out["assess"]["555555"]["C949"]
        assert s["oa"]["used"] == 3                     # roster count wins
        assert s["pre"]["first_pct"] == 50 and s["pre"]["rows"][0]["date"] == "2026-07-20"
        # Just one student (the in-app student view).
        one = asum.load(["555555"], only=["0555555"], roster_path=rpath,
                        db_path=path)
        assert set(one["assess"]) == {"555555"}
        none = asum.load(["000123"], only=["000123"], roster_path=rpath,
                         db_path=path)
        assert none["assess"] == {}
    finally:
        os.remove(path)
        os.remove(rpath)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
