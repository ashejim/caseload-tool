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

from src import history  # noqa: E402

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
    a._append_log = lambda msg, error=None: a.logs.append(msg)
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


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
