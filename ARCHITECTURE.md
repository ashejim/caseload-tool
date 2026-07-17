# Architecture

A map of how this project is organized, for anyone (including future-you)
picking it up. It describes **where things live** and the **direction** the code
is being refactored, so new work lands in the right place.

## What the app is

A desktop tool (Python + Tkinter/CustomTkinter) that automates repetitive
Salesforce caseload work for a course instructor: filing student notes, sending
emails (via Outlook) and texts (via Mongoose), reviewing a caseload, and
tracking student progress. It drives a real browser (Playwright/Chromium) to
act inside Salesforce and Mongoose, and replays their internal APIs where it can
for speed.

## Layout

```
scripts/          entry points + the (large) GUI
  launcher.py     the whole GUI + app controller (being decomposed — see below)
  fresh_demo.py   run an isolated demo instance
  login.py, ...   small dev/one-off helpers

src/              domain logic + integrations (importable, mostly GUI-free)
  version.py          single source of the version string
  config.py           settings.json + paths + per-user data dir + migrations
  scenarios.py        "actions" (scenarios) + note templates: YAML load/save + models
  note_form.py        the Salesforce note form model (NoteData) + fillers
  dates.py            date/timezone helpers (single source of truth)
  hotkeys.py          hotkey-spec string conversions (pynput / vk / keysym)
  caseload_csv.py     caseload CSV parsing + column handling + email-col recog
  caseload_filter.py  the filter engine + "Task N" facet routing
  student_lookup.py   finding/opening a student in the live Salesforce list
  history.py          local SQLite snapshots + departures + passed-outcomes archive
  success_path.py     per-course step checklists → recommended next action
  text_message.py     Mongoose SMS: templating, timezone scheduling, send
  mongoose_contacts.py  StudentID → Salesforce Contact id mapping (SQLite)
  email_template.py   {{var}} rendering for email/subject
  outlook_email.py, outlook_signature.py   Outlook (COM) send + signatures
  crypto_store.py     encrypt/decrypt local PII files at rest
  browser.py, selectors.py, grid_rows.py   Playwright + Salesforce DOM helpers
  action_queue.py     queue of batch actions to run later
  proc_priority.py    process priority tweaks
  ui_common.py        shared UI primitives (button styles, dialog sizing/
                      geometry, tooltip, checkbox images, listbox drag, fonts)
  dialogs.py          standalone modal dialogs (parent + args → value)
  rich_text.py        RichTextEditor widget + HTML parsers + clipboard HTML
  os_open.py          open URIs/files in external apps (Edge, default, Word)

tests/            unit tests (pure logic) + a smoke-import test
CHANGELOG.md      user-facing release notes (see src/version.py for the scheme)
build.py          PyInstaller one-folder build + release zip
```

Per-user data (`scenarios.yaml`, `caseload.csv`, templates, browser profile,
history DBs) lives in `%APPDATA%\caseload-notes\`, **not** in the repo — so the
install stays read-only and the repo stays free of personal data. That's why
`scenarios.yaml` is gitignored.

## The `launcher.py` situation

`scripts/launcher.py` is a ~34k-line file that currently holds **everything**
GUI: the app controller plus every panel, editor, dialog, and the browser
worker thread. The main pieces inside it:

| Class / group | Responsibility |
|---------------|----------------|
| `App` | the top-level controller — wires everything together, owns the fire flow |
| `BrowserWorker` | the Playwright worker thread; talks to `App` over a queue |
| `CaseloadPanel` | the caseload viewer/grid |
| `ScenarioEditor` / `NoteEditor` / `FilterRow` | the action editor |
| `DataPanel`, `RichTextEditor` | momentum/calibration view, rich-text email editor |
| `prompt_*` functions | modal dialogs (edit note, batch review, calendar, …) |

This is being **incrementally decomposed** into smaller modules. The goal is a
newcomer can open one focused file instead of scrolling a 34k-line one.

### Refactoring plan (in progress)

Done in small, verified steps — each must keep `tests/test_smoke_import.py`
green and the app launchable:

1. **Domain logic → `src/`.** Pure, GUI-free helpers move out of `launcher.py`
   into named, unit-tested `src/` modules (e.g. `src/dates.py`). Safest first —
   these are testable and don't touch the UI.
2. **Shared UI kernel → `src/ui_common.py`.** DONE — the small primitives
   dialogs and panels share (button styles, tooltips, dialog sizing/geometry,
   checkbox images, listbox drag). Re-imported into `launcher.py` so call sites
   are unchanged.
3. **Dialogs → `src/dialogs.py`.** DONE — all 21 `prompt_*` modals moved out,
   including the two rich-text ones, once `RichTextEditor` was lifted into
   `src/rich_text.py` and the external-open helpers into `src/os_open.py`.
4. **Panels + worker → own modules.** `caseload_panel.py`, `scenario_editor.py`,
   `browser_worker.py`, etc.
5. **Slim `App`.** Split the controller by concern (fire flow / batch / EA /
   texting / settings) once the file is smaller.

## Conventions

- **Domain vs. UI.** Logic that has nothing to do with widgets lives in `src/`
  and gets a unit test. GUI code stays in `scripts/` (for now) and is verified
  by launching the app.
- **Small, single-purpose modules** with a docstring saying what they own.
- **One source of truth.** If two places compute the same thing (e.g. the
  timezone map), consolidate into one module and import it.
- **Tests are the safety net.** `tests/test_*.py` are plain scripts —
  `python tests/test_x.py` prints `N/N passed`. Add a test alongside any new
  `src/` module. `tests/test_smoke_import.py` imports every module (catches a
  dangling reference left by a move) — run it after every refactor step.

## Running, testing, building

```
# run the app (from the repo root, with the project venv)
.venv\Scripts\python.exe scripts\launcher.py

# run all tests
for t in tests\test_*.py: .venv\Scripts\python.exe %t

# build a distributable zip (PyInstaller)
.venv\Scripts\python.exe build.py     # -> CaseloadNotes-vX.Y.Z.zip
```

Releasing: bump `src/version.py` + add a `CHANGELOG.md` entry, commit, tag
`vX.Y.Z`, then `gh release create vX.Y.Z CaseloadNotes-vX.Y.Z.zip`.
