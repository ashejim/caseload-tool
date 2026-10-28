"""Background Learner360 notes import (App._maybe_auto_l360_notes and the
_l360_bg_* batch loop) driven with a fake worker / Tk root — no GUI, no
browser. Checks: batches of _L360_BG_BATCH, yielding while the app is busy,
saving via history.persist_l360_notes(mark_done=False), and stamping the run
done exactly once at the end.

Run: python tests/test_l360_notes_bg.py
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import history, mongoose_contacts  # noqa: E402
from src.config import HISTORY_DB  # noqa: E402

# NEVER touch the real history DB from a test. The app's at-rest encryption
# treats a stray plaintext history.db (even an empty schema) as a crash
# leftover and later encrypts it OVER the real data — this test once caused
# exactly that (2026-10-25) via load_contact_ids' default db_path.
mongoose_contacts.load_contact_ids = lambda **kw: {}
_REAL_DB_STATE = (os.path.exists(HISTORY_DB),
                  os.path.getmtime(HISTORY_DB) if os.path.exists(HISTORY_DB)
                  else None)

_spec = importlib.util.spec_from_file_location(
    "launcher_bg_test", os.path.join(ROOT, "scripts", "launcher.py"))
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)
App = launcher.App


class FakeRoot:
    """root.after: queue callbacks; the test pumps them (delays ignored)."""
    def __init__(self):
        self.q, self.lock = [], threading.Lock()

    def after(self, ms, fn):
        if ms >= 3600_000:          # the 6 h re-arm: not within this test
            return
        with self.lock:
            self.q.append(fn)

    def pump(self, limit=500):
        for _ in range(limit):
            with self.lock:
                fn = self.q.pop(0) if self.q else None
            if fn is None:
                # Idle: wait for a background save thread to report back.
                saving = [t for t in threading.enumerate()
                          if t.name == "l360-notes-save"]
                if not saving:
                    return
                for t in saving:
                    t.join(5)
                continue
            fn()


class FakeWorker:
    def __init__(self):
        self.ready_event = threading.Event()
        self.ready_event.set()
        self.batches = []

    def submit_l360_notes_fetch(self, targets, on_done, surface=True,
                                background=False):
        assert background and not surface
        self.batches.append(len(targets))
        by = {sid: [{"l360_id": f"{sid}-1", "created_at":
                     "2026-10-01T10:00:00.000Z", "type": "Email to Student",
                     "body": "hi", "course": "C1"}] for _w, sid in targets}
        on_done({"ok": True, "by_student": by, "fail_statuses": {}})


def _app(db):
    a = types.SimpleNamespace()
    for name in ("_maybe_auto_l360_notes", "_start_l360_notes_bg",
                 "_l360_bg_step", "_l360_bg_got", "_l360_bg_saved",
                 "_l360_bg_finish"):
        setattr(a, name, getattr(App, name).__get__(a))
    a._L360_BG_BATCH = App._L360_BG_BATCH
    a.root, a.worker = FakeRoot(), FakeWorker()
    a.settings = types.SimpleNamespace(l360_auto_notes_days=7)
    a._offline, a._is_busy, a.logs = False, False, []
    a._append_log = lambda msg, error=None, **kw: a.logs.append(msg)
    return a


def test_background_import_batches_and_stamps_once():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db)
    real = (history.persist_l360_notes, history.mark_l360_notes_imported,
            history.l360_notes_last_import, history.l360_note_targets)
    calls = {"mark_done": [], "stamped": 0}
    try:
        def persist(by, **kw):
            calls["mark_done"].append(kw.get("mark_done"))
            return real[0](by, db_path=db, mark_done=kw.get("mark_done", True))

        def stamp(**kw):
            calls["stamped"] += 1
            real[1](db_path=db)

        history.persist_l360_notes = persist
        history.mark_l360_notes_imported = stamp
        history.l360_notes_last_import = lambda **kw: real[2](db_path=db)
        targets = [(f"w{i}", f"S{i}") for i in range(23)]
        history.l360_note_targets = lambda **kw: targets

        a = _app(db)
        a._is_busy = True                     # user busy → must wait
        a._maybe_auto_l360_notes()
        a.root.pump(limit=3)
        assert a.worker.batches == [], a.worker.batches
        a._is_busy = False
        a.root.pump()
        assert a.worker.batches == [10, 10, 3], (a.worker.batches, a.logs)
        assert set(calls["mark_done"]) == {False}
        assert calls["stamped"] == 1
        assert history.notes_count(db_path=db) == 23
        assert a._l360_bg is None
        assert any("23 new note(s)" in m for m in a.logs), a.logs
        # Not due again right after a completed run.
        a2 = _app(db)
        a2._maybe_auto_l360_notes()
        a2.root.pump(limit=2)
        assert a2.worker.batches == []
    finally:
        (history.persist_l360_notes, history.mark_l360_notes_imported,
         history.l360_notes_last_import, history.l360_note_targets) = real
        os.remove(db)


class FakeMomiWorker(FakeWorker):
    def __init__(self, n):
        super().__init__()
        self.n, self.steps = n, []

    def submit_l360_momi_step(self, step, arg, on_done):
        if step == "caseload":
            self.steps.append("caseload")
            rows = [{"student_id": f"S{i}", "wgu_id": f"w{i}"}
                    for i in range(self.n)]
            on_done({"ok": True, "rows": rows, "drift": [],
                     "watch": {"bundles": ["m.js"], "flags": {}, "keys": {}}})
        else:
            self.steps.append(len(arg))
            on_done({"ok": True, "students": [dict(r, momi={}) for r in arg],
                     "statuses": {"200": len(arg)}, "keys": {"mi": ["x"]},
                     "drift": []})


def test_background_momi_refresh_batches_then_saves_once():
    real = (history.persist_momi, history.momi_capture_status)
    saved = []
    try:
        history.persist_momi = lambda students, **kw: (
            saved.append(len(students)) or {"students": len(students),
                                              "weeks_new": 0, "weeks_updated": 0,
                                              "factor_rows": 0})
        history.momi_capture_status = lambda **kw: {"last_captured": None}
        a = types.SimpleNamespace()
        for name in ("_maybe_auto_l360_momi", "_start_l360_momi_bg",
                     "_l360_mbg_step", "_l360_mbg_got", "_l360_mbg_finish",
                     "_l360_momi_done"):
            setattr(a, name, getattr(App, name).__get__(a))
        a._L360_MOMI_BATCH = App._L360_MOMI_BATCH
        a.root, a.worker = FakeRoot(), FakeMomiWorker(45)
        a.settings = types.SimpleNamespace(l360_auto_momi_days=7)
        a._offline, a._is_busy, a.logs, a.watch = False, True, [], []
        a._append_log = lambda msg, error=None, **kw: a.logs.append(msg)
        a._l360_watch_update = lambda w, d: a.watch.append(w)
        a._set_idle = lambda: (_ for _ in ()).throw(
            AssertionError("background run must not touch busy state"))
        a._maybe_auto_l360_momi()
        a.root.pump(limit=3)
        assert a.worker.steps == [], a.worker.steps   # waits while busy
        a._is_busy = False
        a.root.pump()
        assert a.worker.steps == ["caseload", 20, 20, 5], a.worker.steps
        assert saved == [45]                           # one save at the end
        assert a.watch and a.watch[0]["keys"] == {"mi": ["x"]}
        assert a._l360_mbg is None
    finally:
        history.persist_momi, history.momi_capture_status = real


class FakeActivityWorker(FakeWorker):
    def __init__(self):
        super().__init__()
        self.windows = []

    def submit_l360_activity_fetch(self, targets, days, on_done, surface=True,
                                   background=False):
        assert background and not surface
        self.batches.append(len(targets))
        self.windows.append(days)
        on_done({"ok": True, "failed": 0, "by_student": {
            sid: [{"date": "2026-10-20", "category": "Coursework"}]
            for _w, sid in targets}})


def test_background_activity_window_batches_and_stamp():
    from datetime import datetime, timedelta
    real = (history.persist_l360_activity, history.mark_l360_activity_imported,
            history.l360_activity_last_import, history.l360_note_targets)
    calls = {"mark_done": [], "stamped": 0}
    try:
        history.persist_l360_activity = lambda by, **kw: (
            calls["mark_done"].append(kw.get("mark_done"))
            or {"events": len(by), "rows_new": len(by)})
        history.mark_l360_activity_imported = lambda **kw: calls.__setitem__(
            "stamped", calls["stamped"] + 1)
        last = (datetime.now() - timedelta(days=9)).isoformat(timespec="seconds")
        history.l360_activity_last_import = lambda **kw: last
        history.l360_note_targets = lambda **kw: [(f"w{i}", f"S{i}")
                                                 for i in range(43)]
        a = types.SimpleNamespace()
        for name in ("_maybe_auto_l360_activity", "_l360_abg_step",
                     "_l360_abg_got", "_l360_abg_saved"):
            setattr(a, name, getattr(App, name).__get__(a))
        a._L360_ACT_BATCH = App._L360_ACT_BATCH
        a.root, a.worker = FakeRoot(), FakeActivityWorker()
        a.settings = types.SimpleNamespace(l360_auto_activity_days=7)
        a._offline, a._is_busy, a.logs = False, True, []
        a._append_log = lambda msg, error=None, **kw: a.logs.append(msg)
        a._maybe_auto_l360_activity()
        a.root.pump(limit=3)
        assert a.worker.batches == []                  # waits while busy
        a._is_busy = False
        a.root.pump()
        assert a.worker.batches == [20, 20, 3], a.worker.batches
        assert set(a.worker.windows) == {16}           # 9 days since + 7
        assert set(calls["mark_done"]) == {False} and calls["stamped"] == 1
        assert a._l360_abg is None
        assert any("43 new row(s)" in m for m in a.logs), a.logs
    finally:
        (history.persist_l360_activity, history.mark_l360_activity_imported,
         history.l360_activity_last_import, history.l360_note_targets) = real


def test_manual_import_uses_background_batches_with_progress():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db)
    real = (history.persist_l360_notes, history.mark_l360_notes_imported)
    try:
        history.persist_l360_notes = lambda by, **kw: real[0](
            by, db_path=db, mark_done=kw.get("mark_done", True))
        history.mark_l360_notes_imported = lambda **kw: real[1](db_path=db)
        a = _app(db)
        a._start_l360_notes_bg([(f"w{i}", f"S{i}") for i in range(60)],
                               manual=True)
        a.root.pump()
        assert a.worker.batches == [10] * 6, a.worker.batches
        assert any("[l360] notes 50/60" in m for m in a.logs), a.logs
        assert any(m.startswith("✓ Learner360 notes (import)") for m in a.logs)
    finally:
        history.persist_l360_notes, history.mark_l360_notes_imported = real
        os.remove(db)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    now = (os.path.exists(HISTORY_DB),
           os.path.getmtime(HISTORY_DB) if os.path.exists(HISTORY_DB) else None)
    assert now == _REAL_DB_STATE, "a test created/modified the REAL history.db"
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
