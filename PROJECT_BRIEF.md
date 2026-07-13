# CaseloadNotes — Project Brief

## What it is

A Windows desktop tool that automates the repetitive Salesforce / Outlook /
Mongoose work of running a WGU course caseload. It drives the user's own browser
session with Playwright, so it needs **no Salesforce admin permissions** — it
does exactly what the instructor could do by hand, just instantly and in bulk.

Originally a hotkey that filled one repetitive note; now a full caseload
cockpit: a searchable/filterable student viewer with live task pass/fail and
Momentum, reusable single/panel/batch **actions** that send Outlook emails,
Mongoose texts, and/or file notes, off-caseload student handling, per-course
success paths, and more.

## Goal

Give a course instructor back the hours currently lost to Salesforce Lightning's
slowness and the same fields re-entered for every student — while keeping every
action reviewable and running safely under the instructor's own login.

## Why it matters — the time it saves

The value is not "typing faster." It's removing the **find → click → wait →
re-enter** loop that Salesforce forces for every single student. Filing one note
by hand is ~2 minutes: find the student (~10–15s), open the record and wait for
Lightning (~5–8s), open the note panel (~5s), set two dropdowns (~6s), find and
type the **course code** (~8–12s), type a subject (~5s), pick academic
activities (~10–15s), type the body (~10–30s), submit and close (~8s). The app
does all of it from a template — course code auto-detected — in ~15s of waiting.

Multiplied across a normal day on a ~227-student caseload (individual notes and
emails, periodic batch outreach, daily triage), that is **≈ 1.5–2 hours saved
per day — roughly 8–10 hours per 5-day work week**. Batch actions dominate: a
"welcome the 25 new students" run that is ~90 minutes by hand collapses to a few
reviewed minutes. See the README's *What it saves you* section for the full
step-by-step justification.

## Design decisions

- **Playwright over Selenium** — better auto-waiting, modern API.
- **Client-side, own login** — a persistent browser context; sign in to
  Salesforce/SSO once, reuse the session. No admin API access needed.
- **Caseload from the grid API** — the caseload loads from Salesforce's
  `getCaseLoadMainGridData` Aura response (pass/fail, contact ids, momentum) with
  a CSV export as fallback, so no special list-view column setup is required.
- **Reviewable + safe** — batch actions preview the recipient list before
  sending; notes can be filled-for-review (Submit off); local student data (note
  log, history, caseload) is encrypted at rest.
- **Progressive disclosure** — Simple/Advanced modes, curated default columns,
  and a small labeled sample action set, so new users aren't overwhelmed while
  power users keep everything.

## Background

The author is Senior Course Faculty in WGU's IT College. The project replaced
Pulover's macro creator, which broke on Salesforce slowness and minor UI
changes. It ships as an open-source, locally-run tool that needs no admin
permissions — distributed as a zipped Windows build via GitHub Releases.
