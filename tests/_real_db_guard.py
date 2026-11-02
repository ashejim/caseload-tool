"""Test guard: make opening the REAL history.db impossible during a test run.

Background: on 2026-10-25 a test opened the real history.db through a default
``db_path`` while the app was closed; the app's at-rest encryption then treated
that stray file as data and wiped the history. Comparing the file's mtime
before/after a run can't tell a test write from the running app's own writes,
so instead every ``sqlite3.connect`` to the real path raises immediately and
names the call site.

Usage (top of a test module, before importing src modules is fine too):
    import _real_db_guard  # noqa: F401
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import HISTORY_DB  # noqa: E402

_REAL = os.path.normcase(os.path.abspath(str(HISTORY_DB)))
_orig_connect = sqlite3.connect


def _path_of(database) -> str:
    db = str(database)
    if db.startswith("file:"):                       # URI form
        db = db[5:].split("?", 1)[0]
    if db in (":memory:", ""):
        return ""
    return os.path.normcase(os.path.abspath(db))


def _guarded_connect(database, *args, **kwargs):
    if _path_of(database) == _REAL:
        raise AssertionError(
            "a test tried to open the REAL history.db — pass a temporary "
            "db_path (see tests/_real_db_guard.py)")
    return _orig_connect(database, *args, **kwargs)


if getattr(sqlite3.connect, "__name__", "") != "_guarded_connect":
    sqlite3.connect = _guarded_connect
