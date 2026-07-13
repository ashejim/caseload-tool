"""Fresh-install demo launcher.

Runs the app as a BRAND-NEW user would see it — its own isolated config dir
(`_fresh_demo/` at the repo root), so it seeds the sample actions, shows the
first-run welcome, and keeps its own settings/login/lock. It can run at the
SAME TIME as your real instance without touching your real scenarios, settings,
or Salesforce session, and it does NOT grab the global hotkeys.

Usage (from the repo root):
    python -m scripts.fresh_demo            # launch (fresh on first run)
    python -m scripts.fresh_demo --reset    # wipe config back to first-run
    python -m scripts.fresh_demo --keep     # keep prior demo state, don't reseed

The Salesforce login (browser_data) is preserved across runs — even on --reset —
so you only sign in once. Delete `_fresh_demo/` entirely for a truly clean slate.
"""

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "_fresh_demo"


def _reset_config() -> None:
    """Delete everything in the sandbox EXCEPT the browser profile, so the next
    launch re-seeds sample actions/templates and shows the first-run welcome."""
    SANDBOX.mkdir(parents=True, exist_ok=True)
    for item in SANDBOX.iterdir():
        if item.name == "browser_data":
            continue  # keep the Salesforce login
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        except Exception:
            pass


def main() -> None:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    keep = "--keep" in sys.argv
    if "--reset" in sys.argv or (
        not keep and not (SANDBOX / "scenarios.yaml").exists()
    ):
        _reset_config()

    # These MUST be set before importing the launcher: src.config computes all
    # of its paths (and seeds the sample scenarios/templates) at import time.
    os.environ["CASELOAD_CONFIG_DIR"] = str(SANDBOX)
    os.environ["CASELOAD_NO_HOTKEYS"] = "1"
    os.environ["CASELOAD_TITLE_SUFFIX"] = "   ●  FRESH-INSTALL DEMO"

    from scripts.launcher import main as launcher_main
    launcher_main()


if __name__ == "__main__":
    main()
