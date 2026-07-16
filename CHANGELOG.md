# Changelog

Notable changes per release. Versions follow the scheme in `src/version.py`
(MAJOR = scenarios.yaml format break, MINOR = new features, PATCH = fixes).

## 0.19.2 — 2026-07-16

- **Clear warning when email can't be sent.** Automated email sending needs
  **Outlook Classic** (the desktop app) — the "new Outlook" and Outlook on the
  web don't expose the automation it uses, and new Windows 11 PCs increasingly
  ship with only "new Outlook". The app now checks for Outlook Classic at
  startup and shows a one-time notice if it's missing, and any email failure
  raises a loud, blocking message that names the likely cause and the fix
  (install/enable Outlook Classic, or turn off the "New Outlook" switch) rather
  than a quiet log line. Notes, texts, and the caseload viewer work without
  Outlook. (A failed email was already never counted as sent.)
- **Fixed a crash** when expanding **Settings → Appearance → Display (text size
  & scaling)** — the section now opens correctly.

## 0.19.1 — 2026-07-15

- **A starter note template ships with new installs.** A fresh install now
  includes a **"Student note"** template (Purpose / Outcome / Student action
  items / Next follow-up / Summary), bound to the **Sample - quick note** action
  — so pressing its hotkey opens the fill-in form and you can see how templates
  work right away. Edit it in **Settings → Note templates**, or clear the note's
  "Default template" to type a plain note instead. (Existing installs keep their
  own actions — add templates yourself in Settings.)

## 0.19.0 — 2026-07-15

Reusable **note templates**, a faster and louder fire experience, and a batch
email fix.

### Note templates
- **Fill-in note forms.** Build reusable note templates in **Settings → Note
  templates** — each a set of labelled fields (Purpose, Attempt, Summary, …)
  with defaults. At fire time you tab through them and the filled result becomes
  the note body. A field can be a single line, a paragraph box, a **dropdown**,
  or a **date** (type it, pick from the calendar, or use quick-picks like "1
  week"). Paste a "Label: default" block to build one in seconds.
- **Choose at fire time.** A **Choose template ▾** button appears in the note
  dialog, the quick-note (＋ Note), and the batch note step — pick one and it
  fills the body. Templates are tagged by course / interaction type, so the
  right ones are **suggested** for the student you're on (with "Show all…" for
  the rest). Don't need one? Ignore the button — normal typing is unchanged.
- **Default per note.** An action's note can bind a default template (in the
  action editor) that opens its fill form automatically when it fires. Create /
  edit templates right from the picker with **＋ New** / **✎ Edit**.
- **Bold with `**text**`.** Wrap text in double asterisks to make it bold in the
  filed note (e.g. a bold `**Purpose**` label).

### Faster, louder fires
- **Notes open faster.** Firing a note against the open record (e.g. a hotkey
  action) no longer stalls a couple of seconds navigating to the Essential
  Actions tab — it reuses the startup EA scan, so the note dialog appears
  quickly.
- **You can see what it's waiting on.** While the app is blocked on Salesforce
  or Mongoose, the busy indicator — and the browser lock screen — now say
  exactly what: "waiting on Salesforce — filing the note…", "waiting on Mongoose
  — sending the text…", and so on.

### Batch email review
- **Deselecting an email no longer skips the note.** Unchecking students in the
  email review used to drop them from the *whole* action. Now it only skips the
  email — a prompt lets you keep their note/text or drop them entirely, and you
  can skip the email step for everyone while still filing notes.

### Other
- **API texting on for everyone.** Existing installs now switch to sending texts
  via the Mongoose API on first launch (matching new installs) — no more
  "text-ID export is stale" prompts from the old segment-export path.
- **Clickable Task badges.** A student's Task badges open the EMA Score Report
  even when no status shows yet (caseload data can lag the real result).
- **Open departed students.** In ⚑ Departures, click a student to open them in
  the 🗄 Archived view and review / update their saved data.

## 0.18.0 — 2026-07-14

Batch actions are now **reviewed right in the caseload viewer**, plus a
declutter pass and a few onboarding follow-ups.

### Two-step batch fire
- **Pick the students in the viewer, then Start.** Pressing a batch action no
  longer opens straight into a modal. Instead the caseload viewer **filters to
  just the students that match the batch**, with them all **pre-checked** and a
  **✓ Start / Cancel** bar. The browser is free here, so you can sort, inspect a
  student, even open their record — then deselect anyone you want to skip.
- **Start** then runs the usual **email / text / note review** over exactly the
  students you kept (you can still deselect further there); **Cancel** puts the
  viewer back the way it was. A plain note batch with nothing to review files
  straight from Start.
- **The viewer stays scoped to that batch** while it runs — the busy scrim only
  covers the browser, so you watch it work through exactly that set — and
  restores your full list (and prior search) when it finishes.

### Batch students in the viewer
- The same scoping applies to **queued** runs, and each **pending queue row gets
  a 👁 button** that previews its target students in the viewer before it runs.
- The selection / scope banner is **tinted to the action's group color** (a light
  wash) so you can tell at a glance which batch you're reviewing.

### Declutter
- **`⋯ More` overflow menus.** The main toolbar and the caseload-viewer header
  each keep the everyday buttons inline and tuck the rest (Add group, Mongoose,
  Restart browser; Departures, Momentum, Export history, Search archived) into a
  single **`⋯ More`** popup that floats over the list without reshuffling it.

### Onboarding follow-ups
- **One "Action log" tab.** Per-batch result tabs are consolidated into a single
  collapsible **Action log** (per action + course, with ✕ and *Clear all*).
- **❔ Help** getting-started dialog (a 3-step walkthrough + feature tour) with
  **A− / A+** text sizing, pinned top-right next to ■ STOP.
- **Texting via API by default** for new installs — no Mongoose segment export
  needed to start texting (existing users keep their setting).

## 0.17.0 — 2026-07-13

A **new-user friendliness** pass — the app is powerful but was overwhelming to
newcomers. This release makes it approachable out of the box (progressive
disclosure + guided discovery) without hiding any of the power.

### New-user experience
- **Clean starter actions.** A fresh install now seeds a small, course-agnostic
  sample set — a **"Sample single actions"** group (blue: a quick note, a
  follow-up email) and a **"Sample batch actions"** group (green: a welcome
  email+text batch, a Task-1-passed batch) — each clearly marked *edit or delete
  me*, every one filing a note, batches preview-on. The old course-specific
  seed templates are gone.
- **Simpler first run.** The welcome screen is just the Simple/Advanced editor
  choice — the Salesforce "Caseload Tool view" setup is **no longer pushed at
  startup** (the caseload loads from the live grid API now; the view only speeds
  up batch-email addresses and is offered just-in-time + in Settings).
- **Sensible default viewer columns.** New users see a curated set (Name,
  Preferred Name, Course Code, Term End Date, Momentum, Tasks) instead of every
  column. **"Copy my caseload view"** in the column chooser matches your
  Salesforce list view's columns.
- **Toolbar & viewer tooltips**, and both collapse to emoji-only when narrow
  (driven by the buttons' own widths) — the **viewer search box stays usable
  almost always**.

### Fixes & polish
- **Privacy:** a student's Mobile no longer falls back to a staff/CI phone
  column (which was showing the instructor's own number).
- First-run window now opens at a usable width and resizes normally (viewer
  stays shown); student-info pane no longer crowds out the list.
- Firing a single note with Submit off no longer blocks with a Proceed/Abort
  prompt — it fills for review with a gentle note (batches still confirm).
- Friendlier startup: not-signed-in shows "sign in, then ↻ Caseload" instead of
  a raw timeout dump; the Mongoose text-ID heads-up is no longer a red scare for
  users with no export; positive completions log in green.
- **Fresh-install demo** (`fresh_demo.bat`): run the brand-new-user experience
  in an isolated sandbox alongside your real instance (own config/login, no
  global hotkeys) for onboarding troubleshooting.

## 0.16.0 — 2026-07-08

A new **Action Queue** lets you line up several batch actions and run them in
sequence, plus the rebranded splash/icon are fixed up.

### Major features
- **Action Queue.** A new **Queue** tab in the activity panel. Turn on **➕ Add
  to queue** and click the batch actions you want; each one is **reviewed when
  you add it** (emails/texts/notes — exactly the usual review), and the reviewed
  work is stored. Then hit **Start** to run everything in sequence — nothing
  sends until you run it. While a run is going you can **Pause** (the current
  action finishes, then it stops so you can fire a one-off action), **Continue**,
  or **Cancel**. Each row shows its state — reviewed ○, running ●, done ✓, or
  error ✗ — and a failed action marks its row without stopping the rest. Rows
  are checkboxes, so you can uncheck one to skip it or re-run what's left later.
  Clicking an action while something's already running offers to add it to the
  queue instead of just making you wait.
  *For now the queue takes standard batch actions; branched and text-only
  actions still fire directly (queueing them is coming).*

### Fixes & polish
- **Splash logos restored.** The animated splash again shows all five
  integration logos swirling in — the **Outlook** logo and the full
  **Salesforce** wordmark were missing from the 0.15.0 art.
- **New desktop icon.** The app icon's owl was washed out at small sizes; it's
  been redrawn with darker, bolder ink on a clean light tile so it reads clearly
  in the taskbar and on the desktop.
- **Less clutter with API texting.** The manual **⬇ Texting IDs** button now
  hides when "Send texts via the Mongoose API" is on — the API resolves
  recipients and opt-in itself, so the manual segment export isn't needed (it
  stays available as a fallback when the API path is off).

## 0.15.0 — 2026-07-06

Texting can now go through Mongoose's own API (faster, no compose-modal
flakiness), one action can branch into per-condition sub-actions, success-path
steps are filterable, and the app has a new look.

### Major features
- **Texting via the Mongoose API (opt-in).** A new Settings toggle — *"Send
  texts via the Mongoose API"* — schedules texts through Mongoose's own send
  endpoint instead of driving the compose modal, so it's faster and free of the
  cold-tab compose flakiness. It resolves each recipient by their Salesforce
  Contact id, works across any department, and reads opt-in straight from
  Mongoose (`optedIn`) — cleanly **skipping anyone not actually opted in**
  instead of the old "no match" failures when Salesforce and Mongoose disagree.
  The compose modal stays the automatic fallback, and with the toggle on the
  manual segment / "text IDs" setup isn't needed.
- **Action branching.** One action can now hold conditional sub-actions
  ("branches") — e.g. a single *Welcome* that sends each student their own
  course's email/text/note. Branches are edited in a tab strip
  (rename / duplicate / color), and firing routes each student to the first
  branch they match — for single, selection, and batch fires — with fire-time
  safeguards for overlapping branches and students who match none. (Advanced;
  enable in Settings.)
- **Filter by success-path step.** Action and view filters can now target a
  student's success-path step status (done / due / blocked / skipped) — e.g.
  *"C769 · Welcome is due"*. The column picker is now a short, searchable,
  curated dropdown (success-path steps first, then your current columns, then a
  "search all…") — the same everywhere: action filters, the caseload viewer, and
  step gate / skip-when conditions.

### Also
- **Success-path completion backfill + rename-safe actions.** Renaming an action
  no longer breaks its success-path binding — the old name is kept as an alias
  and step bindings are repointed automatically. A new *"Backfill success-path
  completions from history"* (Settings) marks steps done for students who
  already had the action filed (per the note log), including under former names
  — fixing success-path filters that showed already-handled students as due.
- **Mongoose text-ID freshness safeguards.** A stale contact-id / opt-in export
  is flagged on load, auto-refreshed at startup, and prompts before a texting
  fire; students opted-in in Salesforce but not yet in the export fall back to
  the SF field (flagged "unverified" in the review) instead of being silently
  skipped. (Not used when the API text path above is on.)
- **New look.** New colored owl app icon, plus an animated splash — the
  integration logos swirl inward and settle around the owl.
- **Texting setup doc.** `docs/TEXTING_SETUP.md` explains how texting resolves
  recipients + opt-in and the optional one-time Mongoose segment setup.

## 0.14.0 — 2026-07-02

The tool now reads the caseload — and files follow-ups — through Salesforce's
own data feeds, so it no longer depends on how your list view is set up. Plus a
rebrand, a reworked action editor, smarter texting opt-in, and single-action
filtering.

### Major features
- **Whole caseload from the JSON feed (column-independent).** The caseload is
  now built from the same Aura grid response the page already fetches, not the
  CSV export — so it's complete **regardless of which columns your Salesforce
  list view shows**. Removing/rearranging columns no longer breaks find, batch,
  notes, task pass/fail, or Essential Actions. The CSV export stays an automatic
  fallback (health-gated), so if the feed ever changes, the tool reverts cleanly.
- **Essential Actions from its JSON feed.** The EA dashboard is read from its
  data feed too, fixing the "0 Essential Actions found" cases caused by a view
  missing the Student ID column. Falls back to the table scrape, and now warns
  clearly if that fallback can't read the view.
- **Follow-up date & note write-back via the API.** Setting a student's Follow-up
  Date or Note now persists through Salesforce's own save action instead of the
  flaky in-cell edit — fixing the bug where an edit silently didn't stick (it was
  saving a blank value).
- **Single-action filtering.** A new action option gates a single fire — or a
  hand-picked selection — by filter conditions: students who don't match are
  shown in a popup where you can tick any to **fire on anyway**, and the rest are
  skipped. Mutually exclusive with batch mode (batch *selects* students; this
  *gates* the ones you fire on).

### Texting
- **Opt-in now comes from Mongoose, not Salesforce.** The Salesforce
  "TextingPreference" field can say *Opted In* for a student whose SMS opt-in is
  actually off, so texts were attempted and failed. The tool now treats
  membership in your loaded Mongoose segment as the truth (with the SF field as a
  fallback only for courses you haven't exported), and lists genuinely
  not-opted-in students in the text review.

### Rebrand + UI
- **Renamed to "Caseload Tool."** WGU branding dropped throughout; new animated
  startup splash and app icon; a **Settings** screen reorganized into navigable
  tabs with an **About** page (how to report issues, links).
- **Reworked action editor.** Email, Text, and Notes are now clearly separated
  parts you **add with a blue button and remove with a gray Delete** (Notes can
  be emptied too, with a warning) — instead of checkboxes. Option checkboxes
  within a part are visually subordinate to the part itself.
- **Quit-while-busy warning** — closing the app mid-action now asks before
  interrupting a running fire/batch.
- **Filter presets fixed** — the *is within* operator (this week / this month /
  next 30 days …) now always lands on a valid preset, so a filter can't silently
  match nobody; old saved actions self-heal on load.
- **Momentum calibration chart** — thin-sample bars are drawn faint (not dashed),
  so "dashed" only ever means the predicted band.

### Project / security
- **Now source-available under a proprietary license** (previously MIT), and the
  repository was renamed to drop WGU. Local student data encryption (from 0.13.0)
  continues to protect the cache, history, and note log at rest.

### Reliability
- **Survives an offline launch.** If the network/VPN is down when the app starts,
  the browser no longer crashes the whole app — it stays open with a clear
  message so you can reconnect and ↻ Caseload.
- **Download guard** — a caseload export that drops the Student ID column is
  rejected (the previous good export is kept) rather than quietly corrupting the
  fallback.
- Added tests for the caseload guards and the EA/JSON mapping.

## 0.13.0 — 2026-06-30

A performance leap from reading Salesforce's own data feed instead of scraping
the rendered page, a full **Success Path** workflow, and a batch of reliability
fixes.

### Major features
- **Instant pass/fail via the Aura API.** The caseload page already fetches its
  whole grid from a Salesforce endpoint (`getCaseLoadMainGridData`) — every
  student's per-task pass/fail, Contact id, momentum, and more. The tool now
  reads that response directly instead of scroll-scraping the rendered list, so
  the live task pass/fail goes from **~30–40 seconds to instant** — with the
  proven scroll-scrape kept as an automatic fallback (so it can never show less
  than before). The desktop no longer stutters during the update.
- **Complete contact-id map + richer data.** From the same grid read: a complete
  Student→Contact-id map (covers texting opt-ins the segment export missed), and
  the grid's rich per-student fields (academic standing, planned graduation,
  last academic activity, course/student status, …) are layered onto the
  caseload for **history/data collection** and made available as **viewer
  fields** (opt-in via the detail panel's ⚙).
- **Instant note loading.** The student note viewer now shows *your own* notes
  for the course **instantly** (read from the same Aura feed, no navigating to
  the record), with a **⤓ All notes** button to pull everyone's via the full
  Notes History when you need it — so the common case is fast and nothing is
  hidden.
- **Success Paths.** Per-course checklists that show, per student, each step's
  status (Done / Due / Blocked / Skipped) and the recommended next action,
  right in the student detail. Steps are marked done automatically when their
  action fires, or manually (tick / skip / reset) from the panel. A new
  *record-only* action type (shown with a ✎) records a support without sending
  anything.

### Security
- **Encrypt local student data at rest (app password).** The local files that
  hold student PII — the caseload cache, history, success-path data, and note
  log — can be encrypted on disk and unlocked with an app password, so they're
  unreadable if the laptop is lost or the files are copied off it. The data is
  decrypted into place while the app runs and re-encrypted (plaintext shredded)
  on exit. Stdlib-only crypto (scrypt + HMAC-SHA256), no heavyweight dependency.
  First launch offers to turn it on; **Settings → "Require app password"** sets
  how often it's needed (every launch / after each restart / weekly — remembered
  within a boot session via Windows DPAPI), with **Change password** there too.
  Data-loss-averse by design: plaintext is shredded only after its encrypted
  copy is verified, and a crash leaves the latest data recoverable.
- **Capture scrubbing + retention** — captured network logs scrub session tokens
  before they touch disk, and old capture/probe/screenshot debug artifacts are
  auto-deleted after 7 days.

### Reliability + safety
- **Loud STOP button** — a red, always-visible button aborts a running batch or
  fire at the next safe point, including a text mid-compose (before it's sent).
- **Auto-close stray console tabs** — batches no longer leave a pile of open
  Salesforce record tabs; they're closed on refresh and at batch end.
- **Texting** — survive Mongoose's loading overlay and the first-compose
  cold-start so one timezone group can't sink the whole batch.
- **Notes** — deep-linked record retries when the note panel doesn't render on
  the first load (was an intermittent "No visible note panel" skip).
- **File notes via the Salesforce API (opt-in, on by default).** Eligible notes
  are filed through Salesforce's own note-save endpoint instead of driving the
  on-page form, which removes the intermittent "Couldn't tick the Academic
  Activity … Submit disabled" cold-start failure (and is faster). The on-page
  form remains the automatic fallback for anything not eligible (no Contact id,
  an Essential-Action-attached note, a note left unsubmitted) or if the API
  call fails, so nothing is lost. Toggle in **Settings → "File notes via the
  Salesforce API."**

### Under the hood
- Network-capture now records response bodies; an `auraprobe:` probe and the
  note-save Aura replay (`apinote:`) underpin the new API note filing above.

## 0.12.0 — 2026-06-23

Momentum calibration and a new **Data** tab, support for students who aren't on
your caseload, and a batch of viewer polish — building on the 0.11.0 notes
release.

### Major features
- **Momentum calibration** — the tool ingests WGU's "Archive (last 30 days)"
  results export (`results_archive*.csv`) into a local outcomes store and
  measures how well the **Momentum** prediction actually tracks who passes. It
  freezes each student's *entry-time* Momentum — the only fair basis, since
  Momentum self-corrects as a student progresses — and reports pass-rate vs.
  predicted band, per course. Fresh downloads are auto-detected and ingested on
  reload, with a staleness reminder. A **📈 Momentum** button opens the
  calibration report (entry-fair / entry-proxy / exit-diagnostic modes, filtered
  by course and by student course-load).
- **Data tab** — a new "Data" tab in the activity area with four views:
  **Pass-rate vs prediction** (per-course calibration bars), **At-risk**
  students (current Low / Med-Low, with juggling-course flags), **Momentum
  trajectory** (entry→exit drift), and **Pass rate over time**. Charts draw on a
  native canvas (no matplotlib, keeping the build lean), pop out to a second
  monitor, and support a course picker and preset date ranges.
- **Off-caseload students** — look up and act on students in your course who are
  assigned to another instructor: type a Student ID or email in the viewer
  search to **find and open their record via Salesforce global search**, and
  **file notes** for them through that nav path.

### Improvements
- **Viewer polish** — clicking a student email now CCs the PM, row scrolling is
  smoother, and note filling is sturdier.
- **Note subject** UI and **Success Path** foundations; a **Salesforce session
  pre-check** runs before fires.
- More robust data-collection scrapes and fresher caseload tooltips.

### Internal
- Temporary task-unlock probe wired behind a hidden viewer-search keyword
  (feature on hold pending a live example).

## 0.11.0 — 2026-06-15

Faster note filing, smoother fires, and a snappier window — building on the
0.10.0 texting release.

### Major features
- **Faster note filing (Salesforce Contact id)** — when a student's Contact id
  is known, the tool **deep-links straight to their record** (~2.6s to a ready
  note panel) instead of searching the caseload list. Ids come from the
  Mongoose segment export and are also **harvested as you work** ("collect-as-
  you-go"), so coverage grows automatically and notes/texts get faster over
  time. Gated notes ("Email from Student", which need an Academic Activity) and
  email fires keep using the reliable search path.
- **All input up front** — combined batch *and* single fires now show every
  prompt/review (scenario prompts, note edits, email review, text review) at
  the start, then send texts/emails and file notes **unattended**. No more
  reviewing texts, waiting for them to send, then being pulled back for more.

### Improvements
- **Mongoose sign-in handling** — a text-bearing action checks the Mongoose
  session **before** the review (clear "sign in, then re-fire" message + opens
  Mongoose if logged out, instead of timing out); Mongoose also opens in the
  background at startup with a sign-in heads-up. New **🐭 Mongoose** button.
- **Snappier UI** — action editors build lazily (only the one you open, not all
  ~20 at once), the activity log batches its redraws during a run, and email
  templates are cached so a batch review doesn't re-read the file per student.

### Fixes
- Note "Submit is disabled" failures — the Academic Activity checkbox tick is
  now verified and re-clicked if a Lightning re-render drops it; the note course
  comes from the caseload row so filing doesn't depend on an on-page table.

## 0.10.0 — 2026-06-14

Text messaging. An action can now **send a text** through Mongoose (the SMS
platform behind "Cadence"), alongside the existing note + email channels —
composed, reviewed, and sent entirely inside the tool. You never touch the
Mongoose UI.

### Major features
- **Send texts (single & batch)** — add a "Send text (Mongoose)" step to any
  action. The body is a plain-text template with the same `{{variables}}` as
  email (`first_name`, `preferred_name`, `course_code`, …), rendered tool-side.
  Single texts are personalized and shown in an in-app review/edit dialog;
  batches send one shared message per group. The tool drives Mongoose
  (open → pick inbox → add recipients → message → schedule) to completion.
- **Always-scheduled, with an acceptable window** — texts are scheduled (never
  sent immediately, a Mongoose limitation). Each action defines an acceptable
  **window in the student's local time** (default 10 AM–4 PM); the text goes out
  **ASAP within it** — at least ~10 min from now, rolling to the window's start
  the next day if it's already too late today.
- **Timezone-aware + smart grouping** — sends land at the right local time per
  timezone; when several timezones resolve to the *same* absolute send time
  (a wide window fired mid-day), they **merge into one Mongoose compose** for a
  faster, smoother send. Reviews now show **Today / Tomorrow / a date**.
- **Contact-ID matching (blank-mobile-proof)** — students are matched to their
  Salesforce Contact ID from a Mongoose **segment export**, so texting reaches
  students even when their caseload mobile is blank or differs. The new
  **⬇ Texting IDs** button auto-exports each caseload department's
  `all <course> students` segment, joins it to the caseload (by mobile, then
  name), and persists the map locally (SQLite); a pop-up walks you through
  creating a segment for any department that has none.
- **Combined actions** — one action can file a note **and** send an email **and**
  send a text, reviewed together.

### Improvements
- Non-opted-in students are skipped up front (faster batches); students with an
  unknown timezone default to Mountain rather than being dropped.
- **↻ Browser** one-click restart for hang recovery; firing is blocked while the
  live task pass/fail scrape is updating (prevents stalls/contention).
- Auto department switching in Mongoose before composing each group.

### Notes
- Texting needs the `tzdata` package (bundled in the build) for timezone math.

## 0.9.0 — 2026-06-10

A large feature release: Essential Actions support, live task pass/fail,
a new local caseload-history store, follow-up field editing, richer filters,
and rich paste in the email editor.

### Major features
- **Essential Actions (EA) in the caseload viewer** — scrape the EA dashboard
  and show/filter students by their Essential Action; **EA-aware single-action
  fire** (opt-in), with a per-note "attach to EA" toggle.
- **Caseload history (new)** — a local SQLite store snapshots the dynamic
  fields (Momentum, task status, follow-up, notes) on each caseload reload, at
  a **user-set interval** (Settings → Off / 6 / 8 / 12 h / Daily). A
  **Departures** view lists students who left the caseload since the last
  capture, split into *completed* (passed) vs *needs follow-up*; one-click
  **Export history to CSV** for pandas/Excel.
- **Live task pass/fail** — a background scrape reads each task cell's real
  pass / returned / in-process state (the colour the CSV export drops) and
  shows it on the grid + quick-view badges, with per-task status filters.
- **Follow-up Date & Note editing from the viewer** — set a student's
  Salesforce follow-up date and note in place (writes back to Salesforce); an
  empty note clears it.
- **Richer filters** — column-vs-column comparison (e.g. `{Task 2}` as a value)
  and inclusive date operators (on-or-before / on-or-after).
- **Email editor: rich paste** — pasting now keeps links, bold/italic/
  underline, headings and lists when the clipboard carries formatted content
  (plain-text paste is unchanged when it doesn't).
- **Template variables** — added `{{preferred_name}}` (falls back to first
  name) and a name-capitalization setting that cleans up CSV casing.

### Improvements
- Sign-in detection, the calendar picker opens near the mouse, and the grid
  updates instantly after an edit.
- Browser focuses for login on startup, then minimizes once the caseload loads.
- Batch review gets a viewer-style "select all" checkbox and a "Student (N)" tag.
- Single-instance guard + clean Playwright shutdown (fixes a launch error).
- Performance: blind filter/navigation sleeps replaced with bounded
  readiness waits.

### Fixes
- Editor Save/Done bar stays reachable when the window is wide/maximized.
- Course code now auto-fills on a main-window note fire.
- Caseload column chooser show/hide now works.
