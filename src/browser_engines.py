"""Which browser the app automates (⚙ Settings ▸ General ▸ Automation browser).

Microsoft Edge is the tested, zero-setup default (preinstalled on Windows).
Google Chrome or Brave — or another Chromium-based browser's .exe — can be
chosen instead, flagged as untested. Firefox isn't possible: Playwright
only drives its own patched Firefox build, not an installed one.

Each browser gets its OWN CaseloadNotes profile folder (never the user's
personal profile), so switching means signing in to WGU / Mongoose once in the
new one. Edge keeps the original folder (browser_data) so existing sign-ins
survive. Pure helpers, no Playwright import.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from src.config import BROWSER_DATA_DIR, USER_CONFIG_DIR

_PF = os.environ.get("ProgramFiles", r"C:\Program Files")
_PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
_LAD = os.environ.get("LOCALAPPDATA", "")

# key → label, exe name, Playwright channel (installed-browser lookup) or
# candidate install paths, tested flag.
ENGINES = {
    "edge": {"label": "Microsoft Edge (recommended)", "exe": "msedge.exe",
             "channel": "msedge", "tested": True,
             "paths": [rf"{_PF86}\Microsoft\Edge\Application\msedge.exe",
                       rf"{_PF}\Microsoft\Edge\Application\msedge.exe"]},
    "chrome": {"label": "Google Chrome", "exe": "chrome.exe",
               "channel": "chrome", "tested": False,
               "paths": [rf"{_PF}\Google\Chrome\Application\chrome.exe",
                         rf"{_PF86}\Google\Chrome\Application\chrome.exe",
                         rf"{_LAD}\Google\Chrome\Application\chrome.exe"]},
    "brave": {"label": "Brave", "exe": "brave.exe", "channel": "",
              "tested": False,
              "paths": [rf"{_PF}\BraveSoftware\Brave-Browser\Application\brave.exe",
                        rf"{_PF86}\BraveSoftware\Brave-Browser\Application\brave.exe",
                        rf"{_LAD}\BraveSoftware\Brave-Browser\Application\brave.exe"]},
}
# Not offered: Vivaldi (tested 2026-11-18 — Playwright's launch hangs). It can
# still be tried as a custom .exe; the 45 s launch limit in src/browser.py then
# falls back to Edge.
CUSTOM = "custom"
UNTESTED_NOTE = ("Untested with CaseloadNotes — if anything misbehaves "
                 "(sign-in, notes, texting), switch back to Microsoft Edge.")


def _first_existing(paths) -> str:
    for p in paths:
        try:
            if p and Path(p).is_file():
                return p
        except OSError:
            continue
    return ""


def installed() -> list:
    """[(key, label)] for the browsers found on this machine (Edge first)."""
    return [(k, e["label"]) for k, e in ENGINES.items()
            if _first_existing(e["paths"])]


def profile_dir(key: str) -> Path:
    """This browser's CaseloadNotes profile (Edge keeps the original)."""
    return BROWSER_DATA_DIR if key in ("", "edge") else \
        USER_CONFIG_DIR / f"browser_data_{key}"


def resolve(settings) -> dict:
    """What to launch for ``settings`` (browser_engine / browser_exe_path):
    {key, label, exe, channel, executable_path, profile, tested, note}.
    An unavailable choice falls back to Edge, with ``note`` saying so."""
    key = str(getattr(settings, "browser_engine", "") or "edge").lower()
    note = ""
    if key == CUSTOM:
        path = str(getattr(settings, "browser_exe_path", "") or "").strip()
        if path and Path(path).is_file():
            return {"key": CUSTOM, "label": Path(path).stem,
                    "exe": Path(path).name.lower(), "channel": "",
                    "executable_path": path, "profile": profile_dir(CUSTOM),
                    "tested": False, "note": ""}
        note = f"Browser not found ({path or 'no .exe chosen'}) — using Edge."
        key = "edge"
    e = ENGINES.get(key)
    if e is None:
        note, key, e = f"Unknown browser '{key}' — using Edge.", "edge", ENGINES["edge"]
    path = _first_existing(e["paths"])
    if key != "edge" and not path:
        note = f"{e['label']} isn't installed — using Edge."
        key, e = "edge", ENGINES["edge"]
        path = _first_existing(e["paths"])
    return {"key": key, "label": e["label"], "exe": e["exe"],
            "channel": e["channel"],
            # A Playwright channel finds the installed browser itself;
            # others launch by path.
            "executable_path": "" if e["channel"] else path,
            "profile": profile_dir(key), "tested": e["tested"], "note": note}


def label_for(settings) -> Optional[str]:
    """Short label of the configured browser (for logs / Settings)."""
    return resolve(settings)["label"]
