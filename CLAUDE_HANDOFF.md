# Claude Handoff

Orientation doc for an AI assistant (or human) picking up this project. Last
updated **2026-10-15**. For narrative history and per-feature detail, see the
persistent memory index (outside the repo) and `CHANGELOG.md`.

## What this is

A Windows desktop tool (Python + CustomTkinter GUI) that automates a WGU course
instructor's caseload workflow: filing Salesforce notes, sending Outlook emails
and Mongoose texts, scraping live task pass/fail, and local analytics over a
caseload-history database. Browser automation is Playwright driving its own
bundled Chromium. Distributed as an unsigned PyInstaller one-folder build.

- Repo: `ashejim/caseload-tool` (public, proprietary / all-rights-reserved)
- Owner: Dr. Jim Ashe (mathematician; prefers standard conventions, simple
  shareable builds, workarounds over heavy fixes)

## ▶ PICK UP HERE (laptop handoff, 2026-10-15)

**In flight: Learner360 notes import (`l360notes:`)** — built + unit-tested,
NOT yet run live, NOT committed on desktop `main` (see "Getting the code" below).

**Getting the code / data on the laptop:**
- Code: `git fetch && git checkout wip/l360-notes` (branch holds the notes
  import + this file on top of `main`). If it isn't on GitHub, it wasn't pushed
  from the desktop yet.
- Data: `history.db` is local, encrypted, gitignored and holds student PII —
  it does NOT travel. The laptop builds its own: load the caseload, then
  `l360momi:` (fills `l360_ids`), then `l360notes: dry`. The dedupe numbers
  will differ from the desktop's (fewer SF-scraped notes on the laptop).
- Claude's memory (`~/.claude/projects/D--salesforce-automation/memory/`) is
  desktop-only; this file carries the essentials.

Goal: keep Data > Throughput's contacts line current automatically by pulling
each learner's FULL note history from Learner360
(`GET /studentNotes/v1/notes/<learner uuid>`) instead of relying on the SF
`sweepnotes:` scrape (60-note cap, manual).

Pieces:
- `src/learner360.py` — `NOTES_PATH`, `_NOTE_FIELDS` (CANDIDATE field names —
  guessed, unverified), `iso_utc`, `parse_notes`, `notes_shape` (field names +
  labels only, FERPA-safe), `fetch_notes_batch`.
- `src/history.py` — `persist_l360_notes(by_student, contact_ids, dry)` dedupes
  each 360 note vs the 74k SF-scraped notes: `already` (id `l360:<id>`) /
  `exact` (same student + same second + same type or body prefix) / `fuzzy`
  (same body prefix within ±36 h; reports hour offsets) / `new` (inserted).
  `_find_same_note` + `_norm_body` also wired into `persist_notes` so a LATER SF
  scrape upserts the l360 row instead of double-counting. `l360_note_targets`
  (learners from `l360_ids`, refreshed ≤30 d), `l360_notes_last_import` (meta).
- `src/browser_worker.py` — `_l360_notes_fetch` (cmd `L360_NOTES`,
  `submit_l360_notes_fetch`), reuses `_l360_session`.
- `scripts/launcher.py` — keyword `l360notes:` / `l360notes: dry` →
  `App._l360_notes_import(dry, auto)` / `_l360_notes_done`.
- `tests/test_learner360.py` 13/13 (incl. dedupe both directions).

**Dry run #1 (desktop, 2026-10-15)** — 206 learners, 241 s, **122,436 notes**
(full history back to 2019, all authors/courses) vs 13,999 SF-scraped notes
stored for them. Real fields: no `id` → `compositeKey`/`newId`/`oldId`/
`noteNumber`/`interactionId`; `createdDate` (Z or `.000+0000`; ~3% missing →
`lastModifiedDate` fallback); `type` mixes SF labels and snake_case codes
(`email_to_student`, `instant_message_text`, `email_from_student`…) →
`sf_type_label` maps codes to SF labels (fixes inbound detection); `subType` is
a topic, no longer merged into type. BUG found + fixed: matching wasn't 1:1
(24,169 "matches" for 13,999 stored notes) → now one stored note per incoming
note (`claimed`), and duplicate ids inside Learner360's own lists are skipped
(`dup_in_360`). Fuzzy clock offsets were spread +6…+16 h and −6/−7 h (unclear —
re-check after the 1:1 fix). **Needs dry run #2** (log now also prints id-field
uniqueness `present/distinct` → pick the truly unique id).
Concern for auto-import: 4 min of `_set_busy` blocks the UI — consider a
non-blocking background mode or a weekly cadence.

**Dry run #2:** `compositeKey` is unique (32793/32793; `noteNumber` is not);
1:1 matching now sane (7,276 matched for 129 learners). New problems: 77/206
learners got HTTP **503** (gateway overload; run took 10 min) → fetch now uses
concurrency 3 + retry/backoff on 429/5xx. **4,728 same-event repeats** inside
Learner360 lists (same second + same text, different key) → deduped as
`dup_in_360`. **Suspicious 2026-09 spike** (6,620 "new" vs ~1,600/month) —
possibly migration copies dated by migration time. Dry runs now STAGE parsed
notes into `history.db` table `l360_notes_staging` so the spike / leftover
mismatches can be analyzed offline (aggregates only) without re-pulling.

**Next steps:**
1. Run the app → type `l360notes: dry` in the caseload search box. On a fresh
   machine first run `l360momi:` (needs Learner360 ids; sign in to Learner360
   in the app's browser yourself — Claude must not enter credentials).
2. Read the dry-run log: fix `_NOTE_FIELDS` to the real field names; check type
   labels look SF-style ("Email to Student", "Instant Message (IM) / Text") so
   `classify_note_channel/direction` apply; check exact vs fuzzy vs new and the
   fuzzy clock offsets (a constant offset = timezone skew → normalize in
   `iso_utc`). Expect many "new" in older months (full history vs 60 cap) —
   that's a correction, not double counting.
3. Real run `l360notes:`; re-run should show 0 new.
4. Add daily background auto-import mirroring `_maybe_auto_l360_momi`
   (Settings combobox + `Settings.l360_auto_notes_days`, re-arm timer).
5. Commit; then release (stash decision-tree WIP first) — Learner360 work isn't
   in any release yet.

**Recently shipped (on `main`, local commits not yet pushed at handoff time):**
- `84dcc95` Learner360 weekly MOMI history + change watch (`l360momi:`) —
  history schema **v5**: `momi_weekly`, `momi_factors`, `l360_ids`.
- `d884588` weekly MOMI auto-refresh (startup +3 min, re-arms 6 h, quiet;
  Settings > Caseload & Data > "Learner360 MOMI history refresh", default weekly).
- `961a1b8` refactor: shared `_l360_session` (open Learner360 + harvest auth).

**Learner360 facts** (WGU's new caseload app; strategy = HYBRID — SF stays the
caseload source, 360 = extra data): gateway `https://faculty-api-gateway.wgu.edu`;
`caseloads/search` pages are **0-based**; gateway **415s without
`Content-Type: application/json`** even on GET; weekly MOMI dates arrive
MM-DD-YYYY; `momentumScore` is 0–1 with cuts ≈0.2/0.5/0.7/0.9; each learner's
weekly series = last ~26 weeks. Team filter ("WST- Bojonny") = 16 caseloads /
~3,310 learners (`caseloadOwner IN [...]`).

**Parked: MOMI-vs-outcome prediction re-analysis.** Learner360 only lists
CURRENT students and has **no PIDM / Student-ID lookup** (probed live
2026-10-15: all 400/404; don't re-probe), so resolved students' MOMI can't be
backfilled. Analysis is PROSPECTIVE: run ~early Dec 2026 once captured students
reach Oct/Nov term ends (goal: beat old AUC ~0.62). Team-wide capture is an
option if the sample is thin (FERPA scope — ask first).

**Claude-in-Chrome / auto-mode note:** the auto-mode classifier blocks reusing
the Learner360 session token from ad-hoc scripts (even in-app probes); the user
switched out of auto mode to allow the PIDM probe. Shape-only discovery
(field names, statuses) is fine; never extract/print tokens.

## Current state (as of 2026-10-15)

- **Released: v0.26.1** (`07b4062`) — Mongoose texting fix (straight `"` +
  smart punctuation rejected → `sanitize_sms`). **v0.26.0** (`ea226d0`) — Bot
  Drafts review (chatbot eval, off by default), quick single text from the
  student view, term-end pass-rate report (`scripts/non_passer_report.py`;
  snapshot-based 91.1% matches WGU's 91.7%).
- After v0.26.1 on `main`: `604e9a4` Throughput stale-notes caption + the three
  Learner360 commits above.
- Older open items (inbox-labeler re-arm, in-app backup button, GUI
  live-eyeballs of v0.24/v0.25 UI) still stand — see "Notable deferred".

### Uncommitted WIP on `main` (do NOT ship without a decision)

- `src/decision_tree.py`, `tests/test_decision_tree.py` (untracked) and the
  matching changes in `src/data_panel.py` (modified) = the interpretable
  **"Action tree"** advanced Data-tab view. Ran on real data: it does **not**
  beat the existing isotonic momentum-risk ranker, so it stays OUT of the risk
  score — value is action-routing only. Intentionally left uncommitted.
- `sample_data/sample_caseload.csv` may show as modified after a build — the
  build regenerates sample data. Harmless; revert if you don't intend to commit
  regenerated fixtures.

## Dev setup

- Python **3.14** via the project venv at `.venv/`. Always use
  `.venv/Scripts/python.exe` (the system Python lacks the deps).
- Deps include: customtkinter, playwright (1.60), pillow, pyinstaller (6.20).
  `pytest` is **not** installed.
- Platform is Windows 11; shell is PowerShell (a Git Bash tool is also
  available). Prefer absolute paths.

## Running things

```
# Run the app (dev)
.venv/Scripts/python.exe scripts/launcher.py

# Offline demo (no browser/login, synthetic data)
demo.bat                        # or: .venv/Scripts/python.exe -m scripts.demo
demo.bat --reset                # rebuild the fake caseload

# Tests — each file is a standalone script (NOT pytest):
.venv/Scripts/python.exe tests/test_contact_pref.py
# ...prints "N/N passed" and exits non-zero on failure.

# Syntax check
.venv/Scripts/python.exe -m py_compile scripts/launcher.py
```

Test loop discipline: **kill the running launcher before relaunching** — no
stacked windows / single instance.

## Release process (see also memory: release_process)

A GitHub Release is more than a version bump:

1. Bump `src/version.py` `__version__` + add a `CHANGELOG.md` entry.
2. `.venv/Scripts/python.exe build.py` → produces `dist/CaseloadNotes/` and
   `CaseloadNotes-vX.Y.Z.zip` in the project root. build.py regenerates the
   bundled `sample_data/` (so the frozen demo toggle works).
3. Commit, `git push origin main`.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. `gh release create vX.Y.Z CaseloadNotes-vX.Y.Z.zip --title ... --notes ...`
   (`gh` is authenticated; repo is public; build is unsigned → SmartScreen/SAC
   may warn on first run).

Versioning: MAJOR = scenarios.yaml format break, MINOR = features, PATCH = fixes.

## Architecture

- `scripts/launcher.py` — the monolith (~29k lines): `App` (main window +
  worker orchestration) and `CaseloadPanel` (the caseload grid + student info
  view). A refactor to extract modules is partly done (Steps 1–3 shipped in
  v0.20.0: `src/ui_common`, `dialogs`, `rich_text`, `os_open`); Steps 4–5 (panels
  + BrowserWorker, then slim App) are deferred. See `ARCHITECTURE.md`.
- `src/history.py` — local SQLite (`history.db`): caseload snapshots, contact
  notes, outcomes archive, student flags (contact preference, pronouns),
  momentum-risk model. Encrypted at rest.
- `src/caseload_csv.py` — caseload CSV parsing + `DISPLAY_TO_CSV` column map.
- `src/data_panel.py` — the Data tab (calibration, risk, trajectory, throughput
  charts; + the uncommitted Action-tree view).
- `scripts/sample_data.py` / `scripts/demo.py` — offline demo data + runner.
- Data locations: **dev** = `PROJECT_ROOT/history.db`; **frozen build** =
  `%APPDATA%\caseload-notes\history.db`. Offline demo uses an isolated
  `_demo/` sandbox (dev) or `%APPDATA%\caseload-notes-demo` (frozen).

## Invariants & gotchas (learned the hard way)

- **Notes can only be EDITED, never deleted** in WGU Salesforce → the note-write
  path must never double-file; never advise deleting a note.
- **Never report a failed email as sent.** Email needs **Outlook Classic** (COM);
  "new Outlook"/web is unsupported (registry probe + loud pre-flight alert).
- **Encryption at rest**: local PII files are DPAPI/app-password encrypted;
  decrypt-on-unlock / re-encrypt-on-exit. A prior bug let a 0-byte leftover
  plaintext shadow the good `.enc` (fixed v0.23.1) — keep the "non-empty AND
  newer" guard.
- **Front-load dialogs before navigation** — show fire dialogs first, then
  navigate; never navigate concurrently or the browser un-minimize buries the
  popup and it looks hung. Don't use `messagebox` after `mail.Display()` (Outlook
  steals focus) — use `ask_yes_no_topmost`.
- **Passers drop off the WGU caseload export** → snapshots freeze at pre-pass
  state; durable pass records live in the outcomes archive (Departures → Update
  archive). Only the "Archive (Last 30 Days)" view is archived.
- The at-risk model is near its **data ceiling** — the limit is data (a ≤9wk
  near-static MI window), not modelling. The grid API feed is exhausted; don't
  re-probe. See `data_analysis/DATA_REQUEST.md` (gitignored).
- Keep a 🧪 **Test group** in `scenarios.yaml` exercising each feature as a
  regression checklist; use safe-to-fire conventions.

## Notable deferred / next-up

- **Inbox labeler** — SHIPPED in v0.25.0. Remaining: seed the 15 shared-box
  categories (manual, for colors) and fix the startup auto-refresh (fires once;
  wants a timer re-arm + 6–8h max-age + `U|` age-out). See
  `docs/INBOX_LABELER_HANDOFF.md`.
- **In-app "Export caseload backup" button** — the CSV backup shipped as a
  script only; packaged colleagues need a Settings button (export logic already
  exists in `scripts/export_caseload_backup.py`).
- Live-verify the v0.25.0 (and still-pending v0.24.0) shipped build UI.
- Decide the fate of the Action-tree / decision-tree WIP (ship as action-routing
  view, or drop).
- launcher.py refactor Steps 4–5.
- Rich-text editor UX backlog (template switching, preview, font zoom).

## Memory

The assistant keeps persistent notes at
`~/.claude/projects/D--salesforce-automation/memory/` (`MEMORY.md` is the index).
Those recall entries reflect what was true when written — verify any named file,
function, or flag still exists before relying on it. This handoff is a snapshot;
the memory index is the living record.
