"""Tests for src/cohort_view.py (the viewer's "👥 Course cohort" rows) and the
cohort student view. Synthetic data only.
Run: python tests/test_cohort_view.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _real_db_guard  # noqa: E402,F401
from src import cohort_view  # noqa: E402

COHORT = {"C949": [{"student_id": "555", "owner": "CI One", "team": "CS Apps",
                    "program": "BSCS", "momentum": "Low", "term_end": "2027-01-31",
                    "wgu_id": "u-555", "departed_at": None},
                   {"student_id": "777", "owner": "CI Two", "team": "CS Apps",
                    "program": "BSSWE", "momentum": "High", "term_end": "",
                    "wgu_id": "", "departed_at": "2026-11-07"}],
          "C769": []}
ROSTER = [{"StudentID": "000555", "CourseCode": "C949", "Name": "Ann A",
           "contactID": "003X", "CourseStatus": "Registered"},
          {"StudentID": "000888", "CourseCode": "C769", "Name": "Bo B",
           "CourseMentor": "Me", "CourseStatus": "Registered",
           "Task1": "2026-10-01✓ (1)", "Task2": "2026-10-20⌛ (1)"},
          {"StudentID": "000999", "CourseCode": "C769", "Name": "Cy C",
           "CourseStatus": "Passed"}]
COMP = lambda n, r, p: {"num": n, "title": {"4.4": "Explains Algorithms",  # noqa: E731
                                            "4.5": "Data Structure Impact"}[n],
                        "rating": r, "pct": p}
SUMM = {"555": {"C949": {
    "types": {"oa": True, "pa": False}, "cut": 74,
    "oa": {"used": 3, "passed": False, "next": {"scheduled": "2026-11-20",
                                                "approval": "Mentor Approved"},
           "sittings": [{"n": 1, "score": 65}, {"n": 2, "score": 70},
                        {"n": 3, "score": 66}]},
    "pre": {"sittings": 2, "first_pct": 58, "last_pct": 80, "since_last_oa": 1},
    "titles": {"4.4": "Explains Algorithms", "4.5": "Data Structure Impact"},
    "attempts": [{"n": 1, "comps": [COMP("4.4", "Approaching", 55),
                                    COMP("4.5", "Competent", 80)]},
                 {"n": 2, "comps": [COMP("4.4", "Approaching", 60)]},
                 {"n": 3, "comps": [COMP("4.4", "Unsatisfactory", None),
                                    COMP("4.5", "Approaching", 70)]}]}},
    "888": {"C769": {"types": {"oa": False, "pa": True}, "oa": {}, "pre": {}}}}


def test_build_rows_team_fallback_and_competency_columns():
    rows = cohort_view.build_rows(["C949", "C769"], COHORT, ROSTER, SUMM)
    by = {(r["StudentID"].lstrip("0"), r["CourseCode"]): r for r in rows}
    assert set(by) == {("555", "C949"), ("777", "C949"), ("888", "C769")}  # passer excluded
    a = by[("555", "C949")]
    assert a["Name"] == "Ann A" and a["CourseMentor"] == "CI One"
    assert a["Source"] == "Team (Learner360)" and a["Contact id"] == "003X"
    assert a["OA Used"] == 3 and a["OA Scores"] == "65 / 70 / 66"
    assert (a["OA 1"], a["OA 2"], a["OA 3"], a["OA 4"]) == (65, 70, 66, "")
    assert a["OA Last Score"] == 66 and a["OA Next"] == "2026-11-20"
    assert a["Explains Algorithms (all)"] == "55 / 60 / Unsat"
    assert a["Explains Algorithms (last)"] == "Unsat"
    assert a["Data Structure Impact (all)"] == "80 / — / 70"
    assert a["Data Structure Impact (last)"] == 70
    assert a["Weakest Competency"] == "Explains Algorithms"    # Unsat < 70%
    assert a["Pre-A First %"] == 58 and a["_uuid"] == "u-555"
    assert by[("777", "C949")]["Source"].endswith("· left")
    b = by[("888", "C769")]
    assert b["Source"].startswith("Course scan") and b["CourseMentor"] == "Me"
    assert b["PA Task 1"] == "2026-10-01✓ (1)" and b["OA Used"] == ""
    assert set(rows[0]) == set(rows[-1])                     # uniform columns


def test_course_instructor_filter_works_on_cohort_rows():
    """The viewer's 'Course Instructor' filter (the caseload's CI column)
    must match the cohort's CI column — it returned 0 rows live."""
    from src import caseload_csv, contextual_actions
    rows = cohort_view.build_rows(["C949", "C769"], COHORT, ROSTER, SUMM)
    headers = list(rows[0].keys())
    assert caseload_csv.resolve_column("Course Instructor", headers) == "CourseMentor"
    got = contextual_actions.gate_rows(
        [{"column": "Course Instructor", "op": "is not empty", "value": ""}], rows)
    assert len(got) == 3
    got = contextual_actions.gate_rows(
        [{"column": "Course Instructor", "op": "is any of", "value": "CI One, Me"}], rows)
    assert sorted(r["CourseMentor"] for r in got) == ["CI One", "Me"]


def test_cohort_quick_view_renders():
    import customtkinter as ctk
    from scripts import launcher
    from src import assess_summary
    root = ctk.CTk()
    root.withdraw()
    try:
        p = object.__new__(launcher.CaseloadPanel)
        p.qv_body, p.qv_notehead = ctk.CTkFrame(root), ctk.CTkFrame(root)
        p.qv_name = ctk.CTkLabel(root, text="")
        p._cell = lambda row, k: str((row or {}).get(k, "") or "")
        p._ensure_detail_height = lambda: None
        p._narrow = False
        p._notes_hint = lambda: None
        p._copy_icon_btn = lambda parent, v: ctk.CTkLabel(parent, text="⧉")
        p.app = types.SimpleNamespace(_open_wgu_url=lambda u: None,
                                      _refresh_one_assess=lambda s, c: None)
        rows = cohort_view.build_rows(["C949", "C769"], COHORT, ROSTER, SUMM)
        real = assess_summary.load
        assess_summary.load = lambda ids, only=None: {"assess": {
            "555": {"C949": dict(SUMM["555"]["C949"], links={}, course="C949")}}}
        try:
            row = next(r for r in rows if r["StudentID"] == "000555")
            p._show_cohort_quick_view(row)
            texts = []

            def walk(w):
                for c in w.winfo_children():
                    try:
                        t = c.cget("text")
                        if isinstance(t, str) and t:
                            texts.append(t)
                    except Exception:
                        pass
                    walk(c)
            walk(p.qv_body)
            info = " | ".join(texts)
            # Info (left/right columns) in qv_body; the Assessments box in the
            # notes column (qv_notehead) so it isn't pushed below the info.
            assert "CI One" in info and "Assessments" not in info
            texts.clear()
            walk(p.qv_notehead)
            joined = " | ".join(texts)
            assert "Assessments · C949" in joined
            assert "3 of 4 used" in joined
            assert p.qv_name.cget("text") == "👥 Ann A"
            pa = next(r for r in rows if r["StudentID"] == "000888")
            p._show_cohort_quick_view(pa)
            texts.clear()
            walk(p.qv_body)
            assert any("T1: 2026-10-01✓ (1)" in t for t in texts)
        finally:
            assess_summary.load = real
    finally:
        root.destroy()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
