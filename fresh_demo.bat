@echo off
REM Launch the FRESH-INSTALL DEMO instance (isolated config in _fresh_demo\).
REM Safe to run alongside your real Caseload Tool — separate login, settings,
REM and no global hotkeys. Double-click for a normal launch, or from a prompt:
REM     fresh_demo.bat --reset    (wipe back to a clean first-run)
REM     fresh_demo.bat --keep     (keep prior demo state)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m scripts.fresh_demo %*
