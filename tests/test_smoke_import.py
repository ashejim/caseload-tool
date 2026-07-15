"""Smoke test: every module imports cleanly.

This is the safety net for refactoring the big `scripts/launcher.py` into
smaller modules. It doesn't launch the GUI — it just imports each module, which
runs all top-level code (constants, class definitions, function definitions).
That catches the most common refactor breakage: a name that moved to another
module but is still referenced by its old (now-undefined) global name, a
circular import between two new modules, or a typo'd import.

It is NOT a substitute for launching the app and clicking through — GUI logic
inside methods only runs when exercised — but it turns "did the cut-and-paste
leave a dangling reference" into a fast, automatic check.

Run: python tests/test_smoke_import.py
"""
import importlib
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Domain / library modules under src/ (no GUI). Add new extracted modules here
# as they are created so the smoke test grows with the refactor.
SRC_MODULES = [
    "src.version",
    "src.config",
    "src.dates",
    "src.ema_links",
    "src.scenarios",
    "src.note_form",
    "src.note_text",
    "src.caseload_filter",
    "src.caseload_csv",
    "src.history",
    "src.success_path",
    "src.student_lookup",
    "src.text_message",
    "src.mongoose_contacts",
    "src.email_template",
    "src.action_queue",
]


def test_src_modules_import():
    for name in SRC_MODULES:
        importlib.import_module(name)


def test_launcher_imports_without_launching():
    """Import scripts/launcher.py as a module. It must NOT open a window — the
    GUI only starts under `if __name__ == '__main__'`."""
    path = os.path.join(ROOT, "scripts", "launcher.py")
    spec = importlib.util.spec_from_file_location("launcher_smoke", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # A couple of anchors so the import can't be "successful" but empty.
    assert hasattr(mod, "App"), "App class missing from launcher"
    assert hasattr(mod, "BrowserWorker"), "BrowserWorker missing from launcher"


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
