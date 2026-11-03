"""Tests for src/contextual_actions.py — which actions fit a selection.
Synthetic actions/rows only. Run: python tests/test_contextual_actions.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _real_db_guard  # noqa: E402,F401
from src import contextual_actions as ca  # noqa: E402
from src.scenarios import (BatchConfig, NoteData, PathStep,  # noqa: E402
                           ScenarioConfig, SuccessPath)


def sc(name, **kw):
    return ScenarioConfig(name=name, hotkey="", close_tab_after=False, **kw)


PATHS = {"C769": SuccessPath(course="C769", steps=[
    PathStep(id="t2", description="Task 2 prep", action="Task1 passed flup C769"),
    PathStep(id="t3", description="Task 3 prep", action="Task 2 passed C769")])}

ACTIONS = [
    sc("Task1 passed flup C769", batch=BatchConfig(filters=[
        {"column": "Task 1", "op": "is", "value": "Passed"}])),
    sc("Task 2 passed C769", batch=BatchConfig(filters=[
        {"column": "Task 2", "op": "is", "value": "Passed"}])),
    sc("welcome-D502"),                                         # name → D502
    sc("C964 only", fire_filters=[
        {"column": "Course Code", "op": "is", "value": "C964"}]),
    sc("Override D342", notes=[NoteData(
        interaction_format="Single Interaction", interaction_type="x",
        course_code="", course_code_override="D342")]),
    sc("Email from ST"),                                        # general
]

ROWS = [
    {"StudentID": "1", "CourseCode": "C769", "Task1Status": "Passed",
     "Task1Date": "2026-10-01", "Task2Status": ""},
    {"StudentID": "2", "CourseCode": "C769", "Task1Status": "Passed",
     "Task1Date": "2026-10-03", "Task2Status": "Passed", "Task2Date": "2026-10-20"},
    {"StudentID": "3", "CourseCode": "C964"},
]


def test_course_inference():
    by = {a.name: ca.action_courses(a, PATHS) for a in ACTIONS}
    assert by["Task1 passed flup C769"] == {"C769"}      # success path
    assert by["welcome-D502"] == {"D502"}                 # name
    assert by["C964 only"] == {"C964"}                    # filter
    assert by["Override D342"] == {"D342"}                # note override
    assert by["Email from ST"] == set()                   # general


def test_single_course_selection_and_suggestion():
    nxt = {"1": "Task1 passed flup C769", "2": "Task 2 passed C769"}
    res = ca.applicable_actions(ACTIONS, ROWS[:2], success_paths=PATHS,
                                next_action_for=lambda r: nxt[r["StudentID"]])
    names = [a["name"] for a in res]
    # D502/C964/D342 actions are not offered for C769 students.
    assert set(names) == {"Task1 passed flup C769", "Task 2 passed C769",
                          "Email from ST"}, names
    t1 = next(a for a in res if a["name"] == "Task1 passed flup C769")
    t2 = next(a for a in res if a["name"] == "Task 2 passed C769")
    assert (t1["matched"], t1["total"], t1["suggested"]) == (2, 2, 1)
    assert (t2["matched"], t2["suggested"]) == (1, 1)    # only student 2 passed T2
    assert res[0]["suggested"] == 1 and res[-1]["name"] == "Email from ST"


def test_mixed_courses_only_common_actions():
    res = ca.applicable_actions(ACTIONS, ROWS, success_paths=PATHS)
    assert [a["name"] for a in res] == ["Email from ST"]


def test_batch_conditions_are_soft_fire_filters_hard():
    rows = [{"StudentID": "9", "CourseCode": "C769", "Task1Status": ""}]
    res = {a["name"]: a for a in
           ca.applicable_actions(ACTIONS, rows, success_paths=PATHS)}
    # Batch targeting is soft: still offered, nobody meets it.
    a = res["Task1 passed flup C769"]
    assert (a["matched"], a["total"]) == (0, 1) and a["conditions"]
    # A fire-filter (hard gate) action nobody passes is NOT offered.
    gated = [sc("Hard gated C769", fire_filters=[
        {"column": "Task 1", "op": "is", "value": "Passed"}])]
    assert ca.applicable_actions(gated, rows, success_paths=PATHS) == []
    assert ca.describe_conditions(a["conditions"]) == "Task 1 is Passed"
    assert (ca.describe_conditions([{"column": "sp:C769:t2", "op": "is",
                                     "value": "due"}])
            == "success-path step t2 is due")


def test_empty_selection():
    assert ca.applicable_actions(ACTIONS, [], success_paths=PATHS) == []


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
