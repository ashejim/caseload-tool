"""Local, versioned backups of the history database.

Why this exists: on 2026-10-25 the encrypted history (``history.db.enc``) was
overwritten with a near-empty database (a stray plaintext file was taken for a
crash leftover), and the only full copy turned out to be an old laptop copy.
This module keeps dated copies somewhere the app's own save path — and
development work in the project folder — never writes to.

Design:
- Taken at STARTUP, before the vault is unlocked or any job runs, so each copy
  is the state the last clean exit left behind. At most one per
  ``min_hours`` (default 20 h).
- Copies the ENCRYPTED file (``history.db.enc``) when encryption is on, so the
  backup is as protected as the original; falls back to the plaintext
  ``history.db`` only for installs that never enabled encryption. ``vault.json``
  (salt + verifier, needed to decrypt with the password) is copied alongside
  whenever it changes.
- Folder defaults to ``~/Documents/CaseloadNotes Backups`` — outside the
  project folder and, on this machine, on a different drive.
- Files are set READ-ONLY, so a stray script can't overwrite or delete them.
- Retention: newest ``keep_daily`` + newest per ISO week for ``keep_weekly``
  weeks + newest per month for ``keep_monthly`` months. Only files matching
  this module's own name pattern are ever pruned.
- A database under half the size of the newest backup is saved as
  ``…-SUSPICIOUS…`` and pruning is SKIPPED for that run, so a wiped database
  can never rotate good backups out. SUSPICIOUS files are never pruned.

Everything takes explicit paths (tests pass temp dirs); nothing here opens the
live database.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_NAME_RE = re.compile(r"^history-(\d{8})-(\d{6})(-SUSPICIOUS)?\.db(\.enc)?$")
_SHRINK_FRACTION = 0.5

_README = """CaseloadNotes — local backups of your caseload history
=====================================================

Each file history-YYYYMMDD-HHMMSS.db.enc is a copy of history.db.enc as it was
when the app STARTED at that time (i.e. the state left by the previous clean
exit). The files are encrypted exactly like the original: you need your
CaseloadNotes password (and the vault.json next to them) to open them.

Files are read-only on purpose. Files marked -SUSPICIOUS were much smaller than
the backup before them (possible data loss) — they are kept, never pruned, and
the good backups before them are not rotated away.

TO RESTORE
  1. Close CaseloadNotes.
  2. In the CaseloadNotes data folder (the project folder, or
     %APPDATA%\\caseload-notes for the packaged app), rename the current
     history.db.enc to history.db.enc.broken (keep it).
  3. Copy the backup you want there and rename it to history.db.enc.
     (If you changed your password since that backup, also copy the
     vault-*.json saved with it and rename it vault.json — keep the old one.)
  4. Make sure there is NO plain history.db file there, then start the app.
"""


def default_backup_dir() -> Path:
    return Path.home() / "Documents" / "CaseloadNotes Backups"


def _parse(name: str) -> Optional[datetime]:
    m = _NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def list_backups(backup_dir) -> list[dict]:
    """Backups in ``backup_dir``, newest first: [{path, when, size,
    suspicious}]."""
    d = Path(backup_dir)
    out = []
    if not d.is_dir():
        return out
    for p in d.iterdir():
        when = _parse(p.name)
        if when is None or not p.is_file():
            continue
        out.append({"path": p, "when": when, "size": p.stat().st_size,
                    "suspicious": "-SUSPICIOUS" in p.name})
    out.sort(key=lambda b: b["when"], reverse=True)
    return out


def backup_due(backup_dir, *, min_hours: float = 20,
               now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    good = [b for b in list_backups(backup_dir) if not b["suspicious"]]
    return not good or (now - good[0]["when"]) >= timedelta(hours=min_hours)


def _set_readonly(p: Path, on: bool) -> None:
    mode = p.stat().st_mode
    p.chmod((mode & ~stat.S_IWRITE) if on else (mode | stat.S_IWRITE))


def make_backup(data_dir, backup_dir=None, *, min_hours: float = 20,
                keep_daily: int = 7, keep_weekly: int = 4,
                keep_monthly: int = 6, force: bool = False,
                now: Optional[datetime] = None) -> dict:
    """Back up ``data_dir``/history.db.enc (or history.db if never encrypted)
    into ``backup_dir`` if one is due, then prune. Never raises. Returns
    {status: 'ok'|'not_due'|'no_data'|'suspicious'|'error', path?, size?,
    pruned?, error?}."""
    try:
        now = now or datetime.now()
        data_dir = Path(data_dir)
        bdir = Path(backup_dir) if backup_dir else default_backup_dir()
        src = data_dir / "history.db.enc"
        enc = True
        if not src.exists():
            src, enc = data_dir / "history.db", False
        if not src.exists() or src.stat().st_size == 0:
            return {"status": "no_data"}
        if not force and not backup_due(bdir, min_hours=min_hours, now=now):
            return {"status": "not_due"}
        bdir.mkdir(parents=True, exist_ok=True)
        readme = bdir / "README.txt"
        if not readme.exists():
            readme.write_text(_README, encoding="utf-8")

        size = src.stat().st_size
        prior = [b for b in list_backups(bdir) if not b["suspicious"]]
        suspicious = bool(prior) and size < _SHRINK_FRACTION * prior[0]["size"]
        name = (f"history-{now:%Y%m%d-%H%M%S}"
                f"{'-SUSPICIOUS' if suspicious else ''}.db{'.enc' if enc else ''}")
        dest = bdir / name
        tmp = bdir / (name + ".partial")
        shutil.copy2(src, tmp)
        if tmp.stat().st_size != size:
            tmp.unlink(missing_ok=True)
            return {"status": "error", "error": "copy size mismatch"}
        os.replace(tmp, dest)
        _set_readonly(dest, True)

        # vault.json: keep a dated copy whenever it differs from the last one.
        vault = data_dir / "vault.json"
        if enc and vault.exists():
            vb = sorted(bdir.glob("vault-*.json"))
            if not vb or vb[-1].read_bytes() != vault.read_bytes():
                vdest = bdir / f"vault-{now:%Y%m%d-%H%M%S}.json"
                shutil.copy2(vault, vdest)
                _set_readonly(vdest, True)

        if suspicious:
            return {"status": "suspicious", "path": str(dest), "size": size,
                    "prior_size": prior[0]["size"], "pruned": []}
        pruned = prune(bdir, keep_daily=keep_daily, keep_weekly=keep_weekly,
                       keep_monthly=keep_monthly)
        return {"status": "ok", "path": str(dest), "size": size,
                "pruned": [p.name for p in pruned]}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def prune(backup_dir, *, keep_daily: int = 7, keep_weekly: int = 4,
          keep_monthly: int = 6) -> list[Path]:
    """Delete backups outside the retention policy (never SUSPICIOUS ones,
    never anything not matching this module's name pattern, never the newest
    ``keep_daily``). Returns the removed paths."""
    good = [b for b in list_backups(backup_dir) if not b["suspicious"]]
    keep: set = set()
    keep.update(b["path"] for b in good[:max(1, keep_daily)])
    weeks, months = {}, {}
    for b in good:                       # newest first → first seen wins
        wk = b["when"].isocalendar()[:2]
        mo = (b["when"].year, b["when"].month)
        if wk not in weeks and len(weeks) < keep_weekly:
            weeks[wk] = b["path"]
        if mo not in months and len(months) < keep_monthly:
            months[mo] = b["path"]
    keep.update(weeks.values())
    keep.update(months.values())
    removed = []
    for b in good:
        if b["path"] in keep:
            continue
        try:
            _set_readonly(b["path"], False)
            b["path"].unlink()
            removed.append(b["path"])
        except OSError:
            pass
    return removed
