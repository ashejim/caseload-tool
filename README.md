<p align="center">
  <img src="docs/images/owl.png" alt="CaseloadNotes owl logo" width="150">
</p>

# CaseloadNotes — Caseload automation for WGU Course Instructors

(Note automation)+(Text scheduler/automation)+(Mail merge)+(EA dashboard)+(Caseload)+(Data collection + outcome tracking)+(Analytics)...
= caseload-tool

A Windows desktop tool that organizes and automates the repetitive Salesforce/Outlook/Mongoose
work of running a WGU course caseload — filing notes, sending emails and texts,
and seeing at a glance who needs attention. It runs client-side under your own
login, so **no Salesforce admin permissions are required**.

It also quietly builds a **longitudinal local record of your caseload** — daily
snapshots, student outcomes, and every contact you make — that WGU doesn't
report back to instructors. That record powers trend, engagement, and pass-rate
analytics (including predicted-vs-actual Momentum calibration) you otherwise
couldn't get, and it stays on your machine, encrypted at rest.

Press a hotkey (or click a student in the built-in viewer) and a pre-defined
note is filled and submitted on the active student — no typing through fields,
no clicking through Lightning. Batch the same action across every student a
filter matches, and it emails/texts/notes all of them in one reviewed pass.

Built to replace AutoHotkey / Pulover macros that break on Salesforce slowness
and UI changes.

## What it does

- **Files Student Notes automatically** — Interaction Format, Interaction Type,
  Course Code, Subject, Academic Activities, Body — with the **course code
  auto-detected** from the caseload, then submits and closes the tab.
- **A built-in caseload viewer** — a searchable, filterable list of your
  students with per-student info, **live task pass/fail** and **Momentum**,
  Essential Actions, and a quick-view panel. Loads from Salesforce's caseload
  API (no manual list-view setup needed).
- **Actions** — reusable single-fire, panel (per-student), and **batch**
  actions that can send an **Outlook email** (templated, variables, auto-CC the
  Program Mentor, your signature), a **Mongoose text** (templated, timezone-aware
  scheduling, opt-in aware), and/or **file notes** — alone or combined.
- **Batch by filter** — pick students by course, task status, momentum,
  follow-up date, success-path step, etc., review the recipient list, and fire.
- **Off-caseload students** — view and file notes/texts for students in your
  course who are on another instructor's caseload.
- **Success paths** — per-course step checklists that surface the recommended
  next action per student.
- **Local data collection + tracking** — WGU doesn't report historical outcomes
  back to instructors, so the app keeps its own longitudinal record: automatic
  **daily caseload snapshots** (Momentum, task pass/fail, follow-ups, key dates),
  a **contact-note history** (your outreach *and* inbound replies, captured from
  Salesforce), and an ingested **passed-outcomes archive** — all in a local,
  encrypted database, with a **CSV backup/export** and recovery guide.
- **Data & analytics tab** — turns that record into insight: Momentum
  **calibration** (predicted vs. actual pass rate by band), **momentum-risk**
  targeting of in-progress students, **throughput** and **completion** by month,
  **departures**, and a snapshot-based **term-end pass-rate report** that
  reproduces the official rate the 30-day archive under-counts (with a companion
  command-line report for deeper analysis).
- **Extras** — an action queue, conditional action branching, and at-rest
  encryption of local student data.

## What it saves you

Every note, email, or text you file by hand is a chain of small, slow,
repetitive steps — most of the time spent **waiting on Salesforce Lightning to
load** and **re-entering the same fields** for every student. The app collapses
each chain to one keypress or click. The estimates below are for a typical
~227-student caseload; your mileage depends on how much outreach you do.

### Filing one Student Note by hand

| Step | ~Time |
|---|---|
| Find the student (search box, wait, click the row) | 10–15s |
| Open their record + wait for Lightning to render | 5–8s |
| Open the **New Note** panel + wait | 5s |
| Set **Interaction Format** + **Interaction Type** dropdowns | 6s |
| Find + type the **Course Code** (per student) | 8–12s |
| Type the **Subject** | 5s |
| Pick the **Academic Activities** (multi-select) | 10–15s |
| Type / paste the **Body** | 10–30s |
| **Submit**, wait, then close the tab | 8s |
| **Total** | **~90–150s (~2 min)** |

With the app: press the hotkey (or fire from the viewer) — every field is filled
from your template, the **course code is auto-detected**, and it submits and
closes. You wait ~15s. **Net: ~1.5–2 min saved per note.**

### A follow-up email + note

By hand: open Outlook, compose and personalize the email, send, then file the
note above → **~4–5 min**. With the app: the templated email renders with the
student's name, course, and the Program Mentor auto-CC'd; you glance at it, it
sends and logs the note → **~30–45s. ~3–4 min saved.**

### Batch actions — where the hours go

"Welcome the 25 newly-assigned students" by hand means repeating the email+note
chain 25 times ≈ **~90 min**. The app filters the caseload, shows you the
recipient list to confirm, and fires all of them in one pass — a few minutes.
That's **~3 min saved per student**, so **~60–75 min saved on that one run**.

### Caseload triage

Knowing who's stalled, who passed a task, who's low-momentum normally means
scrolling Salesforce and clicking into records one by one. The viewer shows it
all at once, and task pass/fail comes from the caseload API in **~0.0s** instead
of a ~36s scrape. **~10–20 min/day saved** just knowing where to focus.

### Bottom line

For a ~227-student caseload with a normal mix of individual notes and periodic
batch outreach:

- ~15 individual notes/emails a day × ~2.5 min → **~37 min**
- batch outreach averaging ~20 students/day × ~3 min → **~60 min**
- daily caseload triage → **~15 min**

**≈ 1.5–2 hours per day, or roughly 8–10 hours per 5-day work week.**
Heavy-outreach days (welcome waves, end-of-term reminders) can save 3+ hours on
their own; quiet days closer to 45 min. Most of what's saved isn't typing — it's
the **waiting and clicking** Salesforce makes you repeat for every student.

## Quick start (end users)

### 1. Download and extract

Download `CaseloadNotes-vX.Y.Z.zip` from
[Releases](https://github.com/ashejim/caseload-tool/releases) and extract it
anywhere — Desktop, Documents, wherever. You need Microsoft Edge installed
(default on Windows 10/11).

> **📧 Email sending requires Outlook Classic** — the installed desktop app.
> The **"new Outlook"** and **Outlook on the web** are *not* supported for
> automated sending (they don't expose the automation Outlook Classic does).
> If email isn't working, install/enable Outlook Classic, or turn **off** the
> **"New Outlook"** switch in the top-right of the Outlook window. (Everything
> else — notes, texts, the caseload viewer — works without Outlook.)

### 2. First-run — unblock the app

Windows 11 may block the launcher on first run because the build isn't signed
with a known-publisher certificate:

- **Recommended:** right-click `CaseloadNotes.exe` → **Properties** → check
  **Unblock** at the bottom of the General tab → **OK**.
- **If that doesn't work:** Windows Security → **App & browser control** →
  **Smart App Control settings** → switch **Off** (system-wide).

### 3. First-run — sign in

> **🌐 Close your other Microsoft Edge windows first.** The app runs its **own**
> Edge to reach Salesforce. If your personal Edge is already open, Windows makes
> them share one session and the app may not be able to sign in (you'll see
> *"Salesforce isn't signed in"*). Close all Edge windows, then launch the app —
> it opens its own Edge. (Your day-to-day browser, e.g. Chrome/Vivaldi, is fine;
> only **Edge** conflicts.)

Double-click `CaseloadNotes.exe`. A browser window opens — sign in to Salesforce
as you normally would (SSO/MFA). The session is saved and reused on every
subsequent launch, so you won't have to sign in again. The welcome screen lets
you pick **Simple** or **Advanced** editor mode; you can change it later in
**⚙ Settings**. No other setup is required — the caseload loads automatically.

## Daily use

1. **Launch** CaseloadNotes — after a moment the status reads "Browser ready"
   and your caseload appears in the viewer.
2. **File a quick note:** open a student's **New Note** panel in Salesforce (or
   select the student in the viewer) and **press the action's hotkey**, anywhere
   on your system.
3. **Batch:** click a batch action, review the recipient list, and fire — emails,
   texts, and notes go out for everyone the filter matched.

Bare F-keys (F2–F12) are claimed system-wide while the launcher runs and return
to normal when you close it. F1 is reserved by the browser. Modifier combos
(e.g. `Ctrl+Shift+W`) work too.

## Editing and adding actions

Your actions live in `%APPDATA%\caseload-notes\scenarios.yaml`. Edit them in the
app via **✎ Edit actions** (one tab per action — set the note fields, email,
text, batch filters, and hotkey, then **Save**) or directly in the YAML file. A
fresh install ships a small set of **Sample** actions marked *edit or delete me*
to start from.

## Security & privacy

CaseloadNotes runs **entirely on your machine, under your own login** — there is
no server, no account, and nothing is sent to the author or any third party.

- **No credentials are stored in this repository or by the app.** You sign in to
  Salesforce yourself, in a real browser window, using your normal SSO + MFA. The
  authenticated session lives only in a local `browser_data/` folder on your PC
  and never leaves it. Access to WGU systems is always gated by *your* SSO and
  MFA — having the source code grants no access to any data.
- **Student data (FERPA) stays local.** Caseload exports, notes, texting
  contacts, and history snapshots are written only to your machine and are
  excluded from source control. Local PII files can additionally be **encrypted
  at rest** behind an app password (**⚙ Settings**).
- **What's in the repo is code only** — no student records, no session tokens, no
  API keys. See [SECURITY.md](SECURITY.md) for the full statement and how to
  report an issue.

## Known issues

- **Smart App Control blocks the .exe** on first run — see Quick start step 2.
  Will be resolved once the project is code-signed.
- **First action of a session is slower** while Salesforce Lightning warms up;
  the rest are fast.
- **Comments in scenarios.yaml are lost** when you save via the app — edit the
  file directly if you need them.
- **Links stuck at about:blank** on a fresh launch: middle-click or right-click →
  **Open link in new tab** to bypass. Clears after the first driven action.

## Developer setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python -m scripts.launcher
```

**Fresh-install demo** — run the brand-new-user experience in an isolated
sandbox (its own config/login, no global hotkeys) alongside your real instance:

```powershell
fresh_demo.bat            # or: python -m scripts.fresh_demo
fresh_demo.bat --reset    # wipe back to a clean first-run
```

**Offline demo (no login)** — run the whole tool on synthetic sample data with
the browser automation off. No Salesforce/Mongoose credentials needed; ideal for
contributors exploring or adding features (see `CONTRIBUTING.md`):

```powershell
demo.bat                  # or: python -m scripts.demo
demo.bat --reset          # rebuild the fake sample caseload from scratch
```

In a **downloaded/built app**, open the demo from inside the tool:
**⋯ More → 🔌 Try demo mode (sample data)**. It launches an isolated demo window
alongside your real session (its own config, never touches real data); close it
from **⋯ More → ← Close demo**. Firing a note there is simulated locally (it lands
in the Action log), while email/text/Salesforce-refresh show a "🔌 Demo" note.

## Building a distributable

```powershell
python build.py           # PyInstaller → dist\CaseloadNotes\ + CaseloadNotes-vX.Y.Z.zip
python build_nuitka.py    # Nuitka (slower build, less SAC-flagged)
```

Zip the output folder (build.py does this for you) and upload it to Releases.

## License

See [LICENSE](LICENSE).
