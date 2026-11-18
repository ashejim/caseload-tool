"""Light OA-approval check: target selection + attempts-only update
(src/history.py), the summary's pending-approval field (src/assess_summary),
the EA column fallback (src/l360_overlay) and the app job. Synthetic data;
temp DB only (never the real history.db). No browser.

Run: python tests/test_approval_check.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import _real_db_guard  # noqa: E402,F401
from src import assess_summary as asum, history, l360_overlay  # noqa: E402

FAIL = {"n": 1, "status": "Not Passed", "passed": False, "score": "61",
        "result": "2026-10-12", "approval": "", "pending": False}
REQ = {"n": 2, "status": "", "passed": False, "score": "", "result": "",
       "approval": "Student Requested Approval", "pending": True}
PASS = {"n": 1, "status": "Passed", "passed": True, "score": "80",
        "result": "2026-10-01", "approval": "", "pending": False}


def _seed(path, now):
    def rec(atts, cv="40160025"):
        return {"pidm": "77", "cv": cv, "assessments": {"oa": {
            "id": "9", "cut": "74", "attempts": atts}},
            "coaching": [{"date": "2026-10-13", "keep": True}]}
    history.persist_course_assess("C949", {
        "111": rec([FAIL, REQ]),            # failed + approval requested
        "222": rec([FAIL]),                 # failed, nothing requested
        "333": rec([PASS]),                 # passed → never checked
        "444": rec([]),                     # no attempts → never checked
        "555": rec([FAIL], cv=""),          # no course version → can't ask
    }, db_path=path, now=now)


def test_targets_and_attempts_only_update():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _seed(path, dt(2026, 11, 18, 8, 0))
        t = history.approval_check_targets(db_path=path, now=dt(2026, 11, 18, 12, 0))
        assert [x[0] for x in t] == ["111", "222"], t          # pending first
        assert t[0] == ("111", "C949", "77", "40160025")
        # Checked recently → skipped until min_hours have passed.
        assert history.approval_check_targets(
            db_path=path, now=dt(2026, 11, 18, 9, 0)) == []
        # Approval granted: attempts replaced, coaching + updated_at kept.
        granted = {"oa": {"id": "9", "cut": "74", "attempts": [
            FAIL, dict(REQ, approval="Approved", pending=False)]}}
        n = history.update_course_assess_attempts(
            "C949", {"111": granted, "999": granted, "222": {}},
            db_path=path, now=dt(2026, 11, 18, 12, 5))
        assert n == 1                      # unknown student / empty → no-op
        con = sqlite3.connect(path)
        row = con.execute("SELECT assessments_json, coaching_json, updated_at "
                          "FROM course_assess WHERE student_id='111'").fetchone()
        con.close()
        a = json.loads(row[0])
        assert a["oa"]["attempts"][1]["approval"] == "Approved"
        assert a["_checked_at"] == "2026-11-18T12:05:00"
        assert json.loads(row[1])[0]["keep"] is True and row[2] == "2026-11-18T08:00:00"
        # The check time now counts as "recently checked" for 111.
        t2 = history.approval_check_targets(db_path=path, now=dt(2026, 11, 18, 13, 0))
        assert [x[0] for x in t2] == ["222"], t2              # 111 just checked
    finally:
        os.remove(path)


def test_summary_and_ea_column_fallback():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    rfd, rpath = tempfile.mkstemp(suffix=".json")
    os.close(rfd)
    try:
        with open(rpath, "w", encoding="utf-8") as f:
            json.dump([], f)
        _seed(path, dt(2026, 11, 18, 8, 0))
        out = asum.load(["111", "222"], roster_path=rpath, db_path=path)
        ap = out["assess"]["111"]["C949"]["oa"]["approval"]
        assert ap == {"n": 2, "status": "Student Requested Approval",
                      "as_of": "2026-11-18T08:00"}
        assert out["assess"]["222"]["C949"]["oa"]["approval"] is None
        rows = [{"StudentID": "000111", "CourseCode": "C949", "EssentialAction": ""},
                {"StudentID": "000222", "CourseCode": "C949",
                 "EssentialAction": "Assessment approval"}]
        snap = l360_overlay.build_snapshot(rows, {}, version=1,
                                           extras={"assess": out["assess"]},
                                           list_cols=["@ea"])
        v = snap["students"]["111"][0]["cols"]["@ea"]
        assert v[0] == "OA approval requested" and v[3] == "app:refresh:C949"
        assert "as of 2026-11-18T08:00" in v[2] and "Not a live Essential Action" in v[2]
        # A real EA record wins over the assessment-data fallback.
        assert snap["students"]["222"][0]["cols"]["@ea"][:2] == ["Assessment approval", "mid"]
    finally:
        os.remove(path)
        os.remove(rpath)


def test_app_job_updates_and_logs_counts_only():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "launcher_ap_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    logs, pushed, sched, updated = [], [], [], []
    real_t, real_u = L.history.approval_check_targets, L.history.update_course_assess_attempts
    try:
        L.history.approval_check_targets = lambda **kw: [("111", "C949", "77", "cv")]
        L.history.update_course_assess_attempts = \
            lambda course, by: updated.append((course, sorted(by))) or len(by)

        class W:
            ready_event = types.SimpleNamespace(is_set=lambda: True)

            def submit_approval_check(self, items, on_done):
                on_done({"ok": True, "by_course": {"C949": {"111": {"oa": {
                    "attempts": [FAIL, REQ]}}}}})
        a = types.SimpleNamespace(
            settings=types.SimpleNamespace(approval_check_hours=3),
            worker=W(), _append_log=lambda m, **kw: logs.append(m),
            _push_overlay_snapshot=lambda: pushed.append(1),
            _diag_once=lambda k, m: logs.append(m),
            root=types.SimpleNamespace(after=lambda ms, fn=None: sched.append(ms)
                                       if ms else (fn() if fn else None)))
        a._maybe_approval_check = lambda: None
        L.App._maybe_approval_check(a)
        assert updated == [("C949", ["111"])] and pushed == [1]
        assert any("1 waiting on approval" in m for m in logs), logs
        assert not any("111" in m for m in logs)              # no ids in the log
        assert 3 * 3600_000 in sched                          # re-armed
        # Off → nothing runs.
        a.settings.approval_check_hours = 0
        updated.clear()
        L.App._maybe_approval_check(a)
        assert updated == []
    finally:
        L.history.approval_check_targets, L.history.update_course_assess_attempts = real_t, real_u


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
