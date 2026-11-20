# Handoff: Chrome as the automation browser fails (v0.29.0)

_2026-11-19. Reported by Jim during C949 data collection. Workaround in use: switched back to Edge._

## Symptom
After choosing **Chrome** as the automation browser (v0.29.0, "choose your browser"):
- The caseload browser shows a popup with an **"infinite loop" error**. The string isn't in
  CaseloadNotes' code (grep, `*.py`), so it comes from the page: Salesforce Lightning or the
  SSO flow.
- `coursescan: capture 600` can't be completed: the Any Course view never becomes usable.
- In the same session (log excerpt below, sanitized):
  - repeated `⚠ Not signed in to Salesforce yet`
  - `Browser connection lost — reopening… (reason: BrowserContext.cookies: Target page, context or browser has been closed)` (twice)
  - `Caseload list table didn't load in time … navigated to access.wgu.edu/pingfed/… → authenticator.pingone.com/pingid/ppm/auth`
  - `EA dashboard scrape failed: Page.goto: net::ERR_ABORTED at …/lightning/n/Essential_Actions`
  - `Learner360 activity (weekly import) stopped: couldn't open Learner360: Page.goto: Timeout 30000ms exceeded`
    (so `l360activity: C949` for the 829-student cohort didn't run; the 31-passer pull did)
  - Earlier the same morning, still on Edge: `Page.goto … interrupted by another navigation to …/secur/frontdoor.jsp?sid=…`
    (SSO frontdoor redirect while navigating to Caseload_App_Page).

**Security note:** the raw log line for the frontdoor redirect contains a live Salesforce session id
(`sid=`). Consider redacting `sid=` (and similar tokens) from logged navigation errors before they
reach the UI log, which users copy and paste.

## Repro (as reported)
1. ⚙ Settings → browser → **Chrome**; the app restarts the browser (fresh profile
   `browser_data_chrome`, never signed in).
2. Sign in when prompted (WGU SSO → PingID).
3. Load the caseload / run `coursescan: capture 600` and switch to Any Course.
4. The "infinite loop" popup appears; the browser connection is lost / pages time out.

Still unknown: exactly when the popup first appears (right after sign-in, or on opening the
caseload view). Ask Jim, or reproduce with a screenshot.

## Where to look
- `src/browser_engines.py`: engine table (Chrome: `channel: "chrome"`, `tested: False`),
  `profile_dir()` (Chrome uses `browser_data_chrome`; Edge keeps `browser_data`), `resolve()`.
- `src/config.py` ~L426: `browser_engine` / `browser_exe_path`.
- Browser restart / reconnect logic in `src/browser_worker.py` ("Browser connection lost —
  reopening…"): the restart may fire mid-SSO and kill the context during PingID, which could
  itself cause a redirect loop.
- Commits `60d3113`, `bb5933f`, `6a2c891` (the browser-choice feature).

## Hypotheses (unverified)
1. **SSO redirect loop in a fresh profile.** Salesforce ↔ PingFederate ↔ PingID bouncing because the
   new profile has no session cookies and something (the app's own navigation, or the auto
   caseload refresh/retry) interrupts the flow. Salesforce surfaces this as a loop/redirect error.
2. **App navigation racing the sign-in.** The auto-refresh ("Roster export is 24h old —
   auto-refreshing…", caseload retry every 10 s) navigates while the user is mid-login, restarting
   the SAML flow each time.
3. **Chrome launch flags / policy differences** under Playwright's `chrome` channel (third-party
   cookie or tracking-protection defaults affecting Lightning or the Learner360 SSO).
4. **The restart during the switch** ("Restarting browser…" followed immediately by a manual CSV
   refresh) leaves a closed context still in use (the `Target page, context or browser has been
   closed` errors).

## Suggested next steps
- Reproduce with Chrome, background jobs paused (no auto-refresh or retries); sign in manually and
  wait for the caseload to load. If that works, hypothesis 2 is likely: suspend app navigation
  while an SSO/PingID page is on screen.
- Capture the popup text and URL at the moment it appears (screenshot + `page.url`).
- Check that the reconnect logic doesn't reopen the context while a sign-in page is active.
- Keep Chrome labelled "untested" (and maybe off the first-run picker) until fixed.

## Minor, found at the same time
- `_course_scan_capture()` (scripts/launcher.py ~L28137) hard-codes **"C769"** in its
  instructions ("switch to 'any course' → C769"). It records whatever course is loaded, so the text
  misleads; use the course being scanned, or "your course".

## Status — 2026-11-19 (fix committed, needs a live re-test)
- **Release cancelled:** the v0.29.0 GitHub release was never published (an
  empty draft from a timed-out upload was deleted; v0.28.0 is still Latest).
  The `v0.29.0` tag exists on GitHub at the pre-fix commit and should be moved
  to the fixed commit when re-releasing.
- **Root cause found in code (hypothesis 2):** `_open_caseload_table` navigated
  to the caseload URL even while the page was mid-SSO, and background jobs
  navigated too, restarting the SAML / PingID flow. This affects ANY profile
  that isn't signed in yet, including a new user's first Edge profile, not just
  Chrome.
- **Fix (2831c62):** the worker holds navigating commands while any tab is on
  a WGU SSO / Salesforce login page ("⏸ Waiting for you to finish signing in…",
  then "✓ Signed in — continuing"); the caseload loader never navigates away
  from a sign-in page; `sid=` and similar tokens are masked in the activity log;
  the course-scan text no longer hard-codes C769. Tests: tests/test_signin_guard.py.
- **Still to verify live:** choose Chrome, sign in with PingID, and confirm the
  caseload loads with no loop popup. Hypotheses 3/4 (Chrome policy differences,
  the reconnect logic) are not addressed. Re-test before re-releasing.
