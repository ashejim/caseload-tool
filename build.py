"""Build the CaseloadNotes one-folder distribution.

Usage:
    .venv\\Scripts\\python.exe build.py

Produces:
    dist/CaseloadNotes/
        CaseloadNotes.exe
        _internal/
            notes.yaml            <- default note presets
            ... (Python runtime, libs)

The browser is Microsoft Edge (preinstalled on Windows 10/11), so we no
longer bundle Chromium — the build is small.

Per-user data (their edited notes.yaml, browser_data/, screenshots/)
lives in %APPDATA%\\caseload-notes\\ so the install folder stays clean.
"""
import shutil
import subprocess
import sys
from pathlib import Path

DIST_NAME = "CaseloadNotes"


def folder_size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def main() -> None:
    project_root = Path(__file__).resolve().parent

    for d in ("build", "dist"):
        target = project_root / d
        if target.exists():
            print(f"Cleaning {target}")
            shutil.rmtree(target)

    print("Running PyInstaller...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
         "caseload_notes.spec"],
        check=True, cwd=project_root,
    )

    dist = project_root / "dist" / DIST_NAME
    if not dist.exists():
        sys.exit(f"Expected build output at {dist} but it's missing.")

    print()
    print(f"Build complete: {dist}  ({folder_size_mb(dist):.1f} MB)")
    print(f"Run:            {dist / (DIST_NAME + '.exe')}")
    print(f"Distribute:     zip the {DIST_NAME}/ folder and share")
    print("Recipients need Microsoft Edge installed (default on Win10/11).")


if __name__ == "__main__":
    main()
