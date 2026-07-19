"""Tests for src.success_path.compute_steps — the per-step Due/Blocked/Skipped
status engine behind the student Success Path panel.

Regression focus: gate/skip_when predicates must be routed through the SAME
filter pipeline the batch engine uses (resolve display-name column -> CSV
header, then route "Task N" to the hidden Task{N}Status/Date/Count facet). A
condition like "Task 1 is Passed" that skips this routing looks up a
non-existent "Task 1" key, never matches, and strands the whole linear path as
Blocked for a student who has actually passed the task.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import success_path as sp  # noqa: E402


def _step(id, *, gate=None, skip_when=None):
    return SimpleNamespace(id=id, description=id, action=id,
                           gate=gate or [], skip_when=skip_when or [])


def _status(steps, row):
    return {s["id"]: s["status"]
            for s in sp.compute_steps(steps, row, {}, {})}


# The C769-shaped path: a welcome step that auto-skips once Task 1 is passed,
# then a linear chain of empty-gate steps.
def _welcome_path():
    return [
        _step("welcome", skip_when=[
            {"column": "Task 1", "op": "is", "value": "Passed"}]),
        _step("task2_prep"),
        _step("task3_prep"),
    ]


def test_task_status_skip_when_routes_to_facet():
    # Task 1 passed (status lives in the hidden Task1Status facet from the
    # scrape) -> welcome auto-skips, so the NEXT step is the actionable one.
    st = _status(_welcome_path(),
                 {"StudentID": "x", "Task1": "2026-07-17 (1)",
                  "Task1Status": "Passed"})
    assert st["welcome"] == sp.STATUS_SKIPPED, st
    assert st["task2_prep"] == sp.STATUS_DUE, st
    assert st["task3_prep"] == sp.STATUS_BLOCKED, st


def test_task_status_skip_when_not_passed_stays_due():
    # Task 1 not passed -> welcome does NOT skip; it's Due and the rest Blocked.
    st = _status(_welcome_path(),
                 {"StudentID": "x", "Task1Status": "In Process"})
    assert st["welcome"] == sp.STATUS_DUE, st
    assert st["task2_prep"] == sp.STATUS_BLOCKED, st


def test_task_status_skip_when_unknown_status_stays_due():
    # No scrape overlay: Task 1 status unknown -> can't skip, welcome stays Due.
    st = _status(_welcome_path(), {"StudentID": "x", "Task1": "2026-07-17 (1)"})
    assert st["welcome"] == sp.STATUS_DUE, st


def test_linear_cascade_first_due_rest_blocked():
    # Empty-gate steps, no completions: only the first is actionable.
    steps = [_step("a"), _step("b"), _step("c")]
    st = _status(steps, {"StudentID": "x"})
    assert st == {"a": sp.STATUS_DUE, "b": sp.STATUS_BLOCKED,
                  "c": sp.STATUS_BLOCKED}, st


def test_task_gate_routes_to_facet():
    # A non-empty gate using a Task-N status column is routed too: the step is
    # Due only when the facet matches (independent of linear order).
    steps = [_step("eot", gate=[
        {"column": "Task 2", "op": "is", "value": "Passed"}])]
    assert _status(steps, {"Task2Status": "Passed"})["eot"] == sp.STATUS_DUE
    assert _status(steps, {"Task2Status": "Returned"})["eot"] == \
        sp.STATUS_BLOCKED


def test_describe_source_phrasing():
    assert sp.describe_source("action:welcome-C769-batch") == \
        "by “welcome-C769-batch”"
    assert sp.describe_source("backfill:welcome-C769") == \
        "(backfilled from “welcome-C769”)"
    assert sp.describe_source("manual") == "manually"
    assert sp.describe_source("") == "manually"
    assert sp.describe_source(None) == "manually"
    assert sp.describe_source("weird") == "weird"  # unknown prefix passes through


def test_event_summary_what_and_when():
    detail = {"event": "completed", "source": "action:welcome-C769-batch",
              "occurred_at": "2026-07-17T10:20:05"}
    s = sp.event_summary(detail)
    assert s == "Completed by “welcome-C769-batch” · " \
                "Jul 17, 2026 10:20 AM", s


def test_event_summary_manual_and_dismissed():
    assert sp.event_summary(
        {"event": "dismissed", "source": "manual",
         "occurred_at": "2026-07-17T09:00:00"}
    ) == "Skipped manually · Jul 17, 2026 9:00 AM"


def test_event_summary_empty_when_no_event():
    assert sp.event_summary(None) == ""
    assert sp.event_summary({}) == ""


def test_step_status_detail_returns_latest_with_source_and_date():
    import os
    import tempfile
    from datetime import datetime
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # let _connect create it fresh
    try:
        sp.log_step("s1", "C769", "welcome", "completed",
                    source="action:welcome-C769-batch", db_path=path,
                    now=datetime(2026, 7, 17, 10, 20, 5))
        # A later manual reset must win (latest event per step).
        sp.log_step("s1", "C769", "welcome", "reset", source="manual",
                    db_path=path, now=datetime(2026, 7, 18, 8, 0, 0))
        d = sp.step_status_detail("s1", "C769", db_path=path)
        assert d["welcome"]["event"] == "reset", d
        assert d["welcome"]["source"] == "manual", d
        assert d["welcome"]["occurred_at"] == "2026-07-18T08:00:00", d
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
