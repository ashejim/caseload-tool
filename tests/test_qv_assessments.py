"""Renders the Caseload Tool's Assessments box (CaseloadPanel._qv_assessments)
in a hidden Tk window with synthetic summaries — every tab, a PA-only own
student and an off-caseload OA student with the course picker. No DB.
Run: python tests/test_qv_assessments.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _real_db_guard  # noqa: E402,F401
import customtkinter as ctk  # noqa: E402
from scripts import launcher  # noqa: E402
from src import assess_summary  # noqa: E402

OA = {"course": "C949", "cut": 74, "oa_code": "VXO1",
      "oa": {"used": 3, "max": 4, "passed": False,
             "next": {"scheduled": "2026-11-20", "approval": "Mentor Pre-Approved"},
             "sittings": []},
      "pre": {"sittings": 2, "first_pct": 58, "last_pct": 80, "since_last_oa": 1,
              "rows": [{"n": 1, "date": "2026-07-20", "pct": 58,
                        "comps": {"4048.4.4": 50}},
                       {"n": 2, "date": "2026-10-20", "pct": 80, "passed": True,
                        "comps": {"4048.4.4": 82}}]},
      "titles": {"4048.4.4": "Explains Algorithms"},
      "attempts": [{"n": 1, "date": "2026-08-01", "score": 61, "comps": [
          {"num": "4048.4.4", "title": "Explains Algorithms",
           "rating": "Approaching", "pct": 55}], "topics": ["Heaps"], "minutes": 50},
                   {"n": 2, "date": "2026-09-01", "score": 66},
                   {"n": 3, "date": "2026-10-01", "score": 70}],
      "prep": {"attempt": 2, "suggested": ["Hash tables"],
               "not_competent": ["Explains Algorithms"]},
      "types": {"oa": True, "pa": False},
      "links": {"cos": "https://my.wgu.edu/courses/course/1/for/2",
                "coaching": "https://my.wgu.edu/coaching-report/x"}}
PA = {"course": "C769", "cut": None, "oa": {"used": 0}, "pre": {},
      "attempts": [], "titles": {}, "types": {"oa": False, "pa": True},
      "links": {"cos": "https://my.wgu.edu/courses/course/9/for/8"}}


def _texts(w) -> list:
    out = []
    for c in w.winfo_children():
        try:
            t = c.cget("text")
            if isinstance(t, str) and t:
                out.append(t)
        except Exception:
            pass
        out += _texts(c)
    return out


def test_box_renders_pa_and_oa_with_tabs():
    root = ctk.CTk()
    root.withdraw()
    try:
        panel = object.__new__(launcher.CaseloadPanel)
        frame = ctk.CTkFrame(root)
        panel._qv_grid = frame
        badges = []
        panel._qv_task_badges = lambda r, row: badges.append(row.get("CourseCode"))
        panel._cell = lambda row, k: str((row or {}).get(k, "") or "")
        panel.app = types.SimpleNamespace(_open_wgu_url=lambda u: None,
                                          _refresh_one_assess=lambda s, c: None)
        real = assess_summary.load
        assess_summary.load = lambda ids, only=None: {"assess": {
            "123": {"C769": PA}, "555": {"C949": OA}}}
        try:
            # Own PA student: tasks inside the box, no OA / pre-A lines.
            n = panel._qv_assessments(0, "000123", row={"CourseCode": "C769"},
                                      show_tasks=True)
            assert n == 1 and badges == ["C769"]
            t = " | ".join(_texts(frame))
            assert "Assessments · C769" in t and "Course page" in t
            assert "OA:" not in t and "Pre-A" not in t
            for w in frame.winfo_children():
                w.destroy()
            # Off-caseload OA student, two courses → picker; OA course chosen.
            panel._qv_assess_course = {}
            panel._qv_assessments(0, "555", courses=["C949", "D278"])
            t = " | ".join(_texts(frame))
            assert "Assessments · C949 · VXO1 · cut 74" in t
            assert "3 of 4 used" in t and "attempt 4" in t and "Hash tables" in t
            assert "Pre 1" in t and "OA 3" in t and "55%" in t      # the grid
            assert "Coaching report" in t
            # Every tab renders.
            body = ctk.CTkFrame(root)
            f = assess_summary.box_flags(OA, False)
            for tab in ("Summary", "Pre-A", "OA 1", "OA 2", "OA 3"):
                for w in body.winfo_children():
                    w.destroy()
                panel._qv_assess_body(body, tab, OA, f, None, "C949")
                assert _texts(body), tab
            panel._qv_assess_body(body, "OA 1", OA, f, None, "C949")
            assert any("Explains Algorithms: Approaching · 55%" in x
                       for x in _texts(body))
            # A course with no data → a short note, no crash.
            panel._qv_assess_course = {"555": "D278"}
            for w in frame.winfo_children():
                w.destroy()
            panel._qv_assessments(0, "555", courses=["C949", "D278"])
            assert any("No assessment data" in x for x in _texts(frame))
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
