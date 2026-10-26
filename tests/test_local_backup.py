"""Tests for src/local_backup.py — temp dirs ONLY; asserts the real data file
and the real default backup folder are untouched.

Run: python tests/test_local_backup.py
"""
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import local_backup as lb  # noqa: E402
from src.config import HISTORY_DB  # noqa: E402


def _state(p):
    p = Path(p)
    return (p.exists(), p.stat().st_mtime if p.exists() else None)


_REAL = (_state(HISTORY_DB), _state(str(HISTORY_DB) + ".enc"),
         _state(lb.default_backup_dir()))


def _data(d, size=1000, enc=True):
    d = Path(d)
    (d / ("history.db.enc" if enc else "history.db")).write_bytes(b"x" * size)
    (d / "vault.json").write_text('{"salt": "aa"}', encoding="utf-8")
    return d


def test_backup_copies_enc_readonly_with_vault_and_readme():
    with tempfile.TemporaryDirectory() as t:
        (Path(t) / "data").mkdir()
        data, bk = _data(Path(t) / "data"), Path(t) / "bk"
        r = lb.make_backup(data, bk, now=datetime(2026, 10, 26, 9, 0))
        assert r["status"] == "ok", r
        p = Path(r["path"])
        assert p.name == "history-20261026-090000.db.enc"
        assert not (p.stat().st_mode & stat.S_IWRITE)          # read-only
        assert (bk / "README.txt").exists()
        assert len(list(bk.glob("vault-*.json"))) == 1
        # Not due again within 20 h; due after.
        assert lb.make_backup(data, bk, now=datetime(2026, 10, 26, 20, 0)
                              )["status"] == "not_due"
        r2 = lb.make_backup(data, bk, now=datetime(2026, 10, 27, 9, 0))
        assert r2["status"] == "ok"
        assert len(list(bk.glob("vault-*.json"))) == 1          # unchanged vault
        try:                                                     # can't clobber
            Path(r2["path"]).write_bytes(b"oops")
            raise AssertionError("backup was writable")
        except PermissionError:
            pass


def test_shrunken_database_is_suspicious_and_blocks_pruning():
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        data.mkdir()
        bk = Path(t) / "bk"
        _data(data, size=10000)
        start = datetime(2026, 10, 1, 9, 0)
        for i in range(10):
            assert lb.make_backup(data, bk, keep_daily=3, keep_weekly=0,
                                  keep_monthly=0,
                                  now=start + timedelta(days=i))["status"] == "ok"
        before = {b["path"].name for b in lb.list_backups(bk)}
        assert len(before) == 3
        _data(data, size=100)                                   # wiped DB
        r = lb.make_backup(data, bk, keep_daily=3, keep_weekly=0,
                           keep_monthly=0, now=start + timedelta(days=11))
        assert r["status"] == "suspicious", r
        assert "-SUSPICIOUS" in Path(r["path"]).name
        names = {b["path"].name for b in lb.list_backups(bk)}
        assert before <= names                                  # nothing rotated
        # Still due daily: the suspicious copy doesn't count as a good backup.
        assert lb.backup_due(bk, now=start + timedelta(days=11, hours=1))


def test_retention_daily_weekly_monthly():
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        data.mkdir()
        bk = Path(t) / "bk"
        _data(data)
        start = datetime(2026, 6, 1, 9, 0)
        for i in range(120):                                    # ~4 months daily
            lb.make_backup(data, bk, now=start + timedelta(days=i))
        kept = lb.list_backups(bk)
        # 7 daily + up to 4 weekly + up to 6 monthly, with overlap.
        assert 7 <= len(kept) <= 7 + 4 + 6, len(kept)
        newest = kept[0]["when"]
        assert newest == start + timedelta(days=119)
        months = {(b["when"].year, b["when"].month) for b in kept}
        assert len(months) >= 4                                 # Jun..Sep kept
        # Foreign files are never touched.
        (bk / "my-notes.txt").write_text("keep me")
        lb.prune(bk)
        assert (bk / "my-notes.txt").exists()


def test_plaintext_fallback_and_no_data():
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        data.mkdir()
        bk = Path(t) / "bk"
        assert lb.make_backup(data, bk)["status"] == "no_data"
        _data(data, enc=False)
        r = lb.make_backup(data, bk, now=datetime(2026, 10, 26, 9, 0))
        assert r["status"] == "ok" and r["path"].endswith(".db"), r


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    now = (_state(HISTORY_DB), _state(str(HISTORY_DB) + ".enc"),
           _state(lb.default_backup_dir()))
    assert now == _REAL, "a test touched the REAL data or backup folder"
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
