"""Automation-browser choice (src/browser_engines.py) + the stale-process
sweep's exe guard (src/browser.py). Fake install paths in a temp dir — no
browser is launched, no real profile is touched.

Run: python tests/test_browser_engines.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import browser, browser_engines as be  # noqa: E402
from src.config import BROWSER_DATA_DIR  # noqa: E402


def _fake_installs(tmp, which):
    """Point each engine's install paths at tmp; create exes for ``which``."""
    saved = {k: list(e["paths"]) for k, e in be.ENGINES.items()}
    for k, e in be.ENGINES.items():
        exe = Path(tmp) / k / e["exe"]
        if k in which:
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("x")
        e["paths"] = [str(exe)]
    return saved


def _restore(saved):
    for k, paths in saved.items():
        be.ENGINES[k]["paths"] = paths


def test_resolve_choices_and_fallbacks():
    tmp = tempfile.mkdtemp()
    saved = _fake_installs(tmp, {"edge", "chrome"})
    try:
        S = lambda **kw: types.SimpleNamespace(**kw)  # noqa: E731
        d = be.resolve(S())
        assert d["key"] == "edge" and d["channel"] == "msedge" and d["tested"]
        assert d["profile"] == BROWSER_DATA_DIR and not d["note"]   # existing profile kept
        c = be.resolve(S(browser_engine="chrome"))
        assert c["key"] == "chrome" and c["channel"] == "chrome" and not c["tested"]
        assert c["profile"].name == "browser_data_chrome" and c["profile"] != BROWSER_DATA_DIR
        b = be.resolve(S(browser_engine="brave"))                  # not installed
        assert b["key"] == "edge" and "isn't installed" in b["note"]
        u = be.resolve(S(browser_engine="netscape"))
        assert u["key"] == "edge" and "Unknown browser" in u["note"]
        assert [k for k, _ in be.installed()] == ["edge", "chrome"]
        exe = Path(tmp) / "other" / "Thorium.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("x")
        cu = be.resolve(S(browser_engine="custom", browser_exe_path=str(exe)))
        assert cu["key"] == "custom" and cu["executable_path"] == str(exe)
        assert cu["exe"] == "thorium.exe" and cu["profile"].name == "browser_data_custom"
        gone = be.resolve(S(browser_engine="custom", browser_exe_path=str(exe) + "x"))
        assert gone["key"] == "edge" and "not found" in gone["note"]
        assert "Untested" in be.UNTESTED_NOTE
        assert "vivaldi" not in be.ENGINES                         # hangs under Playwright
    finally:
        _restore(saved)


def test_stale_sweep_rejects_unsafe_exe_names():
    calls = []
    import subprocess
    orig = subprocess.run
    try:
        subprocess.run = lambda *a, **kw: calls.append(a[0])
        browser._kill_stale_profile_edge("chrome.exe'; Remove-Item x; '", Path("C:/p"))
        browser._kill_stale_profile_edge("", Path("C:/p"))
        assert calls == []                                   # nothing executed
        if sys.platform.startswith("win"):
            browser._kill_stale_profile_edge("brave.exe", Path("C:/p"))
            assert len(calls) == 1 and "Name='brave.exe'" in calls[0][-1]
            assert "C:" in calls[0][-1]
    finally:
        subprocess.run = orig


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
