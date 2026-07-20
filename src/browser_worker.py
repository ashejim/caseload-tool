"""The Playwright browser worker thread.

Extracted from scripts/launcher.py as Step 4 of the launcher decomposition (see
ARCHITECTURE.md). Owns the Playwright/Chromium context in its own thread and
drives Salesforce + Mongoose, replaying their internal APIs where it can.

It talks to the App controller ONLY through the three callbacks passed to
__init__ (on_status / on_note_filed / on_multiple_matches) and its command
queue -- it holds no back-reference to App, so it stays importable and testable
on its own.
"""
import html
import os
import queue
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from src import caseload_csv, caseload_filter
from src.browser import persistent_context
from src.config import CASELOAD_URL
from src.dates import to_iso_date
from src.names import capitalize_name as _capitalize_name
from src.note_log import NoteLogEntry
from src.note_text import note_body_to_html
from src.scenarios import ScenarioConfig, run_scenario
from src.student_lookup import (
    click_caseload_row,
    gather_caseload_matches,
    gather_fuzzy_caseload_matches,
    get_active_student_name,
    lookup_caseload_student,
    _parse_mailto,
    scrape_student_email_from_page,
    typo_variants,
    wait_grid_settled as _wait_grid_settled,
    wait_record_ready as _wait_record_ready,
)


class BrowserWorker:
    SHUTDOWN = object()

    # Columns the caseload CSV MUST keep for the CSV fallback to stay usable.
    # StudentID is the join key every CSV-side path relies on; if a download
    # drops it, the anti-clobber guard rejects that export and keeps the
    # previous good CSV (see _download_caseload_csv).
    _CSV_CRITICAL_COLUMNS = ("StudentID",)

    def __init__(
        self,
        on_status: Callable[[str], None],
        on_note_filed: Callable[[NoteLogEntry], None],
        on_multiple_matches: Callable[[str, list[str]], None],
    ):
        self.q: queue.Queue = queue.Queue()
        self.on_status = on_status
        self.on_note_filed = on_note_filed
        self.on_multiple_matches = on_multiple_matches
        # Most recent LIST_MATCHES result, used by CLICK_MATCH to map a
        # chosen name back to its row locator.
        self._last_matches: list[tuple] = []
        # Network-capture mode (for discovering Salesforce's REST API
        # endpoint without speculation). When `_capture_active` is True,
        # the request listener appends every Salesforce-bound POST /
        # PATCH / PUT into `_capture_log`. App drives start/stop +
        # save-to-file. No PII safeguarding yet; user must scrub
        # before sharing.
        self._capture_active = False
        self._capture_log: list[dict] = []
        # Phase-1 API fast path: the caseload page fires the Aura action
        # `getCaseLoadMainGridData` on load, whose response carries the WHOLE
        # caseload grid (per-task pass/fail + Contact ids + momentum). We
        # passively capture that response here (see _on_response /
        # _capture_grid_data) so the bulk pass/fail scan can read it instead of
        # the slow scroll-scrape. The page fetches the grid in ~100-row PAGES,
        # so we ACCUMULATE: {"by_key": {(StudentID, CourseCode): row}, "ts":
        # epoch} or None.
        self._grid_data: Optional[dict] = None
        # Set while we temporarily switch the caseload list view to "Archive
        # (Last 30 Days)" to export it — suppresses grid-data capture so the
        # archive's (passed-student) rows don't pollute the My-Students grid
        # (pass/fail + contact-id map). See _download_outcomes_archive.
        self._suppress_grid_capture = False
        # EA dashboard JSON feed: while _ea_capture_armed (set around the EA
        # dashboard navigation), _on_response accumulates the EmployeeEvent__c
        # records the page fetches into _ea_data — so EAs can be read from JSON
        # (path 2) regardless of the EA list-view's displayed columns.
        self._ea_data = None
        self._ea_capture_armed = False
        # Freshest harvested Aura credentials (token + context) from live /aura
        # POSTs — reused to REPLAY the note-save action via fetch (no flaky
        # form). {"token","context","ts"} or None. See _harvest_aura_creds.
        self._aura_creds: Optional[dict] = None
        # Freshest Mongoose API Bearer token, harvested from any
        # sms-api.mongooseresearch.com request (rides on every call). Reused to
        # REPLAY the text-send API. {"token","ts"} or None. ~16h lifetime.
        self._mongoose_token: Optional[dict] = None
        # Opt-in (mirrors Settings.note_save_via_api): file notes through the
        # Aura note-save endpoint when eligible instead of driving the form.
        # The App keeps this in sync at startup + when settings change.
        self.note_api_enabled: bool = True
        # Opt-in (mirrors Settings.text_send_via_api): send texts via the
        # Mongoose REST API replay instead of the compose modal. Kept in sync by
        # the App. Default off; the modal is the fallback when it's off or errors.
        self.text_api_enabled: bool = False
        # One-shot diagnostic latches for the batch-email scrape path
        # in `_click_match_by_filter`. The first batch of the session
        # logs WHAT the row mailto carried (or didn't), and WHAT the
        # contact-card scrape found (or didn't), so users can paste
        # the result here when emails aren't resolving. Quiet after
        # that — chatty diagnostics on every student would drown the
        # actual progress messages.
        self._mailto_diag_logged = False
        self._contact_card_diag_logged = False
        # Whether the Mongoose compose has been "warmed up" this browser session
        # (the first compose of a cold/backgrounded tab is flaky). Reset whenever
        # a fresh Mongoose tab is opened.
        self._mongoose_warmed = False
        # `textapi:` capture: a persistent request/response recorder attached to
        # the Mongoose tab (captures BOTH tool-driven and MANUAL compose sends),
        # dumped to text_send_probe.txt for building an API-replay send. `sink`
        # is the accumulating record list; `stop` detaches it.
        self._text_capture_sink = None
        self._text_capture_stop = None
        self.ready_event = threading.Event()
        # Shared STOP signal (set by App._request_stop). Replaced with App's own
        # Event right after construction; the default keeps the worker usable
        # standalone. Long worker steps (e.g. tm.send_text) check it to abort.
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit_scenario(
        self,
        scenario: ScenarioConfig,
        course_code_override: str,
        clipboard: str = "",
        custom_bodies: Optional[dict[int, str]] = None,
        prompt_vars: Optional[dict[str, str]] = None,
        on_done: Optional[Callable[[bool], None]] = None,
        ea: Optional[tuple] = None,
    ) -> None:
        """Queue a scenario for the worker to fill notes against the
        active student. `prompt_vars` carries the user-typed values
        for any `prompts:` block in the scenario; they're substituted
        into note bodies (and email body / subject / to, handled on
        the main thread before queueing). `on_done(success)` is
        called from the worker thread when the run finishes.

        `ea` = (reason, course, close) to file the note via the student's
        Essential Action ("Add Note to EA" / "Add Note & Close EA")
        instead of the embedded note panel; None for the normal path."""
        self.q.put((
            "RUN", scenario, course_code_override, clipboard,
            custom_bodies or {}, prompt_vars or {}, on_done, ea,
        ))

    def submit_read_essential_actions(
        self, on_done: Callable[[dict], None],
    ) -> None:
        """Read the active student's open Essential Actions.
        on_done({eas:[{reason,course,event_progress,intervention}]})."""
        self.q.put(("READ_EA", on_done))

    def submit_read_ea_dashboard(
        self, on_done: Callable[[dict], None],
    ) -> None:
        """Scrape the cross-caseload Essential Actions DASHBOARD.
        on_done({eas:[{student_id,name,reason,...}]})."""
        self.q.put(("READ_EA_DASHBOARD", on_done))

    def submit_probe_unlock(self, on_done: Callable[[dict], None]) -> None:
        """TEMP: dump the open task-unlock popup + EA row icons. on_done({path})."""
        self.q.put(("PROBE_UNLOCK", on_done))

    def submit_probe_aura(self, on_done: Callable[[dict], None]) -> None:
        """PROBE: test whether the Aura token/context needed to replay the note-
        save action are reachable from page JS. on_done({path, reachable, …})."""
        self.q.put(("PROBE_AURA", on_done))

    def submit_probe_ea_feed(self, on_done: Callable[[dict], None]) -> None:
        """DISCOVERY: capture the Essential Actions dashboard's Aura data feed to
        ea_feed_probe.txt. on_done({path, captured, found, descriptor})."""
        self.q.put(("PROBE_EA_FEED", on_done))

    def submit_api_note_test(self, contact_id: str, course: str,
                             on_done: Callable[[dict], None]) -> None:
        """TEST: file a harmless Admin Note via the saveNoteCmpValues API replay
        (no UI form) for one contact. on_done({ok}|{error})."""
        self.q.put(("API_NOTE_TEST", contact_id, course, on_done))

    def submit_quick_note(self, contact_id: str, note_type: str,
                          course_code: str, subject: str, body_html: str,
                          activities: list, on_done: Callable[[dict], None]
                          ) -> None:
        """File one ad-hoc "quick note" via the saveNoteCmpValues API replay
        (no UI form) for a contact. on_done({ok}|{error})."""
        self.q.put(("QUICK_NOTE", contact_id, note_type, course_code, subject,
                    body_html, activities, on_done))

    def submit_close_record_tabs(
        self, on_done: Callable[[dict], None],
    ) -> None:
        """Close all open console record subtabs (back to the Caseload list).
        on_done({closed: N})."""
        self.q.put(("CLOSE_RECORD_TABS", on_done))

    def submit_open_contact_global(
        self, query: str, on_done: Callable[[dict], None],
    ) -> None:
        """Find a Contact ANYWHERE in Salesforce (global search) by Student ID
        / email / name and open its record — for off-caseload students.
        on_done({ok, name, contact_id} | {matches} | {error})."""
        self.q.put(("OPEN_CONTACT_GLOBAL", query, on_done))

    def submit_oc_probe(self, query: str,
                        on_done: Callable[[dict], None]) -> None:
        """PROBE: global-search a student, open their record, and dump every
        field label/value + related-list title (to locate ACI/PM/term/task/
        mobile on an off-caseload record). on_done({ok, fields, related, …})."""
        self.q.put(("OC_PROBE", query, on_done))

    def submit_open_contact_id(self, contact_id: str, name: str,
                               on_done: Callable[[dict], None]) -> None:
        """Open a specific Contact by its 003 id (deep-link) — used by the
        off-caseload search picker. on_done({ok, contact_id, name} | {error})."""
        self.q.put(("OPEN_CONTACT_ID", contact_id, name, on_done))

    def submit_enrich_offcaseload(self, contact_id: str, name: str,
                                  on_done: Callable[[dict], None]) -> None:
        """On-demand full scrape (fields + active ACI) of an already-open
        off-caseload record — the deferred 'Get info' step. Slow but reliable
        (the record is warm). on_done({ok, contact_id, name, profile} | {error})."""
        self.q.put(("ENRICH_OFFCASELOAD", contact_id, name, on_done))

    def submit_find_student(self, query: str, new_tab: bool = False,
                            raise_after: Optional[bool] = None) -> None:
        """Navigate to a student record. When `new_tab` is True the
        student opens in a fresh console subtab (leaving existing tabs
        open); otherwise the current tab is reused. `raise_after`
        controls whether the browser is pulled to the foreground when
        done — defaults to (not new_tab); pass False to navigate in the
        background (e.g. while the user is mid-dialog firing a scenario)."""
        self.q.put(("FIND", query, new_tab, raise_after))

    def submit_find_and_settle(
        self, query: str, contact_id: str,
        on_done: Callable[[dict], None],
    ) -> None:
        """Navigate to a student, then poll until the note panel is loaded.
        If `contact_id` (a 003… Salesforce id) is given, deep-link straight to
        that Contact record (skips the Caseload search); otherwise use the FAST
        find path (Shift+X switch, no reload), falling back to it if the deep
        link fails. Either way the opened record's Contact id is harvested.
        `on_done({ok, contact_id})` fires from the worker thread."""
        self.q.put(("FIND_AND_SETTLE", query, contact_id, on_done))

    def submit_list_matches(
        self, query: str, on_results: Callable[[list[str]], None],
    ) -> None:
        """Search Caseload for matching names without clicking anything.
        Stores the matches on the worker so a later CLICK_MATCH can
        resolve a chosen name back to its row. `on_results(names)` is
        called from the worker thread when done."""
        self.q.put(("LIST_MATCHES", query, on_results))

    def submit_click_match(
        self, name: str, on_done: Callable[[bool], None],
    ) -> None:
        """Click the row from the most recent LIST_MATCHES whose name
        equals `name`. Includes a brief settle-wait after navigation.
        `on_done(success)` is called from the worker thread."""
        self.q.put(("CLICK_MATCH", name, on_done))

    def submit_get_student_context(
        self,
        on_done: Callable[[Optional[dict]], None],
        name_hint: str = "",
    ) -> None:
        """Read the currently-active student's context (name, email,
        course code, PM email, etc.) from the open note panel and/or
        the Caseload table. `name_hint` is used when the caller knows
        which student they just navigated to but the note panel may
        not be open yet (e.g. straight after a find-first navigation).
        `on_done(info_dict_or_None)` is called from the worker."""
        self.q.put(("GET_STUDENT_CONTEXT", on_done, name_hint))

    def submit_fetch_notes(
        self, query: str, on_done: Callable[[dict], None],
        contact_id: str = "",
    ) -> None:
        """Open the student's record, wait for their notes to load, and
        scrape them (ALL authors). Deep-links via `contact_id` when given
        (independent of the caseload view's columns). on_done({notes, …})."""
        self.q.put(("FETCH_NOTES", query, contact_id, on_done))

    def submit_fetch_my_notes(
        self, query: str, on_done: Callable[[dict], None],
    ) -> None:
        """Fast read of the CURRENT USER's notes for the student (via the Aura
        API, no navigation). on_done({notes, source:'mine'}) or {notes: None}
        when the grid/creds aren't available (caller then loads the full
        scrape)."""
        self.q.put(("FETCH_MY_NOTES", query, on_done))

    def submit_fetch_task_status(
        self, query: str, on_done: Callable[[dict], None],
    ) -> None:
        """Row-filter the live Caseload list to `query` (a Student ID) and
        read the real task pass/fail (the cell color/title the CSV export
        drops). on_done({statuses: {"1": {state,status,date,attempts}, ...}})
        or {error}."""
        self.q.put(("FETCH_TASK_STATUS", query, on_done))

    def submit_scrape_all_task_status(
        self, on_done: Callable[[dict], None], expected_sids=None,
    ) -> None:
        """Bulk '2a' scrape: scroll-load the whole Caseload list and read
        every task cell's live pass/fail, keyed by Student ID.
        on_done({by_sid: {sid: {tnum: {...}}}, count}) or {error}.

        `expected_sids` (optional) is the set of Student IDs the caller already
        knows have a task (from the CSV's Task columns). When given, the scroll-
        load stops as soon as it has scrolled PAST all of them — no need to grind
        out the trailing 'stable' passes once every task-bearer is in hand."""
        self.q.put(("SCRAPE_ALL_TASK_STATUS", on_done, expected_sids))

    def submit_send_text(
        self, payload: dict, on_done: Callable[[dict], None],
    ) -> None:
        """Drive the Mongoose compose modal from a fired text action. `payload`:
        body, recipients (list of mobiles), inbox_label, schedule (slot dict or
        None), schedule_name, commit."""
        self.q.put(("SEND_TEXT", payload, on_done))

    def submit_arm_text_capture(self, on_done: Callable[[dict], None]) -> None:
        """Attach a persistent network recorder to the open Mongoose tab (tool
        AND manual sends) for the text-send API discovery."""
        self.q.put(("ARM_TEXT_CAPTURE", on_done))

    def submit_dump_text_capture(
        self, needle: str, on_done: Callable[[dict], None],
    ) -> None:
        """Write whatever the armed capture has recorded to text_send_probe.txt,
        flagging the request whose body carries `needle` (the message you sent)."""
        self.q.put(("DUMP_TEXT_CAPTURE", needle, on_done))

    def submit_probe_mongoose_api(
        self, on_done: Callable[[dict], None],
    ) -> None:
        """Replay-test the harvested Bearer token + fetch the send API maps
        (groupaccounts/profile/messageTypes) → mongoose_api_probe.txt."""
        self.q.put(("PROBE_MONGOOSE_API", on_done))

    def submit_send_text_api(
        self, payload: dict, on_done: Callable[[dict], None],
    ) -> None:
        """Send/schedule a text via the Mongoose REST API replay (not the modal)."""
        self.q.put(("SEND_TEXT_API", payload, on_done))

    def submit_export_segments(
        self, courses: list, on_done: Callable[[dict], None],
    ) -> None:
        """Auto-export each course's Mongoose contacts segment to a CSV the
        launcher joins to the caseload for Contact ids."""
        self.q.put(("EXPORT_SEGMENTS", list(courses), on_done))

    def submit_open_mongoose(self, on_done: Callable[[dict], None]) -> None:
        """Open (or focus) the Mongoose dashboard in the launcher's own browser
        context — so the user can sign in. on_done({ok|error})."""
        self.q.put(("OPEN_MONGOOSE", on_done))

    def submit_mongoose_login_check(
        self, on_done: Callable[[dict], None], surface: bool = True,
    ) -> None:
        """Open Mongoose if needed and report whether the session is signed in.
        `surface` brings the window forward (for sign-in) when it isn't;
        startup passes False for a non-intrusive heads-up. on_done({ok})."""
        self.q.put(("MONGOOSE_LOGIN_CHECK", surface, on_done))

    def submit_salesforce_login_check(
        self, on_done: Callable[[dict], None], surface: bool = True,
    ) -> None:
        """Report whether the Salesforce session is still signed in (no
        navigation). Pre-flight before a fire sends texts/emails so an SSO
        logout aborts up front. on_done({ok})."""
        self.q.put(("SALESFORCE_LOGIN_CHECK", surface, on_done))

    def submit_restart_browser(self, on_done: Callable[[dict], None]) -> None:
        """Tear down and reopen the browser context (hang recovery). Handled in
        _session, not _dispatch_command. on_done({ok}) fires once the fresh
        browser is ready."""
        self.q.put(("RESTART_BROWSER", on_done))

    def submit_set_followup_date(
        self, query: str, date_str: str, on_done: Callable[[dict], None],
    ) -> None:
        """Row-filter the Caseload list to `query` (a Student ID) and set
        that student's Followup Date cell to `date_str` (MM/DD/YYYY).
        on_done({ok, value, error})."""
        self.q.put(("SET_FOLLOWUP_DATE", query, date_str, on_done))

    def submit_set_followup_note(
        self, query: str, note_text: str, on_done: Callable[[dict], None],
    ) -> None:
        """Row-filter the Caseload list to `query` (a Student ID) and set
        that student's Followup Note cell to `note_text`.
        on_done({ok, value, error})."""
        self.q.put(("SET_FOLLOWUP_NOTE", query, note_text, on_done))

    def submit_read_caseload_columns(
        self, on_done: Callable[[list[dict]], None],
    ) -> None:
        """Navigate to Caseload (if not already there) and return the
        list of visible columns + a sniffed type per column. Each
        entry: `{"name": str, "type": "text"|"date"|"number"}`. Used
        by the editor's filter-column dropdown."""
        self.q.put(("READ_CASELOAD_COLUMNS", on_done))

    def submit_read_all_caseload_rows(
        self,
        on_done: Callable[[list[dict]], None],
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Scroll the Caseload table to load every row, then return
        them as a list of dicts keyed by column name. Calls
        `on_progress(row_count)` periodically during the scroll loop."""
        self.q.put(("READ_ALL_CASELOAD_ROWS", on_done, on_progress))

    def submit_click_match_by_filter(
        self,
        query: str,
        on_done: Callable[[bool, dict], None],
        expected_name: str = "",
        contact_id: str = "",
    ) -> None:
        """Fast batch click: type `query` into Salesforce's row filter,
        wait for the table to narrow, then click the single matching
        row. If `contact_id` (003…) is given, DEEP-LINK straight to that
        record instead (skips the filter/click). If `expected_name` is set
        and the filter returns more than one row, only clicks if that name
        matches one — otherwise aborts. ~1.5s per call vs ~25s for the full
        DOM scan.

        on_done receives `(success, row_info)` where row_info carries
        `student_email` and `pm_email` scraped from the row's `mailto:`
        action link (empty on the deep-link path — no row to scrape) plus
        `contact_id` harvested from the opened record's URL."""
        self.q.put((
            "CLICK_MATCH_BY_FILTER", query, expected_name, contact_id, on_done,
        ))

    def submit_download_caseload_csv(
        self,
        save_path: Path,
        on_done: Callable[[bool, str], None],
    ) -> None:
        """Drive Salesforce's List View → Export UI and save the
        resulting CSV to `save_path`. `on_done(success, message)` is
        called from the worker thread when the export completes."""
        self.q.put(("DOWNLOAD_CASELOAD_CSV", save_path, on_done))

    def submit_download_outcomes_archive(
        self,
        save_path: Path,
        on_done: Callable[[bool, str], None],
    ) -> None:
        """Switch the caseload list view to 'Archive (Last 30 Days)', export it
        to `save_path`, and switch back to the prior view. `on_done(ok, msg)`
        fires from the worker thread."""
        self.q.put(("DOWNLOAD_OUTCOMES_ARCHIVE", save_path, on_done))

    def shutdown(self) -> None:
        self.q.put(self.SHUTDOWN)

    def _run(self) -> None:
        """Own the browser session(s). Normally one session for the whole app
        life; a RESTART_BROWSER command tears the context down and _session
        returns True so we reopen a fresh one (hang recovery)."""
        self._pending_restart_cb = None
        first = True
        try:
            while True:
                if not self._session(first):
                    return  # SHUTDOWN
                first = False
        except Exception as e:
            self.on_status(f"Browser worker crashed: {e}")

    def _session(self, first: bool) -> bool:
        """One browser session: open the persistent context, set up, then
        process commands until SHUTDOWN (return False) or RESTART_BROWSER
        (return True — _run reopens a fresh context). The login profile is
        persisted on disk, so a restart preserves the SSO session."""
        with persistent_context() as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if CASELOAD_URL:
                # A failed FIRST navigation (no network / VPN down / DNS can't
                # resolve the Salesforce host) must NOT crash the worker or
                # close the browser — otherwise a transient offline moment at
                # launch takes the whole app down. Log it and continue to the
                # ready state; the user can reconnect and hit ↻ Caseload.
                try:
                    page.goto(CASELOAD_URL)
                except Exception as e:
                    msg = str(e).splitlines()[0]
                    hint = ("check your network / VPN"
                            if "ERR_NAME_NOT_RESOLVED" in msg
                            or "ERR_INTERNET_DISCONNECTED" in msg
                            or "ERR_" in msg else "see the error")
                    self.on_status(
                        f"⚠ Couldn't open the caseload page — {hint}, then use "
                        "↻ Caseload. The browser is open and ready. "
                        f"({msg})")
                # TODO: popups stuck at about:blank on fresh launch — see the
                # README workaround (right/middle-click "Open link in new tab").
                # The hang clears after the first Playwright-driven action.

            # Close any tabs left over from a previous session (Edge persists
            # tabs across runs in the user-data dir). Stale tabs start in a bad
            # state and confuse _active_page. Login/cookies are preserved.
            for extra in list(ctx.pages):
                if extra is page:
                    continue
                try:
                    extra.close()
                except Exception:
                    pass
            try:
                ctx.on("request", self._on_request)
                ctx.on("response", self._on_response)
            except Exception:
                pass
            self.on_status("Browser ready." if first else "Browser restarted.")
            self.ready_event.set()
            try:
                self._raise_browser_window()
            except Exception:
                pass
            # If this session was opened to satisfy a RESTART_BROWSER request,
            # tell the caller now that the new browser is ready.
            cb = getattr(self, "_pending_restart_cb", None)
            if cb is not None:
                self._pending_restart_cb = None
                try:
                    cb({"ok": True})
                except Exception:
                    pass
            while True:
                cmd = self.q.get()
                if cmd is self.SHUTDOWN:
                    return False
                if isinstance(cmd, tuple) and cmd and cmd[0] == "RESTART_BROWSER":
                    _, on_done = cmd
                    self.ready_event.clear()
                    # Drop cached window handles — the new browser is a new
                    # process/window (they self-heal via IsWindow anyway).
                    self._browser_hwnd = None
                    self._browser_pid = None
                    self._pending_restart_cb = on_done
                    self.on_status("Restarting browser…")
                    return True  # _run reopens the context
                try:
                    self._dispatch_command(ctx, cmd)
                except Exception as e:
                    self.on_status(
                        f"Command {cmd[0]!r} failed: {e}. Worker still "
                        "running; use \u21bb Restart browser if it's stuck."
                    )

    def _dispatch_command(self, ctx, cmd) -> None:
        """Dispatch one queued command. Each branch is responsible for
        firing any callbacks it owes the caller (in a try/finally) so
        a partial failure doesn't leave the main thread waiting on a
        wait_variable forever."""
        if cmd[0] == "RUN":
            (_, scenario, override, clipboard, custom_bodies,
             prompt_vars, on_done) = cmd[:7]
            ea = cmd[7] if len(cmd) > 7 else None
            success = False
            try:
                success = self._handle_run(
                    ctx, scenario, override, clipboard,
                    custom_bodies=custom_bodies,
                    prompt_vars=prompt_vars, ea=ea,
                )
            finally:
                if on_done is not None:
                    on_done(success)
        elif cmd[0] == "READ_EA":
            _, on_done = cmd
            res = {}
            try:
                res = self._read_essential_actions(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "READ_EA_DASHBOARD":
            _, on_done = cmd
            res = {}
            try:
                res = self._read_ea_dashboard(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "PROBE_UNLOCK":
            _, on_done = cmd
            res = {}
            try:
                res = self._probe_unlock_action(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "PROBE_AURA":
            _, on_done = cmd
            res = {}
            try:
                res = self._probe_aura(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "PROBE_EA_FEED":
            _, on_done = cmd
            res = {}
            try:
                res = self._probe_ea_feed(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "API_NOTE_TEST":
            _, contact_id, course, on_done = cmd
            res = {}
            try:
                res = self._save_note_via_api(
                    ctx, contact_id=contact_id, note_type="Admin Note",
                    course_code=course, subject="Admin Note",
                    body_html="<p>API replay test — safe to delete.</p>",
                    activities=[])
            finally:
                on_done(res)
        elif cmd[0] == "QUICK_NOTE":
            (_, contact_id, note_type, course_code, subject, body_html,
             activities, on_done) = cmd
            res = {}
            try:
                res = self._save_note_via_api(
                    ctx, contact_id=contact_id, note_type=note_type,
                    course_code=course_code, subject=subject,
                    body_html=body_html, activities=activities)
            finally:
                on_done(res)
        elif cmd[0] == "CLOSE_RECORD_TABS":
            _, on_done = cmd
            res = {"closed": 0}
            try:
                res = {"closed": self._close_all_record_subtabs(ctx)}
            finally:
                on_done(res)
        elif cmd[0] == "OPEN_CONTACT_GLOBAL":
            _, query, on_done = cmd
            res = {}
            try:
                res = self._open_contact_by_global_search(ctx, query)
            finally:
                on_done(res)
        elif cmd[0] == "OC_PROBE":
            _, query, on_done = cmd
            res = {}
            try:
                res = self._oc_probe(ctx, query)
            finally:
                on_done(res)
        elif cmd[0] == "OPEN_CONTACT_ID":
            # Open ONLY — navigate to the record (so a quick note can file) but
            # DON'T scrape the profile/ACI here: that round-trip (fields +
            # related-list reload) is slow and races the cold record load, so it
            # came back empty on a first open. The viewer shows minimal info
            # instantly; the full scrape runs on demand via ENRICH_OFFCASELOAD.
            _, cid, name, on_done = cmd
            res = {}
            try:
                ok = self._navigate_to_contact(ctx, cid)
                if ok:
                    res = {"ok": True, "contact_id": cid, "name": name,
                           "profile": {"aci": []}}
                else:
                    res = {"error": f"couldn't open {name or cid}"}
            finally:
                on_done(res)
        elif cmd[0] == "ENRICH_OFFCASELOAD":
            # On-demand full scrape (fields + active ACI) of an already-open
            # off-caseload record — the deferred "Get info" step. The record is
            # warm by now, and we re-navigate first to be sure it's on /view.
            _, cid, name, on_done = cmd
            res = {}
            try:
                self._navigate_to_contact(ctx, cid)
                profile = self._scrape_offcaseload_profile(ctx, cid, name)
                res = {"ok": True, "contact_id": cid, "name": name,
                       "profile": profile}
            except Exception as e:
                res = {"error": f"couldn't read info for {name or cid}: {e}"}
            finally:
                on_done(res)
        elif cmd[0] == "FIND":
            # Back-compat: older callers queued ("FIND", query) with no
            # new_tab / raise_after flags.
            query = cmd[1]
            new_tab = cmd[2] if len(cmd) > 2 else False
            raise_after = cmd[3] if len(cmd) > 3 else None
            self._handle_find(ctx, query, new_tab, raise_after)
        elif cmd[0] == "FIND_AND_SETTLE":
            _, query, contact_id, on_done = cmd
            res = {"ok": False, "contact_id": ""}
            try:
                # Fast path: deep-link straight to the Contact record when we
                # have its id (skips the Caseload search + its flake).
                if contact_id:
                    res["ok"] = self._navigate_to_contact(ctx, contact_id)
                if not res["ok"]:
                    # No id, or deep link didn't land — fall back to the FAST
                    # find path (Shift+X switch, no reload), in the background.
                    self._handle_find(ctx, query, new_tab=False,
                                      raise_after=False)
                    # Poll until the note panel is actually loaded — exits as
                    # soon as get_active_student_name resolves, up to ~5s.
                    target = self._active_page(ctx)
                    if target is not None:
                        for _ in range(25):
                            try:
                                if get_active_student_name(target):
                                    res["ok"] = True
                                    break
                            except Exception:
                                pass
                            try:
                                target.wait_for_timeout(200)
                            except Exception:
                                break
                # Collect-as-you-go: harvest the opened record's Contact id from
                # its URL so an un-mapped student is fast next time.
                if res["ok"]:
                    try:
                        tgt = self._active_page(ctx)
                        m = re.search(r"/Contact/(003[0-9A-Za-z]{12,15})",
                                      (tgt.url if tgt else "") or "")
                        if m:
                            res["contact_id"] = m.group(1)
                    except Exception:
                        pass
            finally:
                on_done(res)
        elif cmd[0] == "LIST_MATCHES":
            _, query, on_results = cmd
            names: list[str] = []
            try:
                names = self._list_matches(ctx, query)
            finally:
                on_results(names)
        elif cmd[0] == "CLICK_MATCH":
            _, name, on_done = cmd
            success = False
            try:
                success = self._click_match_by_name(ctx, name)
                if success:
                    tgt = self._active_page(ctx)
                    if tgt is not None:
                        try:
                            _wait_record_ready(tgt, 2000)
                        except Exception:
                            pass
                        self._bring_browser_forward(tgt)
            finally:
                on_done(success)
        elif cmd[0] == "GET_STUDENT_CONTEXT":
            _, on_done, name_hint = cmd
            info: Optional[dict] = None
            try:
                info = self._read_student_context(ctx, name_hint)
            finally:
                on_done(info)
        elif cmd[0] == "FETCH_NOTES":
            _, query, contact_id, on_done = cmd
            res = {}
            try:
                res = self._fetch_student_notes(ctx, query,
                                                contact_id=contact_id)
            finally:
                on_done(res)
        elif cmd[0] == "FETCH_MY_NOTES":
            _, query, on_done = cmd
            res = {"notes": None}
            try:
                res = self._fetch_my_notes(ctx, query)
            finally:
                on_done(res)
        elif cmd[0] == "FETCH_TASK_STATUS":
            _, query, on_done = cmd
            res = {}
            try:
                res = self._fetch_task_status(ctx, query)
            finally:
                on_done(res)
        elif cmd[0] == "SCRAPE_ALL_TASK_STATUS":
            _, on_done, expected_sids = cmd
            res = {}
            try:
                res = self._scrape_all_task_status(ctx, expected_sids)
            finally:
                on_done(res)
        elif cmd[0] == "SEND_TEXT":
            _, payload, on_done = cmd
            res = {}
            try:
                res = self._send_text(ctx, payload)
            finally:
                on_done(res)
        elif cmd[0] == "ARM_TEXT_CAPTURE":
            _, on_done = cmd
            res = {}
            try:
                res = self._arm_text_capture_persistent(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "DUMP_TEXT_CAPTURE":
            _, needle, on_done = cmd
            res = {}
            try:
                res = self._dump_text_capture_persistent(needle)
            finally:
                on_done(res)
        elif cmd[0] == "PROBE_MONGOOSE_API":
            _, on_done = cmd
            res = {}
            try:
                res = self._probe_mongoose_api(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "SEND_TEXT_API":
            _, payload, on_done = cmd
            res = {}
            try:
                res = self._send_text_via_api(ctx, payload)
            finally:
                on_done(res)
        elif cmd[0] == "EXPORT_SEGMENTS":
            _, courses, on_done = cmd
            res = {}
            try:
                res = self._export_segments(ctx, courses)
            finally:
                on_done(res)
        elif cmd[0] == "OPEN_MONGOOSE":
            _, on_done = cmd
            res = {}
            try:
                res = self._open_mongoose(ctx, focus=True)
            finally:
                on_done(res)
        elif cmd[0] == "MONGOOSE_LOGIN_CHECK":
            _, surface, on_done = cmd
            res = {"ok": False}
            try:
                res = self._mongoose_login_check(ctx, surface)
            finally:
                on_done(res)
        elif cmd[0] == "SALESFORCE_LOGIN_CHECK":
            _, surface, on_done = cmd
            res = {"ok": False}
            try:
                res = self._salesforce_login_check(ctx, surface)
            finally:
                on_done(res)
        elif cmd[0] == "SET_FOLLOWUP_DATE":
            _, query, date_str, on_done = cmd
            res = {}
            try:
                res = self._set_followup_date(ctx, query, date_str)
            finally:
                on_done(res)
        elif cmd[0] == "SET_FOLLOWUP_NOTE":
            _, query, note_text, on_done = cmd
            res = {}
            try:
                res = self._set_followup_note(ctx, query, note_text)
            finally:
                on_done(res)
        elif cmd[0] == "READ_CASELOAD_COLUMNS":
            _, on_done = cmd
            cols: list[dict] = []
            try:
                cols = self._read_caseload_columns(ctx)
            finally:
                on_done(cols)
        elif cmd[0] == "READ_ALL_CASELOAD_ROWS":
            _, on_done, on_progress = cmd
            rows: list[dict] = []
            try:
                rows = self._read_all_caseload_rows(
                    ctx, on_progress=on_progress,
                )
            finally:
                on_done(rows)
        elif cmd[0] == "CLICK_MATCH_BY_FILTER":
            _, query, expected_name, contact_id, on_done = cmd
            success = False
            row_info: dict = {"pm_email": "", "student_email": "",
                              "contact_id": ""}
            try:
                if contact_id:
                    # Deep-link straight to the record (no row to scrape).
                    success = self._navigate_to_contact(ctx, contact_id)
                if not success:
                    # No id, or deep link didn't land — the filter+click path
                    # (which also owns its post-click settle + card scrape).
                    success, row_info = self._click_match_by_filter(
                        ctx, query, expected_name=expected_name,
                    )
                # Collect-as-you-go: harvest the opened record's Contact id.
                if success:
                    try:
                        tgt = self._active_page(ctx)
                        m = re.search(r"/Contact/(003[0-9A-Za-z]{12,15})",
                                      (tgt.url if tgt else "") or "")
                        if m:
                            row_info["contact_id"] = m.group(1)
                    except Exception:
                        pass
            finally:
                on_done(success, row_info)
        elif cmd[0] == "DOWNLOAD_CASELOAD_CSV":
            _, save_path, on_done = cmd
            success, message = False, ""
            try:
                success, message = self._download_caseload_csv(
                    ctx, save_path,
                )
            finally:
                on_done(success, message)
        elif cmd[0] == "DOWNLOAD_OUTCOMES_ARCHIVE":
            _, save_path, on_done = cmd
            success, message = False, ""
            try:
                success, message = self._download_outcomes_archive(
                    ctx, save_path,
                )
            finally:
                on_done(success, message)
    def _try_match_or_navigate(self, target, query: str,
                               raise_after: bool = True) -> bool:
        """Look for matches in the current DOM. If exactly one
        highest-priority match, click and return True. If multiple,
        post them to the main thread for a picker and return True
        (handled async). Returns False if nothing matched."""
        matches = gather_caseload_matches(target, query, on_status=self.on_status)
        if not matches:
            return False
        best_priority = matches[0][0]
        top = [m for m in matches if m[0] == best_priority]
        if len(top) == 1:
            priority, row, name, name_idx = top[0]
            self.on_status(f"  [search] match: {name!r} (priority {priority})")
            if click_caseload_row(row, name, name_idx, on_status=self.on_status):
                self.on_status(f"Navigated to {name!r}.")
                # Raise only AFTER the navigation click — see _handle_find.
                # Suppressed for new-tab opens (background, by convention).
                if raise_after:
                    self._bring_browser_forward(target)
                return True
            # Found the row but the click didn't route (Lightning
            # active-view race). Report False so _handle_find falls
            # through to re-activate the list and retry, rather than
            # stopping on a false success.
            self.on_status(
                f"  [search] found {name!r} but clicking it didn't open the "
                "record; re-activating the list to retry."
            )
            return False
        # Multiple matches at the same priority — ask user to pick.
        names = [m[2] for m in top]
        self.on_status(
            f"  [search] {len(names)} matches: {', '.join(names)}"
        )
        self.on_multiple_matches(query, names)
        return True

    def _close_record_subtab(self, target) -> bool:
        """Close the currently-open console workspace subtab (the student
        record) with the Shift+X console shortcut, so the live Caseload
        list underneath becomes active again — far faster than reloading
        it. Returns True only once we're back on the Caseload app page;
        the caller falls back to a full reload otherwise (e.g. focus was
        in a field so the shortcut didn't fire, or another record tab
        was underneath)."""
        try:
            # Right after a row click, focus is on the console (not a text
            # field), so the global Shift+X shortcut fires the tab close.
            target.keyboard.press("Shift+X")
        except Exception as e:
            self.on_status(f"  [debug] close-subtab keypress: {e}")
            return False
        # Poll briefly for the URL to fall back to the Caseload app page.
        try:
            # ~1.5s ceiling before we give up and reload. A successful
            # Shift+X re-activates the list well under a second; waiting
            # longer just delays the reload fallback when there was no
            # closeable record (e.g. focus wasn't on the console).
            for _ in range(10):
                if "Caseload_App_Page" in (target.url or ""):
                    return True
                target.wait_for_timeout(150)
        except Exception:
            pass
        return False

    def _close_all_record_subtabs(self, ctx) -> int:
        """Close all open console tabs via the Salesforce console shortcut
        'Shift+W then X' (close all tabs), then make sure the Caseload list is
        back. Batches that deep-link to records leave a workspace subtab open
        per student; across runs they pile up and each keeps its own Lightning
        DOM + components alive (real browser memory/CPU).

        Focus may be sitting in a note field where the shortcut won't fire, so
        we blur with Escape first. The 'close all' can drop the Caseload tab
        too, so we re-open the list afterward if it's gone. Returns 1 if it ran
        (best-effort — the console shortcut doesn't report a count), 0 if there
        was no page. Never raises."""
        target = self._active_page(ctx)
        if target is None:
            return 0
        try:
            target.keyboard.press("Escape")    # blur any focused field/editor
            target.wait_for_timeout(120)
            target.keyboard.press("Shift+W")   # SF console close-all: leader…
            target.wait_for_timeout(180)
            target.keyboard.press("x")         # …then X = close all tabs
            target.wait_for_timeout(700)
        except Exception:
            return 0
        # 'Close all' can take the Caseload tab with it — restore the list so
        # the app still has it to search/click against.
        try:
            if "Caseload_App_Page" not in (target.url or ""):
                self._ensure_caseload_list(target)
        except Exception:
            pass
        return 1

    def _ensure_caseload_list(self, target) -> bool:
        """Navigate `target` to the Caseload list and wait for the list
        table to render. The table must have BOTH a Course Code header
        AND a Name header — the Essential Actions panels match Course
        Code only, so a looser wait would settle on a stale empty table.
        Returns True once the real list is visible."""
        if not CASELOAD_URL:
            return False
        # Lightning sometimes raises "Navigation interrupted" when its own
        # JS triggers a redirect during our goto. The navigation still
        # ultimately succeeds, so we treat the exception as advisory.
        try:
            target.goto(CASELOAD_URL, wait_until="domcontentloaded")
        except Exception as e:
            self.on_status(f"  [debug] goto caseload: {e}")
        try:
            list_table = (
                target.locator("table")
                .filter(has=target.locator('th:has-text("Course Code")'))
                .filter(has=target.locator('th:has-text("Name")'))
            )
            list_table.first.wait_for(state="visible", timeout=20_000)
            return True
        except Exception as e:
            self.on_status(f"Caseload list table didn't load in time: {e}")
            return False

    def _caseload_table_present(self, target) -> bool:
        """Cheap, no-wait check: is the Caseload list table already in the
        DOM right now? Lets _handle_find skip a wasteful full reload when
        the list is plainly present and the student just isn't in the
        ~10 visible rows (the row filter, not a reload, finds those)."""
        try:
            return (
                target.locator("table")
                .filter(has=target.locator('th:has-text("Course Code")'))
                .filter(has=target.locator('th:has-text("Name")'))
                .count() > 0
            )
        except Exception:
            return False

    # Shadow-DOM-piercing scan for record links (Lightning's search results
    # render inside web components). Search-result rows link as
    # /lightning/r/<id>/view (NO object name); the open console tab links as
    # /lightning/r/Contact/<id>/view — the caller's regex keeps only Contact
    # ids (003…) right after /r/, which is the result row, not the tab.
    _DEEP_CONTACT_LINKS_JS = """
      () => {
        const out = [], seen = new Set();
        const visit = (root) => {
          for (const a of root.querySelectorAll('a[href*="/lightning/r/"]')) {
            const href = a.getAttribute('href') || '';
            if (href && !seen.has(href)) {
              seen.add(href);
              out.push({href, text: (a.textContent || '').trim().slice(0, 120)});
            }
          }
          for (const el of root.querySelectorAll('*'))
            if (el.shadowRoot) visit(el.shadowRoot);
        };
        visit(document);
        return out.slice(0, 60);
      }
    """

    def _open_contact_by_global_search(self, ctx, query: str) -> dict:
        """Resolve `query` (Student ID / email / name) to a Contact via
        Salesforce's GLOBAL search, then deep-link to that record (note panel
        ready) — the way to reach students outside the user's caseload.

        Drives Salesforce's own search navigation: the `/one/one.app#<base64>`
        URL with componentDef `forceSearch:searchPageDesktop` and the query as
        `term` (mirrors what the browser's search bar produces). Returns
        {ok, name, contact_id}; {matches:[{contact_id,name}…]} when ambiguous;
        or {error}."""
        import base64
        import json as _json
        from urllib.parse import urlsplit
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        try:
            p = urlsplit(CASELOAD_URL or "https://srm.lightning.force.com")
            base = f"{p.scheme}://{p.netloc}"
        except Exception:
            base = "https://srm.lightning.force.com"
        payload = {
            "componentDef": "forceSearch:searchPageDesktop",
            "attributes": {
                "term": query,
                "scopeMap": {"type": "TOP_RESULTS"},
                "context": {"FILTERS": {}, "searchSource": "ASSISTANT_DIALOG"},
                "groupId": "DEFAULT",
            },
            "state": {},
        }
        enc = base64.b64encode(
            _json.dumps(payload, separators=(",", ":")).encode()).decode()
        self.on_status(f"Salesforce search for {query!r}…")

        def _on_results() -> bool:
            """True once the page is the forceSearch results view (not Home /
            recently-viewed). Distinguishes a real search from the cold-goto
            fallback that lands on Home and shows recently-viewed records."""
            try:
                t = (target.title() or "")
            except Exception:
                t = ""
            if "Search" in t:
                return True
            # The results page also carries the term in its URL hash.
            try:
                if enc[:24] in (target.url or ""):
                    # On the search route — but Home can briefly share the
                    # hash; require a results container too.
                    return bool(target.evaluate(
                        """() => {
                          const has = (root) => {
                            if (root.querySelector('.slds-grid_search-results, '
                                + '[data-aura-class*=\"forceSearchResults\"], '
                                + 'records-record-layout, .search-results')) return true;
                            for (const el of root.querySelectorAll('*'))
                              if (el.shadowRoot && has(el.shadowRoot)) return true;
                            return false;
                          };
                          return has(document);
                        }"""))
            except Exception:
                pass
            return False

        # Drive the search the way the UI does — load the one.app shell, then
        # set the search hash so the SPA fires its hashchange handler (a cold
        # goto to the search URL just lands on Home / recently-viewed).
        try:
            target.goto(f"{base}/one/one.app#{enc}",
                        wait_until="domcontentloaded", timeout=30_000)
            target.wait_for_timeout(2500)
            if not _on_results():
                # Re-fire via an explicit hashchange (and a slightly different
                # hash first to force the event even if the value matches).
                target.evaluate(
                    "(h) => { window.location.hash = ''; window.location.hash = h; }",
                    enc)
                target.wait_for_timeout(3000)
        except Exception as e:
            return {"error": f"search navigation failed: {e}"}
        # Guard: if we never reached a results page, DON'T scrape — that would
        # open a recently-viewed record (the wrong student).
        if not _on_results():
            return {"error": "Salesforce didn't open the search results page "
                    f"for {query!r} (landed on Home). Try again, or open the "
                    "student manually in the browser."}
        # Poll for the Contact result row to render (~up to 10s). The result
        # row links as /lightning/r/<id>/view (id straight after /r/) — that
        # regex captures the Contact result and skips the open console tab,
        # which uses /lightning/r/Contact/<id>/view (object name after /r/).
        contacts: list = []
        for _ in range(20):
            try:
                links = target.evaluate(self._DEEP_CONTACT_LINKS_JS)
            except Exception:
                links = []
            seen: dict = {}
            for lk in (links or []):
                m = re.search(r"/lightning/r/(003[0-9A-Za-z]{12,15})/view",
                              lk.get("href", "") or "")
                if not m:
                    continue
                cid = m.group(1)
                if cid in seen:
                    continue
                name = (lk.get("text", "") or "").split("|")[0].strip()
                if name.lower().startswith("contact"):
                    name = name[len("contact"):].strip()
                seen[cid] = name
            if seen:
                contacts = list(seen.items())
                break
            try:
                target.wait_for_timeout(500)
            except Exception:
                pass
        if not contacts:
            return {"error": f"no Salesforce Contact found for {query!r}"}
        ql = query.strip().lower()
        is_name = (bool(re.search(r"[A-Za-z]", query)) and "@" not in query
                   and not re.fullmatch(r"\d{5,12}", query.strip()))
        exact = [(c, n) for c, n in contacts if (n or "").strip().lower() == ql]
        if is_name:
            # A name is NOT a unique key (Salesforce also matches on surname, and
            # two people can share a name), so we can't safely auto-open. ALWAYS
            # return the full list for the user to pick from.
            return {"matches": [{"contact_id": c, "name": n}
                                for c, n in contacts]}
        if len(contacts) > 1:
            # ID/email is unique enough to open; if several came back, prefer an
            # exact name match, else show the picker.
            if len(exact) == 1:
                contacts = exact
            else:
                return {"matches": [{"contact_id": c, "name": n}
                                    for c, n in contacts]}
        cid, name = contacts[0]
        if self._navigate_to_contact(ctx, cid):
            return {"ok": True, "contact_id": cid, "name": name or query}
        return {"error": f"found {name or query} but couldn't open the record"}

    def _navigate_to_contact(self, ctx, contact_id: str) -> bool:
        """Deep-link straight to a Contact record (skips the Caseload search)
        and wait until the note panel is ready. Returns True if the panel
        appeared. Verified ~2.6s end-to-end vs. the slower search+click; the
        record loads in the standard app but the note panel still renders."""
        target = self._active_page(ctx)
        if target is None or not contact_id:
            return False
        from urllib.parse import urlsplit
        from src import selectors
        parts = urlsplit(CASELOAD_URL or "https://srm.lightning.force.com")
        url = (f"{parts.scheme}://{parts.netloc}"
               f"/lightning/r/Contact/{contact_id}/view")
        self.on_status(
            "Deep-link: opening the record by Contact id (skipping search)…")
        # The note panel occasionally doesn't render on the first deep-link (a
        # Lightning load-timing flake — the "No visible note panel" skip), but a
        # fresh load reliably brings it up. So try twice: load, wait for the
        # panel; if it doesn't appear, RELOAD the record and wait again before
        # giving up.
        for attempt in range(2):
            try:
                if attempt == 0:
                    target.goto(
                        url, wait_until="domcontentloaded", timeout=30_000)
                else:
                    self.on_status(
                        "  note panel didn't render — reloading the record…")
                    target.reload(
                        wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                continue
            # Wait for the note panel to be rendered. Anchor on the note BODY
            # EDITOR — an actual form field fill_note needs — not on the student
            # name header, whose text ("…Note for ") is split across nodes on
            # the standalone record view and so matches unreliably there. First
            # attempt waits a shorter beat so a flake retries sooner; the reload
            # attempt waits the full timeout.
            try:
                selectors.note_body_editor(target).wait_for(
                    state="visible", timeout=(12_000 if attempt == 0 else 20_000))
                # Give the form a moment to become INTERACTIVE (the type
                # <select> enables a beat after the panel paints). fill_note
                # re-checks this, but waiting here hands back a warmer form —
                # fewer races on the reactive Academic Activity gate for
                # "Email from Student" notes.
                try:
                    selectors.interaction_type_select(target).wait_for(
                        state="visible", timeout=4_000)
                    target.wait_for_timeout(300)
                except Exception:
                    pass
                return True
            except Exception:
                pass
            # Fall back to the generic Submit button as a last resort.
            try:
                selectors.submit_button(target).wait_for(
                    state="visible", timeout=3_000)
                return True
            except Exception:
                pass
        return False

    # Shadow-piercing dump of ALL visible text on a record page (in DOM order,
    # so a field's label is immediately followed by its value) + related-list
    # titles — a blind first-pass map to locate ACI (assigned CI) / PM / term /
    # task / mobile on an off-caseload student's record.
    _OC_PROBE_JS = r"""
    () => {
      const snippets = [], related = [], seen = new Set();
      const clean = s => (s||'').replace(/\s+/g,' ').trim();
      const walk = (node) => {
        if (!node) return;
        if (node.nodeType === 1) {
          const cls = (node.className && node.className.toString)
                        ? node.className.toString() : '';
          const sig = (node.tagName||'').toLowerCase() + ' ' + cls;
          if (/related-list|forcerelatedlist|slds-card__header-title/.test(sig)) {
            const t = node.querySelector
                        ? (node.querySelector('span[title], h2, h3') || node) : node;
            const txt = clean(t.innerText || t.textContent);
            if (txt && txt.length < 80) {
              const k = 'R:' + txt;
              if (!seen.has(k)) { seen.add(k); related.push(txt); }
            }
          }
          if (node.shadowRoot)
            for (const c of node.shadowRoot.childNodes) walk(c);
          for (const c of node.childNodes) walk(c);
        } else if (node.nodeType === 3) {
          const t = clean(node.textContent);
          if (t && t.length < 200 && snippets[snippets.length - 1] !== t)
            snippets.push(t);
        }
      };
      try { walk(document.body); } catch(e) {}
      return {snippets: snippets.slice(0, 1600), related};
    }
    """

    # Click the first tab/link whose text contains one of `labels` (shadow-
    # pierced) — used to reveal the "Course Mentor Student Assignments" tab.
    _OC_CLICK_TAB_JS = r"""
    (labels) => {
      const clean = s => (s||'').replace(/\s+/g,' ').trim();
      const cands = [];
      const walk = (root) => {
        let els; try { els = root.querySelectorAll('a,button,[role="tab"],li,span'); }
          catch(e){ return; }
        for (const el of els) cands.push(el);
        let all; try { all = root.querySelectorAll('*'); } catch(e){ return; }
        for (const el of all) if (el.shadowRoot) walk(el.shadowRoot);
      };
      walk(document);
      for (const label of labels) {
        const ll = label.toLowerCase();
        for (const el of cands) {
          const t = clean(el.innerText || el.textContent);
          if (t && t.length < 70 && t.toLowerCase().includes(ll)) {
            try { el.scrollIntoView(); el.click(); return t; } catch(e){}
          }
        }
      }
      return null;
    }
    """

    # Dump every table/grid on the page (shadow-pierced): header row + data rows
    # (cells joined by ' | '), flagging cells that carry a check/success icon
    # with [✓] — to capture "Course Code | Course Mentor | (active ✓)".
    _OC_TABLES_JS = r"""
    () => {
      const clean = s => (s||'').replace(/\s+/g,' ').trim();
      const grids = [];
      const collect = (root) => {
        let gs; try { gs = root.querySelectorAll('table,[role="grid"],[role="table"]'); }
          catch(e){ return; }
        for (const g of gs) grids.push(g);
        let all; try { all = root.querySelectorAll('*'); } catch(e){ return; }
        for (const el of all) if (el.shadowRoot) collect(el.shadowRoot);
      };
      collect(document);
      const out = [];
      for (const g of grids) {
        const headers = [];
        g.querySelectorAll('th,[role="columnheader"]').forEach(h => {
          const t = clean(h.innerText || h.textContent);
          if (t && t.length < 40) headers.push(t);
        });
        const rows = [];
        g.querySelectorAll('tr,[role="row"]').forEach(tr => {
          const cells = [];
          tr.querySelectorAll('td,[role="gridcell"],[role="cell"]').forEach(td => {
            let t = clean(td.innerText || td.textContent);
            const icon = td.querySelector('lightning-icon,[icon-name],svg,img[alt]');
            if (icon) {
              const meta = (icon.getAttribute && (icon.getAttribute('icon-name')
                || icon.getAttribute('title') || icon.getAttribute('alt')
                || icon.getAttribute('aria-label'))) || '';
              if (/check|success|true|yes|selected/i.test(meta)) t = (t ? t + ' ' : '') + '[✓]';
            }
            cells.push(t);
          });
          if (cells.some(c => c)) rows.push(cells.join(' | '));
        });
        if (rows.length) out.push({headers: headers.join(' | '),
                                   rows: rows.slice(0, 50)});
      }
      return out.slice(0, 12);
    }
    """

    # Scroll a related-list panel (matched by a title containing `needle`) into
    # view so Lightning lazy-renders its rows before we dump. Returns the title
    # text if found. Also nudges any "View All" so more than the default 5 show.
    _OC_REVEAL_JS = r"""
    (needle) => {
      const nl = needle.toLowerCase();
      const matches = [];
      const walk = (root) => {
        let els; try { els = root.querySelectorAll('*'); } catch(e){ return; }
        for (const el of els) {
          const t = (el.innerText || el.textContent || '');
          if (t && t.length < 200 && t.toLowerCase().includes(nl)) matches.push(el);
          if (el.shadowRoot) walk(el.shadowRoot);
        }
      };
      walk(document);
      if (!matches.length) return null;
      matches.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
      const hit = matches[0];
      try { hit.scrollIntoView({block: 'center'}); } catch(e) {}
      return (hit.innerText || hit.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    }
    """

    def _oc_probe(self, ctx, query: str) -> dict:
        """Open `query`'s record and dump the page. `query` may carry a click
        path after '||' — e.g. 'Liberty || Processes and Interactions || Course
        Mentor Student Assignments' clicks each tab in order (to reach nested
        tabs like the Course Mentor / ACI list) before dumping."""
        parts = [s.strip() for s in (query or "").split("||")]
        squery = parts[0]
        click_seq = [p for p in parts[1:] if p]
        res = self._open_contact_by_global_search(ctx, squery)
        if not res.get("ok"):
            matches = res.get("matches")
            if not matches:
                return res
            ql = squery.strip().lower()
            pick = next((m for m in matches
                         if (m.get("name") or "").strip().lower() == ql), matches[0])
            if not self._navigate_to_contact(ctx, pick.get("contact_id")):
                return {"error": "couldn't open a search match"}
            res = {"ok": True, "contact_id": pick.get("contact_id"),
                   "name": pick.get("name")}
        target = self._active_page(ctx)
        cid = res.get("contact_id")
        clicked = []
        nav_related = None
        try:
            target.wait_for_timeout(1500)  # let the record settle
            if click_seq:
                for label in click_seq:
                    c = target.evaluate(self._OC_CLICK_TAB_JS, [label])
                    clicked.append(c)
                    if c:
                        target.wait_for_timeout(1600)
            elif cid:
                # Deep-link straight to the Course Mentor Student Assignments
                # related-list FULL view (the complete ACI-by-course list, with
                # a checkmark on the active mentor). URL pattern from a real
                # record: /lightning/r/Contact/<id>/related/
                # CourseMentorStudentAssignments__r/view
                from urllib.parse import urlsplit
                p = urlsplit(target.url or CASELOAD_URL
                             or "https://srm.lightning.force.com")
                url = (f"{p.scheme}://{p.netloc}/lightning/r/Contact/{cid}/"
                       "related/CourseMentorStudentAssignments__r/view")
                target.goto(url, wait_until="domcontentloaded", timeout=30_000)
                target.wait_for_timeout(3000)
                nav_related = url
        except Exception:
            pass
        # The Course Mentor Student Assignments is a lazy related-list PANEL
        # (shows 5, "View All"): scroll it into view so its rows render before
        # we dump (scraping before that is why it kept coming back empty).
        revealed = None
        try:
            for needle in ("Course Mentor Student Assignments", "Course Mentor"):
                r = target.evaluate(self._OC_REVEAL_JS, needle)
                if r:
                    revealed = r
                    target.wait_for_timeout(2500)  # let the list lazy-render
                    break
        except Exception:
            pass
        try:
            data = target.evaluate(self._OC_PROBE_JS)
            tables = target.evaluate(self._OC_TABLES_JS)
        except Exception as e:
            return {"error": f"probe scrape failed: {e}",
                    "contact_id": res.get("contact_id"),
                    "name": res.get("name")}
        return {"ok": True, "contact_id": res.get("contact_id"),
                "name": res.get("name"),
                "url": (target.url if target else ""),
                "clicked_tab": ", ".join(str(c) for c in clicked) or None,
                "revealed": revealed,
                "nav_related": nav_related,
                "snippets": data.get("snippets", []),
                "related": data.get("related", []),
                "tables": tables}

    # Off-caseload Contact profile: our field key -> label spelling(s) as they
    # appear on the Contact "Student Profile" page. The page renders each field
    # as a label snippet immediately followed by its value snippet (DOM order),
    # so we find the label and take the next non-noise snippet. First spelling
    # that resolves wins. Calibrated from real Liberty Frost dumps.
    _OC_FIELD_SPELLINGS = [
        ("student_id", ["Student ID"]),
        ("mobile", ["Mobile"]),
        ("wgu_email", ["WGU Email", "Email"]),
        ("other_email", ["Other Email"]),
        ("timezone", ["Timezone"]),
        ("mentor", ["Mentor"]),            # PM (program mentor) NAME
        ("mentor_email", ["Mentor Email"]),
        ("status", ["Status"]),
        ("college", ["College"]),
        ("degree_program", ["WGU Degree Program"]),
        ("program", ["Program Name"]),
        ("term_number", ["Term Number"]),
        ("term_end", ["Term End Date"]),
        ("momentum", ["Momentum"]),
        ("term_sap", ["Term CUs and SAP"]),
        ("cum_sap", ["Cumulative SAP %"]),
        ("last_activity", ["Last Academic Activity Date"]),
    ]

    # Other labels seen on the page — used to STOP a value scan (a following
    # label means the field we're on had no value), so we never grab a distant
    # unrelated snippet.
    _OC_STOP_LABELS = frozenset({
        "student id", "mobile", "wgu email", "email", "other email", "timezone",
        "mentor", "mentor email", "status", "college", "wgu degree program",
        "program name", "program summary", "term number", "term end date",
        "momentum", "term cus and sap", "cumulative sap %",
        "last academic activity date", "previous term completed cus",
        "first name", "last name", "pidm", "gender", "birthday", "phone",
        "home phone", "international phone", "other phone", "business phone",
        "mobile phone", "do not call", "email opt out", "ferpa access",
        "ferpa indicator", "ferpa designee", "campus code", "affiliation",
        "affiliation code", "full or part time", "mailing address",
        "view contact hierarchy", "student preferred name",
        "sms opt-in academic", "sms opt-in academic", "is nse student",
        "call recording opt-out", "currently experiencing evb",
    })

    @staticmethod
    def _parse_oc_fields(snippets) -> dict:
        """Parse the Contact page's DOM-order text snippets into a field dict
        (label -> next non-noise value). See _OC_FIELD_SPELLINGS."""
        snips = [str(s or "").strip() for s in (snippets or [])]
        stop = BrowserWorker._OC_STOP_LABELS

        def noise(v: str) -> bool:
            return (not v or v == "Preview" or v.endswith("Help Info")
                    or v.startswith("Edit:"))

        out: dict = {}
        for key, labels in BrowserWorker._OC_FIELD_SPELLINGS:
            val = ""
            for label in labels:
                ll = label.lower()
                for i, s in enumerate(snips):
                    if s.lower() != ll:
                        continue
                    for j in range(i + 1, min(i + 5, len(snips))):
                        v = snips[j]
                        if v.lower() in stop:
                            break          # hit the next field's label
                        if noise(v):
                            continue       # help-text / Preview / edit chrome
                        val = v
                        break
                    if val:
                        break
                if val:
                    break
            out[key] = val
        return out

    @staticmethod
    def _parse_oc_aci(tables) -> list:
        """Parse the CourseMentorStudentAssignments related-list rows into ACI
        records: {course, mentor, active}. Row shape (cells joined by ' | '):
        '… | True IsActive | D502 | Tawnya Lee | <date> | <cos> | …'. The
        active row (IsActive=True) is the current assigned course instructor."""
        import re as _re
        out: list = []
        seen = set()
        for t in (tables or []):
            for rowstr in (t.get("rows") or []):
                cells = [c.strip() for c in str(rowstr).split("|")]
                idx = off = None
                active = False
                for k, c in enumerate(cells):
                    if _re.match(r"(True|False)\s+IsActive$", c):
                        idx, off, active = k, 1, c.startswith("True")
                        break
                    if c in ("True", "False") and k + 1 < len(cells) \
                            and cells[k + 1] == "IsActive":
                        idx, off, active = k, 2, (c == "True")
                        break
                if idx is None:
                    continue
                course = cells[idx + off] if idx + off < len(cells) else ""
                mentor = cells[idx + off + 1] if idx + off + 1 < len(cells) else ""
                if not course or not mentor:
                    continue
                key = (course, mentor, active)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"course": course, "mentor": mentor,
                            "active": active})
        return out

    def _read_oc_aci_stable(self, target) -> list:
        """Reveal the CourseMentorStudentAssignments related list and read its
        rows, polling until the parsed ACI records are non-empty AND repeat on
        the next read (or a timeout is hit). Lightning lazy-renders the rows, so
        a single read can catch a half-rendered (or, right after a record
        switch, the PREVIOUS student's) grid — requiring a stable non-empty read
        guards against attributing a stale ACI. Returns [] if the student
        genuinely has no assignment (never a carry-over)."""
        prev = None
        last: list = []
        for _ in range(12):                    # ~ up to 12 × 700ms
            for needle in ("Course Mentor Student Assignments", "Course Mentor"):
                try:
                    if target.evaluate(self._OC_REVEAL_JS, needle):
                        break
                except Exception:
                    pass
            try:
                tables = target.evaluate(self._OC_TABLES_JS)
            except Exception:
                tables = []
            aci = self._parse_oc_aci(tables)
            if aci:
                last = aci
            sig = tuple(sorted((a["course"], a["mentor"], a["active"])
                               for a in aci))
            if aci and sig == prev:             # same non-empty read twice
                return aci
            prev = sig
            target.wait_for_timeout(700)
        return last

    def _read_oc_fields_stable(self, target) -> dict:
        """Poll the Contact 'Student Profile' fields until they actually paint.
        The note panel can be ready before the details render, so an early read
        is empty — accept the first read that yields a real identifying value,
        else return the best (most-populated) partial read."""
        best, best_n = {}, -1
        for _ in range(12):                       # ~ up to 12 × 500ms
            try:
                data = target.evaluate(self._OC_PROBE_JS)
                fields = self._parse_oc_fields(data.get("snippets", []))
            except Exception:
                fields = {}
            if any(fields.get(k) for k in
                   ("student_id", "wgu_email", "mentor", "mobile")):
                return fields
            n = sum(1 for v in fields.values() if v)
            if n > best_n:
                best, best_n = fields, n
            target.wait_for_timeout(500)
        return best

    def _scrape_offcaseload_profile(self, ctx, contact_id: str,
                                    name: str = "") -> dict:
        """Scrape an off-caseload student's mini-profile: Contact-page fields
        (student + PM) then the active ACI(s) from the CourseMentorStudent-
        Assignments related list. Assumes the record is already open on /view.
        Leaves the browser back on /view (note panel ready) so a quick note can
        follow. Returns the field dict with an added 'aci' list (possibly []).
        """
        target = self._active_page(ctx)
        if target is None:
            return {"aci": []}
        # The note panel can be ready a beat BEFORE the 'Student Profile'
        # details paint (separate Lightning components), so a single early read
        # comes back empty — poll until the fields actually appear.
        fields = self._read_oc_fields_stable(target)
        aci: list = []
        try:
            from urllib.parse import urlsplit
            p = urlsplit(target.url or CASELOAD_URL
                         or "https://srm.lightning.force.com")
            url = (f"{p.scheme}://{p.netloc}/lightning/r/Contact/{contact_id}/"
                   "related/CourseMentorStudentAssignments__r/view")
            self.on_status("  reading the assigned course instructor (ACI)…")
            target.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # Lightning is a client-side SPA: navigating between records keeps
            # the PREVIOUS record's related-list rows in the DOM until the new
            # ones render, so a raced scrape can read the prior student's ACI
            # (or an empty list on a cold open). Force a hard reload so the only
            # rows that can render belong to THIS contact, then wait for a stable
            # non-empty read before trusting it.
            try:
                target.reload(wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass
            target.wait_for_timeout(1500)
            aci = self._read_oc_aci_stable(target)
        except Exception:
            aci = []
        # Return to the Contact view so the note panel is ready for a quick note.
        try:
            self._navigate_to_contact(ctx, contact_id)
        except Exception:
            pass
        fields["aci"] = aci
        return fields

    def _handle_find(self, ctx, query: str, new_tab: bool = False,
                     raise_after: Optional[bool] = None) -> None:
        # `new_tab` is a Salesforce CONSOLE subtab, NOT a browser tab.
        # Clicking a student from the Caseload list already opens it as a
        # new console subtab; the only difference between reuse and
        # new-tab is whether we first close the open record (Shift+X) —
        # see the on_caseload block below. We always drive the SAME
        # browser page (never ctx.new_page(), which would spawn a second
        # Edge tab with its own Caseload).
        target = self._active_page(ctx)
        if target is None:
            self.on_status("No browser pages open.")
            return
        # NOTE: we deliberately do NOT raise the browser here. Raising /
        # bring_to_front() activates the tab and makes Lightning
        # re-render, and clicking a row in that same instant raced the
        # re-render — the click landed before the list was ready and the
        # record didn't switch. Instead we navigate first (below, on the
        # settled page) and raise the window only once navigation
        # succeeds — see _bring_browser_forward() calls.
        self.on_status(f"Searching Caseload for {query!r}...")

        # Lightning routes record navigation only from the ACTIVE view.
        # When a student record is already open the Caseload rows remain
        # in the cached DOM (so a search still "finds" them) but clicking
        # them no longer navigates — the URL stays on the open record.
        # So re-activate the list first whenever we aren't already on it.
        try:
            on_caseload = "Caseload_App_Page" in (target.url or "")
        except Exception:
            on_caseload = False
        reloaded = False
        if not on_caseload:
            if not CASELOAD_URL:
                self.on_status(
                    f"No match for {query!r}; a record is open and "
                    "CASELOAD_URL isn't set, so the Caseload list can't "
                    "be reloaded. Open it manually and try again."
                )
                return
            # Fast path (reuse/double-click only): the open record is a
            # console workspace subtab sitting over a STILL-LIVE Caseload
            # list. Closing it (Shift+X) re-activates that list instantly
            # — far cheaper than a full reload. Falls back to a reload if
            # we don't land back on Caseload (e.g. another student tab was
            # underneath). New-tab opens deliberately keep their existing
            # tabs, so they always reload instead of closing anything.
            closed = (not new_tab) and self._close_record_subtab(target)
            if not closed:
                self.on_status("Reloading Caseload list before search...")
                self._ensure_caseload_list(target)
            reloaded = True

        # First pass: search the (now active) Caseload list. New-tab
        # opens stay in the background (no raise) by convention; an
        # explicit raise_after (e.g. background nav for a row-fire)
        # overrides that default.
        if raise_after is None:
            raise_after = not new_tab
        try:
            if self._try_match_or_navigate(target, query, raise_after):
                return
        except Exception as e:
            self.on_status(f"Search failed: {e}")
            return

        # Miss. If the list table is already in the DOM, the student just
        # isn't in the ~10 rendered rows — reloading won't help (and costs
        # a full goto + render wait), so fall straight through to the row
        # filter, which searches ALL rows server-side. Only reload when the
        # list genuinely isn't present (e.g. URL says Caseload but the
        # table hasn't rendered yet).
        if not reloaded and CASELOAD_URL and not self._caseload_table_present(target):
            self.on_status(
                "Caseload list not in DOM — navigating there to retry...")
            if self._ensure_caseload_list(target):
                try:
                    if self._try_match_or_navigate(target, query, raise_after):
                        return
                except Exception as e:
                    self.on_status(f"Retry search failed: {e}")
                    return

        # Step 3: the Caseload table only renders ~10 rows at a time.
        # If the student isn't in that window, type the query into the
        # 'Search All Rows...' filter to narrow the table to a match.
        self.on_status("Not in visible rows; typing into Caseload's row filter...")
        try:
            filter_input = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if filter_input.count() == 0:
                self.on_status("Couldn't find Caseload's row filter input.")
                return
            # focus() instead of click() — when the user's Caseload
            # view has many columns the search input scrolls off the
            # viewport horizontally; click() refuses (requires viewport
            # visibility), focus() doesn't.
            filter_input.focus()
            filter_input.fill("")
            filter_input.fill(query)
            filter_input.press("Enter")
            # Lightning debounces the filter; give it a moment to update.
            _wait_grid_settled(target, 1500)
        except Exception as e:
            self.on_status(f"Filter step failed: {e}")
            return

        try:
            if not self._try_match_or_navigate(target, query, raise_after):
                # Recovery: the row filter found nothing — most often because
                # the Student ID column isn't in the caseload list view (the
                # filter can't match a hidden column). If we know this student's
                # Contact id from the live grid, deep-link straight to the record
                # instead of giving up (column-independent).
                q = (query or "").strip()
                cid = ""
                if re.fullmatch(r"\d{5,12}", q):
                    try:
                        cid = (self.grid_student_contact_map().get(q)
                               or "").strip()
                    except Exception:
                        cid = ""
                if cid.startswith("003") and self._navigate_to_contact(ctx, cid):
                    self.on_status(
                        f"  Row filter missed {q!r} (Student ID column not in "
                        "the caseload view?) — deep-linked by Contact id.")
                    if raise_after:
                        try:
                            self._bring_browser_forward(target)
                        except Exception:
                            pass
                    return
                self.on_status(
                    f"No match for {query!r} after filtering. "
                    "Try Salesforce global search for students outside your caseload."
                )
        except Exception as e:
            self.on_status(f"Search after filter failed: {e}")

    def _list_matches(self, ctx, query: str) -> list[str]:
        """Multi-pass search: returns matching names without clicking.
        Stores rows on self._last_matches so CLICK_MATCH can resolve.
        Order:
          1. exact on current DOM
          2. reload Caseload, exact
          3. row-filter (Salesforce 'Search All Rows…'), exact with query
          4. row-filter with adjacent-transposition variants ('jsoh' →
             try 'sjoh', 'josh', 'jsho'). Catches the most common
             single-typo case using Salesforce's own search, which can
             see all rows (fuzzy is stuck with the ~10 visible ones).
          5. clear row-filter, fuzzy as a last resort
        """
        target = self._active_page(ctx)
        if target is None:
            self.on_status("No browser pages open.")
            self._last_matches = []
            return []
        self.on_status(f"Find: searching Caseload for {query!r}...")

        matches = gather_caseload_matches(target, query, on_status=self.on_status)
        if matches:
            self._last_matches = matches
            return [m[2] for m in matches]

        filter_input = None  # set in step 2 so step 4 can clear it
        if CASELOAD_URL:
            self.on_status("Caseload not in DOM — reloading and retrying...")
            try:
                target.goto(CASELOAD_URL, wait_until="domcontentloaded")
            except Exception as e:
                self.on_status(f"  [debug] goto note: {e}")
            try:
                list_table = (
                    target.locator("table")
                    .filter(has=target.locator('th:has-text("Course Code")'))
                    .filter(has=target.locator('th:has-text("Name")'))
                )
                list_table.first.wait_for(state="visible", timeout=20_000)
            except Exception as e:
                self.on_status(f"Caseload table didn't load: {e}")
                self._last_matches = []
                return []
            matches = gather_caseload_matches(target, query, on_status=self.on_status)
            if matches:
                self._last_matches = matches
                return [m[2] for m in matches]

            self.on_status("Not in visible rows; using Caseload's row filter...")
            try:
                filter_input = target.locator(
                    'input[placeholder="Search All Rows..."]'
                ).filter(visible=True).first
                if filter_input.count() == 0:
                    filter_input = None
            except Exception as e:
                self.on_status(f"Filter lookup failed: {e}")
                filter_input = None

            def _try_filter(text: str) -> list[tuple]:
                """Fill the row filter and gather exact matches against
                `text`. Returns the raw match tuples (empty on any
                failure or zero results)."""
                if filter_input is None:
                    return []
                try:
                    filter_input.focus()
                    filter_input.fill("")
                    filter_input.fill(text)
                    filter_input.press("Enter")
                    _wait_grid_settled(target, 1500)
                except Exception as e:
                    self.on_status(f"  [debug] filter {text!r}: {e}")
                    return []
                return gather_caseload_matches(
                    target, text, on_status=self.on_status,
                )

            # Step 3: original query.
            matches = _try_filter(query)
            if matches:
                self._last_matches = matches
                return [m[2] for m in matches]

            # Step 4: adjacent-transposition typo variants.
            if len(query) >= 3 and filter_input is not None:
                for variant in typo_variants(query):
                    self.on_status(f"Trying typo correction {variant!r}...")
                    matches = _try_filter(variant)
                    if matches:
                        self.on_status(
                            f"Found via typo-correction {variant!r} "
                            f"(you typed {query!r})."
                        )
                        self._last_matches = matches
                        return [m[2] for m in matches]

        # Step 5: clear the row filter (if we set it) so fuzzy sees the
        # full caseload again, then fuzzy-match.
        if filter_input is not None:
            try:
                self.on_status("Clearing row filter for fuzzy search...")
                filter_input.focus()
                filter_input.fill("")
                filter_input.press("Enter")
                _wait_grid_settled(target, 1500)
            except Exception as e:
                self.on_status(f"  [debug] clear filter: {e}")

        self.on_status(f"No exact match for {query!r}; trying fuzzy...")
        fuzzy = gather_fuzzy_caseload_matches(
            target, query, on_status=self.on_status,
        )
        self._last_matches = fuzzy
        return [m[2] for m in fuzzy]

    def _click_match_by_name(self, ctx, name: str) -> bool:
        target = self._active_page(ctx)
        if target is None:
            self.on_status("No browser pages open.")
            return False
        for m in self._last_matches:
            if m[2] == name:
                _, row, mname, name_idx = m
                if click_caseload_row(row, mname, name_idx, on_status=self.on_status):
                    self.on_status(f"Navigated to {mname!r}.")
                    return True
                self.on_status(f"Click on {mname!r} failed.")
                return False
        self.on_status(f"Click failed: {name!r} not in last results.")
        return False

    def _click_match_by_filter(
        self, ctx, query: str, expected_name: str = "",
    ) -> tuple[bool, dict]:
        """Skip the slow full-table DOM scan: type `query` into
        Salesforce's row filter, wait, then click the (one) result.
        For batches with known-unique Student IDs this is ~10x faster
        than _list_matches + _click_match_by_name.

        Returns (success, row_info). When the click lands cleanly,
        row_info is `{"pm_email": …, "student_email": …}` extracted
        from the row's `mailto:` action link BEFORE the click (so
        we can populate the email step in batch mode without the
        user having to add email columns to their Caseload view in
        Salesforce). After the click we additionally try the contact
        card via `_extract_wgu_email` as a second source for the
        student address. Any field we couldn't read comes back as
        an empty string."""
        row_info: dict = {"pm_email": "", "student_email": ""}
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return False, row_info
        self.on_status(f"Fast-find: filtering Caseload by {query!r}...")
        try:
            filter_input = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if filter_input.count() == 0:
                self.on_status("No row filter input; can't fast-find.")
                return False, row_info
            # focus() instead of click() — when the user's Caseload
            # view has many columns the search input scrolls off the
            # viewport horizontally; click() refuses (requires viewport
            # visibility), focus() doesn't.
            filter_input.focus()
            filter_input.fill("")
            filter_input.fill(query)
            filter_input.press("Enter")
            _wait_grid_settled(target, 800)
        except Exception as e:
            self.on_status(f"Fast-find filter failed for {query!r}: {e}")
            return False, row_info

        # Resolve Name column index on the filtered table.
        headers_raw = table.locator("th").all_text_contents()
        name_idx = next(
            (j for j, h in enumerate(headers_raw) if h.strip() == "Name"),
            None,
        )
        if name_idx is None:
            name_idx = next(
                (
                    j for j, h in enumerate(headers_raw)
                    if h.strip().startswith("Name")
                ),
                None,
            )
        if name_idx is None:
            self.on_status("Fast-find: no Name column found.")
            return False, row_info

        # Collect matching rows from the filtered table.
        rows_loc = table.locator("tr")
        n_rows = rows_loc.count()
        candidates: list[tuple] = []
        for r in range(1, n_rows):
            row = rows_loc.nth(r)
            try:
                cells = row.locator("td").all_text_contents()
            except Exception:
                continue
            if not cells or name_idx >= len(cells):
                continue
            cname = cells[name_idx].strip()
            if cname:
                candidates.append((row, cname, name_idx))

        if not candidates:
            self.on_status(f"Fast-find: {query!r} returned 0 rows.")
            return False, row_info

        # Disambiguate via expected_name (the row we matched in main).
        if expected_name:
            chosen = next(
                (c for c in candidates if c[1] == expected_name), None,
            )
            if chosen is None:
                self.on_status(
                    f"Fast-find {query!r}: {len(candidates)} rows but "
                    f"none named {expected_name!r}; skipping."
                )
                return False, row_info
        elif len(candidates) > 1:
            names = ", ".join(c[1] for c in candidates)
            self.on_status(
                f"Fast-find {query!r}: {len(candidates)} ambiguous rows "
                f"({names}); skipping (no expected_name to disambiguate)."
            )
            return False, row_info
        else:
            chosen = candidates[0]

        row, cname, name_idx = chosen

        # BEFORE clicking: scrape the row's "Email Student" mailto
        # link. Historically Salesforce puts the PM as primary and
        # the student as CC in that link, so we can capture both for
        # free here — much more reliable than re-scraping after we've
        # navigated away from Caseload. Best-effort: failure here
        # doesn't block the click; the batch can still proceed with
        # whatever the CSV row provided.
        try:
            mailtos = row.locator('a[href^="mailto:"]')
            if mailtos.count() > 0:
                href = mailtos.first.get_attribute("href") or ""
                primary, cc = _parse_mailto(href)
                if "@" in primary:
                    row_info["pm_email"] = primary
                if "@" in cc:
                    row_info["student_email"] = cc
        except Exception:
            pass

        # Quick diagnostic so the next layer (the batch loop) can
        # see whether the mailto step succeeded vs. silently went
        # empty. Only emit once per session — chatty otherwise.
        if not self._mailto_diag_logged:
            self._mailto_diag_logged = True
            if row_info["pm_email"] or row_info["student_email"]:
                self.on_status(
                    f"Row mailto: pm_email={row_info['pm_email']!r}, "
                    f"student_email={row_info['student_email']!r}"
                )
            else:
                self.on_status(
                    "Row had no mailto: link — Caseload view may be "
                    "missing the 'Email Student' action column. Will "
                    "try the contact card after navigation."
                )

        if not click_caseload_row(row, cname, name_idx, on_status=self.on_status):
            self.on_status(f"Fast-find: click on {cname!r} failed.")
            return False, row_info
        self.on_status(f"Fast-find navigated to {cname!r}.")

        # Wait for the destination page to settle BEFORE scraping the
        # contact card. (The dispatch wrapper used to do this 2s
        # wait, but that ran AFTER this function returned — so the
        # earlier post-click scrape was racing an unloaded page and
        # always coming back empty.)
        post_click_target = self._active_page(ctx)
        if post_click_target is not None:
            try:
                # Ready as soon as the record resolves; small extra settle
                # so the contact card (email field) is painted before scrape.
                _wait_record_ready(post_click_target, 2000)
                post_click_target.wait_for_timeout(250)
            except Exception:
                pass

        # AFTER click + settle: try the contact card on the
        # student's record page. Sweeps several common Salesforce
        # email-field labels + a generic mailto fallback so this
        # works regardless of whether the org calls the field
        # "WGU Email", "Personal Email", "Student Email", etc.
        if post_click_target is not None and not row_info["student_email"]:
            try:
                found = scrape_student_email_from_page(
                    post_click_target,
                    pm_email=row_info.get("pm_email", ""),
                )
                if found:
                    row_info["student_email"] = found
                    if not self._contact_card_diag_logged:
                        self._contact_card_diag_logged = True
                        self.on_status(
                            f"Contact-card scrape found student email: {found}"
                        )
                elif not self._contact_card_diag_logged:
                    self._contact_card_diag_logged = True
                    self.on_status(
                        "Contact-card scrape found no student email "
                        "on the record page. The contact's email field "
                        "may use an unusual label or be hidden — paste "
                        "this log to the launcher dev along with the "
                        "label text shown on the student's record."
                    )
            except Exception:
                pass

        return True, row_info

    def _read_student_context(self, ctx, name_hint: str = "") -> Optional[dict]:
        """Build the variable dict used to render emails and notes.
        `name_hint` lets the caller supply the name we just navigated
        to (e.g. from find-first) when the note panel hasn't been
        opened yet so get_active_student_name() would return ''."""
        target = self._active_page(ctx)
        if target is None:
            return None
        name = name_hint or (get_active_student_name(target) or "")
        info = lookup_caseload_student(target, name) if name else {}
        first, _, last = name.partition(" ")
        return {
            "full_name": _capitalize_name(name),
            "first_name": _capitalize_name(first),
            # Preferred name from the caseload row, else the first name.
            "preferred_name": _capitalize_name(
                info.get("preferred_name", "") or first),
            "last_name": _capitalize_name(last),
            "student_email": info.get("student_email", ""),
            "student_id": info.get("student_id", ""),
            "course_code": info.get("course_code", ""),
            "pm_name": _capitalize_name(info.get("pm_name", "")),
            "pm_email": info.get("pm_email", ""),
        }

    # ----- Network capture (for REST-API discovery) -----

    def start_request_capture(self) -> None:
        """Begin recording Salesforce-bound write requests. Call once
        per discovery session; subsequent fires by the user populate
        `_capture_log`."""
        self._capture_active = True
        self._capture_log = []

    def stop_request_capture(self) -> list[dict]:
        """Stop recording and return the accumulated log. Safe to call
        even if capture wasn't running."""
        self._capture_active = False
        return list(self._capture_log)

    def _on_request(self, request) -> None:
        """Context-level request listener. ALWAYS harvests the live aura.token +
        aura.context from Aura POSTs (so we can replay actions like the note
        save); ADDITIONALLY, in capture mode, records Salesforce write requests
        for probing."""
        try:
            url = request.url or ""
            method = request.method or ""
        except Exception:
            return
        # Always-on: harvest the freshest Aura credentials (token + context).
        # They ride in every /aura POST body and are reused across requests, so
        # a recent one is valid for replaying saveNoteCmpValues via fetch().
        if method.upper() == "POST" and "/aura" in url:
            self._harvest_aura_creds(request)
        # Always-on: harvest the Mongoose API Bearer token (rides on every
        # sms-api.mongooseresearch.com request) — for replaying the text-send API.
        if "sms-api.mongooseresearch.com" in url:
            self._harvest_mongoose_token(request)
        if not self._capture_active:
            return
        if method.upper() not in ("POST", "PATCH", "PUT"):
            return
        if not any(d in url for d in (
            "salesforce.com", "force.com", "lightning.com",
        )):
            return
        # Skip token/auth/session refresh chatter.
        if any(skip in url for skip in (
            "/auth/", "/oauth", "/token", "/session",
            "/aura?aura.token", "/visualforce/session",
        )):
            return
        try:
            self._capture_log.append({
                "kind": "request",
                "url": url,
                "method": method,
                "headers": dict(request.headers),
                "post_data": request.post_data,
            })
        except Exception:
            pass

    def _on_response(self, response) -> None:
        """Context-level RESPONSE listener. ALWAYS watches for the caseload grid
        data response (the Phase-1 API fast path); ADDITIONALLY, in capture
        mode, records Salesforce data-response bodies for probing."""
        try:
            url = response.url or ""
        except Exception:
            return
        # Always-on: harvest the caseload grid (per-task pass/fail + ids) the
        # page already fetches, so the scan can skip the scroll-scrape.
        if "getCaseLoadMainGridData" in url:
            self._capture_grid_data(response)
        # While reading the EA dashboard, grab its EmployeeEvent__c JSON feed.
        if self._ea_capture_armed and "/aura" in url:
            self._capture_ea_data(response)
        if not self._capture_active:
            return
        if not any(d in url for d in (
                "salesforce.com", "force.com", "lightning.com")):
            return
        # Data endpoints only (Aura's generic action endpoint carries the
        # Lightning datatable's rows); skip auth/session + static assets.
        low = url.lower()
        if not ("/aura" in low or "apexremote" in low or "apex" in low
                or "/services/data" in low or "/api/" in low):
            return
        if any(skip in url for skip in (
                "/auth/", "/oauth", "/token", "/aura?aura.token",
                "/visualforce/session")):
            return
        try:
            ctype = (response.headers or {}).get("content-type", "")
        except Exception:
            ctype = ""
        body = ""
        try:
            body = response.text()
        except Exception:
            body = ""
        try:
            self._capture_log.append({
                "kind": "response",
                "url": url,
                "status": getattr(response, "status", None),
                "content_type": ctype,
                "body_len": len(body or ""),
                "body_preview": (body or "")[:60000],
            })
        except Exception:
            pass

    def _capture_ea_data(self, response) -> None:
        """When armed, find the EA dashboard's data feed in an /aura response: a
        list of EmployeeEvent__c records (each with a Contact__r object +
        Intervention__c + CourseCode__c). Keeps the longest such list seen.
        Cheap-skips non-EA responses via a substring check before parsing."""
        try:
            body = response.text()
        except Exception:
            return
        if "Intervention__c" not in body:
            return
        import json
        try:
            i = body.find("{")
            env = json.loads(body[i:]) if i >= 0 else {}
        except Exception:
            return
        for a in (env.get("actions") or []):
            rv = a.get("returnValue")
            cands = []
            if isinstance(rv, list):
                cands.append(rv)
            elif isinstance(rv, dict):
                cands += [v for v in rv.values() if isinstance(v, list)]
            for c in cands:
                if (c and isinstance(c[0], dict)
                        and isinstance(c[0].get("Contact__r"), dict)
                        and "Intervention__c" in c[0]
                        and "CourseCode__c" in c[0]):
                    # Prefer a feed whose Contact__r actually carries a
                    # StudentID__c (the join key) over a leaner competing action
                    # that merely mentions Intervention__c — otherwise we can
                    # latch the wrong list and map every row to nothing. Rank by
                    # (has-usable-StudentID, length).
                    def _score(lst):
                        con = lst[0].get("Contact__r") or {}
                        return (1 if str(con.get("StudentID__c") or "").strip()
                                else 0, len(lst))
                    if self._ea_data is None or _score(c) > _score(self._ea_data):
                        self._ea_data = c

    def _capture_grid_data(self, response) -> None:
        """Parse + ACCUMULATE the getCaseLoadMainGridData Aura response. The page
        fetches the grid in PAGES (~100 rows each), so we merge each page into a
        running {(StudentID, CourseCode): row} map (newest wins) rather than
        overwriting — otherwise we'd only ever hold the last page. Best-effort:
        a read/parse failure just leaves the prior accumulation in place."""
        if self._suppress_grid_capture:
            return   # archive-view export in progress — don't accumulate its rows
        import json
        try:
            body = response.text()
            env = json.loads(body)
        except Exception:
            return
        rows = None
        for a in (env.get("actions") or []):
            if (a.get("state") == "SUCCESS"
                    and isinstance(a.get("returnValue"), list)):
                rows = a["returnValue"]
                break
        if not rows:
            return
        store = self._grid_data or {"by_key": {}, "ts": 0.0}
        by_key = store["by_key"]
        for row in rows:
            sid = str(row.get("StudentID") or "").strip()
            if not sid:
                continue
            course = str(row.get("CourseCode") or "").strip()
            by_key[(sid, course)] = row
        store["ts"] = time.time()
        self._grid_data = store   # silent — the scan's result line reports it

    def _harvest_aura_creds(self, request) -> None:
        """Pull aura.token + aura.context out of an Aura POST body and keep the
        freshest pair. They're reused across requests, so a recent one is valid
        for replaying saveNoteCmpValues. Best-effort; silent."""
        try:
            pd = request.post_data or ""
        except Exception:
            return
        if "aura.token" not in pd:
            return
        from urllib.parse import parse_qs
        try:
            q = parse_qs(pd)
        except Exception:
            return
        tok = (q.get("aura.token") or [""])[0]
        ctx = (q.get("aura.context") or [""])[0]
        if tok and ctx:
            self._aura_creds = {"token": tok, "context": ctx, "ts": time.time()}

    def _harvest_mongoose_token(self, request) -> None:
        """Keep the freshest Mongoose API Bearer token — it rides in the
        Authorization header of every sms-api.mongooseresearch.com request and is
        reused (until ~16h expiry) to replay the text-send API. Best-effort."""
        try:
            auth = (request.headers or {}).get("authorization", "")
        except Exception:
            return
        if auth[:7].lower() == "bearer " and len(auth) > 40:
            self._mongoose_token = {"token": auth[7:], "ts": time.time()}

    def _ensure_mongoose_token(self, page, timeout_ms: int = 6000) -> bool:
        """Make sure a Mongoose API Bearer token has been harvested so the API
        text path can run. The dashboard fires an authenticated sms-api call on
        load (grabbed by _harvest_mongoose_token), so when we have no token yet
        (e.g. the first send of a session) reload the tab and poll briefly.
        Returns True once a token exists."""
        if (self._mongoose_token or {}).get("token"):
            return True
        try:
            page.reload(wait_until="domcontentloaded", timeout=20_000)
        except Exception:
            pass
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if (self._mongoose_token or {}).get("token"):
                return True
            try:
                page.wait_for_timeout(300)
            except Exception:
                break
        return bool((self._mongoose_token or {}).get("token"))

    def _probe_mongoose_api(self, ctx) -> dict:
        """Replay-test the harvested Bearer token: in-page fetch (from the
        Mongoose tab, so the API's CORS origin is satisfied) of the read-only
        maps we need for the send — /api/profile, /api/groupaccounts,
        /api/messageTypes. Proves the token is replayable AND grabs the
        course/department -> groupAccountId map. Writes mongoose_api_probe.txt."""
        from src.config import USER_CONFIG_DIR
        page = self._mongoose_page(ctx)
        if page is None:
            return {"error": "Mongoose isn't open — open the Mongoose tab first."}
        tok = (self._mongoose_token or {}).get("token")
        if not tok:
            return {"error": "No Mongoose bearer token harvested yet — the "
                    "Mongoose tab fires one on load; reload it and retry."}
        endpoints = ["/api/profile", "/api/groupaccounts", "/api/messageTypes",
                     "/api/groupaccounts/mine", "/api/sharedinboxes"]
        out = {}
        for ep in endpoints:
            try:
                out[ep] = page.evaluate(
                    """async ([ep, tok]) => {
                        try {
                            const r = await fetch(
                                'https://sms-api.mongooseresearch.com' + ep,
                                {headers: {'Authorization': 'Bearer ' + tok,
                                           'Content-Type': 'application/json'}});
                            const t = await r.text();
                            return {status: r.status, body: t.slice(0, 9000)};
                        } catch (e) { return {status: 'fetch-error',
                                              body: String(e)}; }
                    }""", [ep, tok])
            except Exception as e:
                out[ep] = {"status": "err", "body": str(e)}
        import re as _re
        def _redact(s):
            return _re.sub(
                r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}",
                "<JWT redacted>", str(s or ""))
        lines = ["=== Mongoose API replay probe (harvested Bearer token) ===",
                 "Bearer JWTs REDACTED. If statuses are 200, the token REPLAYS —",
                 "the send API is buildable.", ""]
        ok = False
        for ep in endpoints:
            r = out.get(ep) or {}
            st = r.get("status")
            if st == 200:
                ok = True
            lines += [f"GET {ep}  -> {st}",
                      "    " + _redact(r.get("body", ""))[:5000], ""]
        path = USER_CONFIG_DIR / "mongoose_api_probe.txt"
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            path = None
        return {"path": str(path) if path else None, "replayable": ok,
                "statuses": {ep: (out.get(ep) or {}).get("status")
                             for ep in endpoints}}

    def _mongoose_api(self, page, tok, method, ep, body=None):
        """One authenticated Mongoose API call via in-page fetch on the Mongoose
        tab (so the API's CORS origin is satisfied + the bearer replays). Returns
        {status, body(text)}."""
        return page.evaluate(
            """async ([method, ep, tok, body]) => {
                const opts = {method, headers: {
                    'Authorization': 'Bearer ' + tok,
                    'Content-Type': 'application/json'}};
                if (body !== null) opts.body = JSON.stringify(body);
                try {
                    const r = await fetch(
                        'https://sms-api.mongooseresearch.com' + ep, opts);
                    let t = ''; try { t = await r.text(); } catch (e) {}
                    return {status: r.status, body: t};
                } catch (e) { return {status: 'fetch-error', body: String(e)}; }
            }""", [method, ep, tok, body])

    def _send_text_via_api(self, ctx, payload) -> dict:
        """Send/schedule a text through Mongoose's REST API (replaying the
        harvested Bearer token) instead of driving the compose modal. `payload`:
        {course, text, contact_ids:[SF Contact id…], schedule_date
        ('YYYY-MM-DDTHH:mm', team-local), schedule_name}. Resolves the group
        account + default messageType for the course, maps each SF Contact id →
        Mongoose numeric id (skipping opted-out), then POSTs the send. Returns
        {ok, status, sent, skipped, gid} or {error}. NOTE: group account +
        messageType are the CURRENT Mongoose department's — the tab must be on
        that course's department (cross-department switching is a later add)."""
        import json as _json
        page = self._mongoose_page(ctx)
        if page is None:
            return {"error": "Mongoose isn't open."}
        tok = (self._mongoose_token or {}).get("token")
        if not tok:
            return {"error": "No Mongoose token harvested yet (reload the tab)."}
        course = (payload.get("course") or "").strip()
        text = payload.get("text") or ""
        contact_ids = [str(c).strip() for c in (payload.get("contact_ids") or [])
                       if str(c).strip()]
        if not text or not contact_ids:
            return {"error": "empty text or no recipients."}
        def _accounts():
            try:
                return _json.loads(
                    (self._mongoose_api(page, tok, "GET", "/api/groupaccounts")
                     ).get("body") or "[]")
            except Exception:
                return []

        def _find_gid(accts):
            return next((a.get("id") for a in accts
                         if str(a.get("apiCode", "")).strip().upper()
                         == course.upper()), None)

        accounts = _accounts()
        gid = _find_gid(accounts)
        if gid is None:
            # Cross-department: /api/groupaccounts + /api/messageTypes are scoped
            # to the CURRENT department, so switch to the course's department
            # first (from /api/profile's code→departmentId map), then re-fetch.
            try:
                depts = (_json.loads(
                    (self._mongoose_api(page, tok, "GET", "/api/profile")
                     ).get("body") or "{}").get("departments")) or []
            except Exception:
                depts = []
            dept_id = next((d.get("id") for d in depts
                            if str(d.get("code", "")).strip().upper()
                            == course.upper()), None)
            if dept_id is not None:
                self.on_status(
                    f"  [api] switching Mongoose to the {course} department "
                    f"(id {dept_id})…")
                cd = self._mongoose_api(page, tok, "POST",
                                        "/api/profile/changeDepartment",
                                        {"departmentId": dept_id})
                # changeDepartment re-issues a DEPARTMENT-SCOPED token; the search
                # + groupaccounts are dept-scoped, so we MUST switch to it (else
                # they keep hitting the old department → 0 matches).
                m = re.search(
                    r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}",
                    cd.get("body") or "")
                if m:
                    tok = m.group(0)
                    self._mongoose_token = {"token": tok, "ts": time.time()}
                    self.on_status("  [api] switch returned a re-scoped token.")
                else:
                    self.on_status(
                        f"  [api] changeDepartment → {cd.get('status')}; NO token "
                        f"in the response (body {len(cd.get('body') or '')} "
                        "chars). If the send fails, the token's elsewhere.")
                accounts = _accounts()
                gid = _find_gid(accounts)
        if gid is None:
            return {"error": f"no Mongoose inbox matching course {course!r} "
                    f"(apiCodes {[a.get('apiCode') for a in accounts]})."}
        try:
            mts = _json.loads(
                (self._mongoose_api(page, tok, "GET", "/api/messageTypes")
                 ).get("body") or "[]")
        except Exception:
            mts = []
        msg_type = next((m.get("id") for m in mts if m.get("isDefault")),
                        (mts[0].get("id") if mts else None))
        def _digits(s):
            return "".join(ch for ch in str(s or "") if ch.isdigit())

        recips, skipped = [], []
        for cid in contact_ids:
            try:
                arr = _json.loads(
                    (self._mongoose_api(page, tok, "POST", "/api/students/search",
                                        {"includeOptOut": True, "searchText": cid})
                     ).get("body") or "[]")
            except Exception:
                arr = []
            cu, cd = cid.upper(), _digits(cid)
            m = next((c for c in arr
                      if str(c.get("campusStudentId", "")).strip().upper() == cu),
                     None)
            if m is None and len(cd) >= 10:   # term was a mobile, not a Contact id
                m = next((c for c in arr
                          if _digits(c.get("mobileNumber")).endswith(cd[-10:])
                          or _digits(c.get("formattedMobile")).endswith(cd[-10:])),
                         None)
            if m and m.get("id") and not m.get("optedOut"):
                recips.append(m["id"])
            else:
                skipped.append(cid)
        if not recips:
            # Token/inbox/search all worked — these students just aren't opted-in
            # Mongoose contacts. Clean skip (0 sent), NOT an error, so the caller
            # doesn't pointlessly fall back to the modal (which also can't add
            # them). This is the authoritative optedIn gate.
            return {"ok": True, "status": None, "sent": 0, "gid": gid,
                    "skipped": skipped, "resp": "",
                    "note": "no opted-in Mongoose contacts among recipients"}
        body = {"groupAccountId": gid, "mediaUris": [], "restrictToAssignment": False,
                "templateId": None, "campaignId": None,
                "smartMessageWindowInSeconds": 0, "text": text,
                "recipients": recips, "fromCompose": True,
                "messageTypeId": msg_type}
        if payload.get("schedule_date"):
            body["scheduleName"] = payload.get("schedule_name") or "API send"
            body["scheduleDate"] = payload["schedule_date"]
        try:
            r = self._mongoose_api(page, tok, "POST",
                                   f"/api/groupaccounts/{gid}/send", body)
        except Exception as e:
            return {"error": f"send failed: {e}"}
        st = r.get("status")
        return {"ok": st in (200, 201, 204), "status": st, "sent": len(recips),
                "skipped": skipped, "gid": gid, "resp": (r.get("body") or "")[:300]}

    # Academic-activity label -> the saveNoteCmpValues boolean field. Positional
    # zip of the selector labels with the payload's note{} flags (confirmed from
    # a real capture 2026-06-30).
    _ACTIVITY_FIELD_BY_LABEL = {
        "Course/Program Information Discussed": "CourseProgramInfoDiscussed__c",
        "Course/Program Information Requested": "CourseProgramInformationRequested__c",
        "Set Academic Goals": "SetAcademicGoals__c",
        "Student Learning Occurred": "StudentLearningOccurred__c",
        "Personal obstacles/non-academic content covered":
            "NonAcademicContentCovered__c",
    }

    def _save_note_via_api(self, ctx, *, contact_id, note_type, course_code,
                           subject, body_html, activities) -> dict:
        """Replay the saveNoteCmpValues Aura action via fetch() from inside the
        page — files a note WITHOUT driving the form (so no reactive Academic
        Activity gate / cold-start flakiness; activities go in as booleans).
        Returns {"ok": True, "note_id"?} or {"error": ...}. Caller falls back to
        the UI form on error."""
        import json as _json
        creds = self._aura_creds
        if not creds:
            return {"error": "no Aura credentials harvested yet"}
        if not (contact_id or "").startswith("003"):
            return {"error": f"bad contactId {contact_id!r}"}
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        note = {
            "Type__c": "", "Name": "",
            "CourseProgramInfoDiscussed__c": False,
            "CourseProgramInformationRequested__c": False,
            "SetAcademicGoals__c": False,
            "StudentLearningOccurred__c": False,
            "NonAcademicContentCovered__c": False,
        }
        for label in (activities or []):
            field = self._ACTIVITY_FIELD_BY_LABEL.get((label or "").strip())
            if field:
                note[field] = True
        params = {
            "contactId": contact_id, "note": note, "extNote": {},
            "survey": {"Student__c": contact_id},
            "saveNoteEAs": [], "saveNoteAndCloseEAs": [],
            "saveExtNoteEAs": [], "saveExtNoteAndCloseEAs": [],
            "noteType": note_type, "courseCode": course_code or "",
            "subject": (subject or note_type),
            "text": body_html or "", "removedEAJunctionIds": [],
        }
        message = _json.dumps({"actions": [{
            "id": "1;a",
            "descriptor": "apex://NotesController/ACTION$saveNoteCmpValues",
            "callingDescriptor": "markup://c:NoteCmp",
            "params": params}]})
        page_uri = f"/lightning/r/Contact/{contact_id}/view"
        js = """async ([message, ctxs, token, pageUri]) => {
            const body = new URLSearchParams();
            body.set('message', message);
            body.set('aura.context', ctxs);
            body.set('aura.pageURI', pageUri);
            body.set('aura.token', token);
            const resp = await fetch('/aura?other.Notes.saveNoteCmpValues=1', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type':
                          'application/x-www-form-urlencoded;charset=UTF-8'},
                body: body.toString(),
            });
            const txt = await resp.text();
            return {status: resp.status, body: txt.slice(0, 20000)};
        }"""
        try:
            res = target.evaluate(
                js, [message, creds["context"], creds["token"], page_uri])
        except Exception as e:
            return {"error": f"fetch failed: {e}"}
        body = (res or {}).get("body", "") or ""
        # Aura may prefix with while(1); — find the JSON envelope.
        i = body.find("{")
        try:
            env = _json.loads(body[i:]) if i >= 0 else {}
        except Exception:
            env = {}
        actions = env.get("actions") or []
        state = actions[0].get("state") if actions else ""
        # The body is capped (the returnValue echoes the whole saved note,
        # which can exceed the cap and TRUNCATE the JSON → parse fails even
        # though the save SUCCEEDED). Fall back to a truncation-proof scan for
        # the first action's state so a real success is never misread as a
        # failure — that misread would wrongly fall back to the form and FILE
        # THE NOTE TWICE.
        if not state:
            m = re.search(r'"state"\s*:\s*"([A-Z]+)"', body)
            state = m.group(1) if m else ""
        if state == "SUCCESS":
            return {"ok": True}
        # surface the server's error (e.g. expired token, validation) for the log
        err = ""
        try:
            err = (env.get("exceptionEvent") and env.get("exceptionMessage")) \
                or (actions[0].get("error") if actions else "")
        except Exception:
            err = ""
        return {"error": f"state={state!r} http={res.get('status')} "
                f"{str(err)[:200] or body[:200]}"}

    def _grid_rows_to_task_status(self, rows) -> dict:
        """Convert getCaseLoadMainGridData rows → {sid: {tnum: info}} — the SAME
        shape the scroll-scan produces — by reusing _parse_task_cell on each
        TaskNhoverText (title) with a color class synthesized from TaskNStatus.
        Downstream (badges, _apply_task_status_to_rows) can't tell the data came
        from the API vs the scrape."""
        from src.student_lookup import _parse_task_cell, task_info_from_grid
        status_to_cls = {
            "passed": "cellColorGreen",
            "returned": "cellColorRed", "revisions needed": "cellColorRed",
            "not passed": "cellColorRed",
            "task submitted": "cellColorBlue", "submitted": "cellColorBlue",
            "pending": "cellColorBlue", "in progress": "cellColorBlue",
        }
        by_sid: dict = {}
        for row in (rows or []):
            try:
                sid = str(row.get("StudentID") or "").strip()
            except Exception:
                continue
            if not sid:
                continue
            statuses: dict = {}
            for n in range(1, 16):
                title = (row.get(f"Task{n}hoverText") or "").strip()
                if title:
                    st = (row.get(f"Task{n}Status") or "").strip().lower()
                    parsed = _parse_task_cell(status_to_cls.get(st, ""), title)
                    if parsed:
                        tnum, info = parsed
                        statuses[tnum] = info
                        continue
                # No usable tooltip — don't drop the task. Recover its outcome
                # from the authoritative TaskNStatus word and/or the TaskN cell's
                # status glyph (a passed task whose tooltip is missing used to be
                # dropped, stranding the student out of the pass/fail scan and so
                # out of their Success Path).
                info = task_info_from_grid(
                    row.get(f"Task{n}Status", ""), row.get(f"Task{n}", ""))
                if info:
                    statuses[str(n)] = info
            if statuses:
                by_sid[sid] = statuses
        return by_sid

    def _grid_acacourse_id(self, sid: str, course: str = "") -> str:
        """Resolve the StudentAcademicCourse id (a6d…) for a student from the
        accumulated grid — the key the follow-up save Aura action wants
        (theStudentAcaCourseId). Prefer the row matching `course` when given;
        else the first row for the student. '' if unknown."""
        grid = self._grid_data
        if not grid:
            return ""
        want_sid = str(sid or "").strip()
        want_course = str(course or "").strip()
        fallback = ""
        for (s, c), row in (grid.get("by_key") or {}).items():
            if str(s).strip() != want_sid:
                continue
            v = str(row.get("StudentAcademicCourseId") or "").strip()
            if not v:
                continue
            if want_course and str(c).strip() == want_course:
                return v
            fallback = fallback or v
        return fallback

    def _save_followup_via_api(self, ctx, *, acacourse_id, note=None,
                               date=None) -> dict:
        """Replay the MentorForceAuraMethods follow-up save Aura action via
        fetch() from inside the page — persists the Course Followup Note or Date
        directly, bypassing the flaky inline list-cell edit (which sometimes
        committed a null value → the 'doesn't persist' bug). Pass note= OR date=
        (one field per call). Returns {"ok": True} or {"error": ...}; caller
        falls back to the inline edit on error."""
        import json as _json
        creds = self._aura_creds
        if not creds:
            return {"error": "no Aura credentials harvested yet"}
        acac = (acacourse_id or "").strip()
        if not acac.startswith("a6d"):
            return {"error": f"bad StudentAcademicCourseId {acacourse_id!r}"}
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        if note is not None:
            method = "saveCourseFollowupNoteToSF"
            # A note is a plain string (confirmed: no JSON wrapping needed).
            params = {"theStudentAcaCourseId": acac,
                      "theCourseFollowupNote": note}
        elif date is not None:
            method = "saveCourseFollowupDateToSF"
            # The Apex method wants the date as a JSON-QUOTED ISO string, e.g.
            # the literal characters "2026-07-31" (quotes included) — observed
            # in the capture. The UI passes MM/DD/YYYY, so normalize to ISO.
            iso = to_iso_date(date)
            params = {"theStudentAcaCourseId": acac,
                      "theCourseFollowupDate": f'"{iso}"'}
        else:
            return {"error": "nothing to save (note/date both None)"}
        descriptor = f"apex://MentorForceAuraMethods/ACTION${method}"
        message = _json.dumps({"actions": [{
            "id": "1;a", "descriptor": descriptor,
            "callingDescriptor": "markup://c:MentoringCMUtilities",
            "params": params}]})
        page_uri = "/lightning/n/Caseload_App_Page"
        endpoint = f"/aura?other.MentorForceAuraMethods.{method}=1"
        js = """async ([message, ctxs, token, pageUri, endpoint]) => {
            const body = new URLSearchParams();
            body.set('message', message);
            body.set('aura.context', ctxs);
            body.set('aura.pageURI', pageUri);
            body.set('aura.token', token);
            const resp = await fetch(endpoint, {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type':
                          'application/x-www-form-urlencoded;charset=UTF-8'},
                body: body.toString(),
            });
            const txt = await resp.text();
            return {status: resp.status, body: txt.slice(0, 20000)};
        }"""
        try:
            res = target.evaluate(
                js, [message, creds["context"], creds["token"], page_uri,
                     endpoint])
        except Exception as e:
            return {"error": f"fetch failed: {e}"}
        body = (res or {}).get("body", "") or ""
        i = body.find("{")
        try:
            env = _json.loads(body[i:]) if i >= 0 else {}
        except Exception:
            env = {}
        actions = env.get("actions") or []
        state = actions[0].get("state") if actions else ""
        if not state:
            m = re.search(r'"state"\s*:\s*"([A-Z]+)"', body)
            state = m.group(1) if m else ""
        if state == "SUCCESS":
            return {"ok": True}
        err = ""
        try:
            err = (actions[0].get("error") if actions else "") \
                or env.get("exceptionMessage") or ""
        except Exception:
            err = ""
        return {"error": f"state={state!r} http={res.get('status')} "
                f"{str(err)[:200] or body[:200]}"}

    def grid_rows_by_key(self) -> dict:
        """The accumulated caseload-grid rows keyed by (StudentID, CourseCode).
        {} if no grid captured yet. Shallow copy — the App reads it on reload to
        layer the grid's rich fields onto the CSV rows."""
        grid = self._grid_data
        if not grid:
            return {}
        return dict(grid.get("by_key") or {})

    def grid_student_contact_map(self) -> dict:
        """{StudentID: contactID} harvested from the accumulated caseload grid
        (getCaseLoadMainGridData) — a COMPLETE, authoritative SF-sourced map
        (every caseload student, not just the texting opt-ins the segment export
        covers, and no fuzzy mobile/name join). {} if no grid captured yet."""
        out: dict = {}
        grid = self._grid_data
        if not grid:
            return out
        for row in (grid.get("by_key") or {}).values():
            try:
                sid = str(row.get("StudentID") or "").strip()
                cid = str(row.get("contactID") or "").strip()
            except Exception:
                continue
            if sid and cid and cid.startswith("003"):
                out[sid] = cid
        return out

    def _task_status_from_grid_api(self, want, t_start):
        """Phase-1 fast path: build pass/fail from the ACCUMULATED
        getCaseLoadMainGridData pages if they're fresh AND cover the expected
        task-bearers. Returns the scan-result dict (source='api'), or None to
        fall back to the scroll-scrape — so any doubt = no regression. Polls up
        to ~6s for the grid pages to finish arriving (the page fetches ~100 rows
        at a time, and the caseload nav that triggers them just happened)."""
        deadline = time.time() + 6.0
        best_cov = -1
        while True:
            grid = self._grid_data
            if grid and (time.time() - grid.get("ts", 0)) < 180:
                rows = list((grid.get("by_key") or {}).values())
                try:
                    by_sid = self._grid_rows_to_task_status(rows)
                except Exception:
                    by_sid = {}
                if by_sid:
                    cov = len(want & set(by_sid)) if want else len(by_sid)
                    best_cov = max(best_cov, cov)
                    enough = (cov >= 0.9 * len(want)) if want else True
                    if enough:
                        secs = round(time.perf_counter() - t_start, 1)
                        return {"by_sid": by_sid, "count": len(by_sid),
                                "rows": len(rows), "source": "api",
                                "lean": False, "secs": secs,
                                "setup_secs": secs, "loop_secs": 0.0,
                                "iters": 0, "stop": "api"}
            if time.time() >= deadline:
                break
            time.sleep(0.25)
        if want and best_cov >= 0:
            try:
                self.on_status(
                    f"  [grid] API covered {best_cov}/{len(want)} expected "
                    "— falling back to the scroll-scrape.")
            except Exception:
                pass
        return None

    @staticmethod
    def _active_page(ctx):
        """Return the best responsive page in `ctx`, or None.

        Prefers the real Salesforce page — the Caseload list first, then
        any Lightning page — over transient tabs. WGU's CSV "Download"
        spawns a short-lived export tab that downloads then closes itself;
        right after the startup auto-refresh that tab is the MOST RECENT
        page, so a naive newest-first pick would grab it. It can even pass
        the responsiveness probe at selection time and then die the moment
        the caller runs a real query ("Target page... has been closed"),
        which is exactly what made the first post-startup search fail.

        Defensive against:
         - stale closed pages (e.g. download-capture tabs Playwright
           hasn't yet cleaned out of ctx.pages),
         - pages where is_closed() returns False but the underlying
           target is mid-teardown,
         - zombie pages that pass both is_closed() AND .url access
           but raise "Target page closed" the moment a locator query
           runs. We do a cheap `locator("html").count()` probe to
           filter these — same kind of operation that subsequent
           callers will run anyway."""
        caseload = lightning = fallback = None
        for page in reversed(ctx.pages):
            try:
                if page.is_closed():
                    continue
                url = page.url or ""
                _ = page.locator("html").count()  # responsive probe
            except Exception:
                continue
            if fallback is None:
                fallback = page  # most-recent responsive page (last resort)
            if "Caseload_App_Page" in url and caseload is None:
                caseload = page
            elif "lightning.force.com" in url and lightning is None:
                lightning = page
        # Caseload list > any Lightning page (e.g. an open record) >
        # whatever responsive page we have (covers about:blank-only states).
        return caseload or lightning or fallback

    def _descendant_pids(self) -> set:
        """PIDs of every process descended from this Python process,
        via a Toolhelp32 snapshot (no third-party deps). Used to tell
        OUR browser (a child of the launcher) apart from any everyday
        Edge/Vivaldi the user has open."""
        import ctypes
        from ctypes import wintypes
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1 or snap == 0:
            return set()
        children: dict = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                children.setdefault(entry.th32ParentProcessID, []).append(
                    entry.th32ProcessID)
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        # BFS down from us.
        out: set = set()
        stack = [os.getpid()]
        while stack:
            pid = stack.pop()
            for child in children.get(pid, ()):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    def _process_exe_names(self) -> dict:
        """pid → lowercased exe filename, via a Toolhelp32 snapshot. Lets us
        tell whether a window's owning process is msedge.exe. Empty on error."""
        import ctypes
        from ctypes import wintypes
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1 or snap == 0:
            return {}
        out: dict = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                out[entry.th32ProcessID] = (entry.szExeFile or "").lower()
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        return out

    def has_foreign_edge_window(self) -> bool:
        """True if a VISIBLE Microsoft Edge window is open that isn't ours — i.e.
        the user has their own Edge running. Because we drive Edge (channel
        msedge), an already-open Edge can prevent our Playwright Edge from
        getting its own instance/session (Edge single-instance). Background
        'startup boost' msedge.exe processes have no visible window, so they
        don't trip this. Windows only; False on any error / off-Windows."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return False
        names = self._process_exe_names()
        if not names:
            return False
        try:
            ours = self._descendant_pids()
        except Exception:
            ours = set()
        ours.add(os.getpid())
        user32 = ctypes.windll.user32
        hit = {"v": False}
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                if cls.value != "Chrome_WidgetWin_1":
                    return True
                if user32.GetWindowTextLengthW(hwnd) <= 0:
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in ours:
                    return True
                if names.get(pid.value, "") == "msedge.exe":
                    hit["v"] = True
                    return False  # found one — stop enumerating
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        except Exception:
            return False
        return hit["v"]

    def _raise_browser_window(self, title_hint: str = "") -> None:
        """Pull the launcher's browser window to the OS foreground.
        `page.bring_to_front()` only activates the tab *within* the
        browser; on Windows it does NOT raise the browser window above
        other apps, so a navigation fired while the launcher has focus
        lands on a window hidden behind the app and looks like nothing
        happened.

        We match the Chromium/Edge top-level window whose owning process
        descends from this launcher (so we never grab the user's
        everyday Edge/Vivaldi), falling back to a page-title match.
        No-op off Windows."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # Fast path: the browser window is stable for the session, so
        # reuse the handle we found last time and skip the (relatively
        # expensive) process snapshot + window enumeration. Only re-scan
        # if the cached handle is gone (browser relaunched/closed).
        cached = getattr(self, "_browser_hwnd", None)
        if cached and user32.IsWindow(cached) and user32.IsWindowVisible(cached):
            self._raise_hwnd(cached)
            return

        try:
            ours = self._descendant_pids()
        except Exception:
            ours = set()

        # (hwnd, pid, title) for every visible, titled Chromium window.
        cands: list = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                if cls.value != "Chrome_WidgetWin_1":
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True  # toolbars / hidden helpers have no title
                tbuf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, tbuf, n + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                cands.append((hwnd, pid.value, tbuf.value))
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        except Exception:
            return

        # Pick OUR browser's window. Chrome, Edge, Vivaldi, Claude, Discord,
        # etc. all share the Chrome_WidgetWin_1 class, so a title or "only one
        # window" guess across EVERY Chromium window could grab the user's own
        # browser and yank it around. So: only a window owned by our descendant
        # process counts as ours (prefer one whose title matches the page);
        # fall back to a sole system-wide candidate only when it's unambiguous.
        chosen = None  # (hwnd, pid)
        mine = [c for c in cands if c[1] in ours]
        if mine:
            chosen = (mine[0][0], mine[0][1])
            if title_hint:
                h = title_hint.lower()
                for c in mine:
                    if c[2] and h in c[2].lower():
                        chosen = (c[0], c[1])
                        break
        elif len(cands) == 1:
            chosen = (cands[0][0], cands[0][1])
        if chosen is None:
            # Couldn't find OUR window. Usually means our Edge shares a process
            # with the user's already-open Edge (single-instance), so none of
            # the enumerated windows trace back to us.
            self.on_status(
                f"  [raise] couldn't find our browser window "
                f"({len(mine)} of {len(cands)} Chromium window(s) are ours)"
            )
            return
        # Cache hwnd + owning pid for the fast path and the focus guard.
        self._browser_hwnd, self._browser_pid = chosen
        self._raise_hwnd(chosen[0])

    def _locate_browser_hwnd(self):
        """Return the launcher's browser top-level HWND (cached, else
        enumerated), WITHOUT raising/focusing it. None if not found.
        Windows only."""
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return None
        user32 = ctypes.windll.user32
        cached = getattr(self, "_browser_hwnd", None)
        if cached and user32.IsWindow(cached):
            return cached
        try:
            ours = self._descendant_pids()
        except Exception:
            ours = set()
        cands: list = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                if cls.value != "Chrome_WidgetWin_1":
                    return True
                if user32.GetWindowTextLengthW(hwnd) <= 0:
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                iconic = bool(user32.IsIconic(hwnd))
                cands.append((hwnd, pid.value, title.value, area, iconic))
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        except Exception:
            return None
        mine = [c for c in cands if c[1] in ours]
        if not mine:
            # Other Chromium apps (Claude, Discord, Spotify, the user's own
            # Chrome/Vivaldi) ALL use the Chrome_WidgetWin_1 class, so a
            # title/size guess across every candidate could grab one of THEM
            # and (e.g.) shove the user's window off-screen. Only a single,
            # unambiguous candidate is safe to assume is ours.
            if len(cands) == 1:
                mine = cands
            else:
                return None
        # Edge spawns junk top-level windows alongside the real browser: the
        # tiny "Restore pages" crash bubble (after an unclean exit) and other
        # transient helpers. Picking the FIRST enumerated one grabbed that
        # bubble, so minimize / raise / the off-screen scan move all targeted
        # the wrong window. Pick the REAL browser window instead: drop the
        # known bubble by title, then prefer a non-iconic window with the
        # largest area (the actual page host).
        real = [c for c in mine if c[2] != "Restore pages"] or mine
        chosen = max(real, key=lambda c: (not c[4], c[3]), default=None)
        if chosen is None:
            return None
        self._browser_hwnd, self._browser_pid = chosen[0], chosen[1]
        return chosen[0]

    def set_browser_enabled(self, enabled: bool) -> None:
        """Enable/disable OS input to the launcher's browser window so the
        user can't click/scroll/type into it mid-automation — doing so
        changes the active console record and breaks a running fire
        ('No visible note panel'). Playwright drives the page over CDP,
        not OS input, so automation keeps working while the window is
        disabled. Safe to call from any thread; Windows-only no-op
        otherwise."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = self._locate_browser_hwnd()
            if hwnd:
                ctypes.windll.user32.EnableWindow(hwnd, bool(enabled))
        except Exception:
            pass

    def _user_is_on_us(self) -> bool:
        """True if the current OS foreground window belongs to the
        launcher or to our own browser. When it belongs to some OTHER
        app (the user has moved on while a record loads), we must NOT
        raise the browser — doing so steals focus / grabs the cursor
        from whatever they're now using. Defaults to True off Windows."""
        if sys.platform != "win32":
            return True
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            allowed = {os.getpid()}
            bp = getattr(self, "_browser_pid", None)
            if bp:
                allowed.add(bp)
            return pid.value in allowed
        except Exception:
            return True

    def _bring_browser_forward(self, page) -> None:
        """Activate the right tab and pull the browser window to the OS
        foreground. Call this AFTER a navigation has completed — calling
        it before clicking races Lightning's re-render on tab activation
        and the click misses. Skipped entirely when the user has focused
        a different app, so we never yank focus away from their work."""
        if not self._user_is_on_us():
            return
        try:
            if page is not None:
                page.bring_to_front()
        except Exception:
            pass
        self._raise_browser_window()

    def _raise_hwnd(self, hwnd) -> None:
        """Bring a known top-level window to the OS foreground. We rely
        on the fact that the launcher is the foreground process when this
        runs (guarded by _user_is_on_us), so a plain SetForegroundWindow
        is honoured. We deliberately do NOT use AttachThreadInput to
        force it past the foreground lock — that defeats Windows' own
        focus-steal protection and was grabbing the cursor from other
        apps."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # Only un-minimize when actually minimized. SW_RESTORE on a
            # snapped (Win+Arrow half-max) or maximized window reverts it
            # to its pre-snap "normal" rectangle — which Windows never
            # updated to the snap location, so the window jumps to a
            # stale, possibly off-screen spot. A snapped window reports
            # as normal (not iconic), so skipping restore leaves the
            # half-max layout untouched.
            if user32.IsIconic(hwnd):
                SW_RESTORE = 9
                user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _minimize_browser_window(self) -> None:
        """Minimize the launcher's browser window — called once the startup
        caseload load is done so it's out of the user's way. No-op off
        Windows / if the window can't be located."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = self._locate_browser_hwnd()
            if hwnd and user32.IsWindow(hwnd):
                SW_MINIMIZE = 6
                user32.ShowWindow(hwnd, SW_MINIMIZE)
        except Exception:
            pass

    @contextmanager
    def _lean_scan_columns(self, table, target):
        """Hide every datatable column EXCEPT the Student ID column for the
        duration of the bulk pass/fail scan, then restore. This is the real
        lag fix: rendering the caseload list's ~100 dead columns (course
        dates, contacts, assessment statuses — none of which the scan reads)
        is what made each scroll-jump re-layout + re-raster a huge grid and
        stutter the whole machine. Hiding them with CSS collapses that to a
        single narrow column.

        Crucially this does NOT change what the scan reads or what the CSV
        export contains:
          - `scan_task_status_window` reads the task cells via
            querySelectorAll, which still finds display:none nodes, and
            Lightning still builds the full row DOM on lazy-load regardless of
            our CSS — so every student's pass/fail is still captured.
          - The CSV "Download" is driven by the Salesforce view's column
            metadata, not the rendered DOM, so it stays complete. (Editing the
            view's columns instead would shrink the export — the trap we're
            avoiding.)
        The SID column is kept VISIBLE so rows keep their height; if every
        column were hidden the rows would collapse, the scroll container's
        scrollHeight would go flat, and the infinite-scroll would stop firing.

        Scoped to the one table via a marker class so the rule can't leak to
        any other grid. No-op (yields False) on any failure, so the scan still
        runs at full width rather than not at all."""
        applied = False
        try:
            # 1-based index (for :nth-child) of the first column whose cell is
            # a 9-10 digit Student ID — found from the rows already rendered.
            idx = table.evaluate(r'''(tbl) => {
                for (const r of tbl.querySelectorAll('tr')) {
                  const tds = r.querySelectorAll('td');
                  for (let i = 0; i < tds.length; i++) {
                    if (/^\d{9,10}$/.test((tds[i].textContent || '').trim()))
                      return i + 1;
                  }
                }
                return 0;
              }''')
            if idx and idx > 0:
                table.evaluate(
                    "(tbl) => tbl.classList.add('__lean_scan__')")
                target.evaluate(r'''([sid, styleId]) => {
                    let st = document.getElementById(styleId);
                    if (!st) {
                      st = document.createElement('style');
                      st.id = styleId;
                      document.head.appendChild(st);
                    }
                    // :nth-child counts position within the <tr>, so this works
                    // whether rows sit under <thead>/<tbody> or the table direct.
                    st.textContent =
                      'table.__lean_scan__ th,table.__lean_scan__ td' +
                      '{display:none !important;}' +
                      'table.__lean_scan__ th:nth-child(' + sid + '),' +
                      'table.__lean_scan__ td:nth-child(' + sid + ')' +
                      '{display:table-cell !important;}';
                  }''', [idx, "__lean_scan_style__"])
                applied = True
        except Exception:
            applied = False
        try:
            yield applied
        finally:
            if applied:
                try:
                    target.evaluate('''(styleId) => {
                        const s = document.getElementById(styleId);
                        if (s) s.remove();
                      }''', "__lean_scan_style__")
                except Exception:
                    pass
                try:
                    table.evaluate(
                        "(tbl) => tbl.classList.remove('__lean_scan__')")
                except Exception:
                    pass

    def _open_caseload_table(self, ctx):
        """Common helper: navigate to Caseload (if not already there)
        and return a locator for the data table.

        Retries on transient failures (page closed mid-goto, target
        died between active_page check and call). If no live page is
        available at all, creates a fresh one rather than giving up
        — keeps the launcher usable even after browser hiccups."""
        last_error = ""
        for attempt in range(3):
            target = self._active_page(ctx)
            if target is None:
                try:
                    target = ctx.new_page()
                except Exception as e:
                    last_error = f"new_page failed: {e}"
                    continue
            try:
                current_url = target.url or ""
            except Exception:
                current_url = ""
            if CASELOAD_URL and "Caseload_App_Page" not in current_url:
                try:
                    target.goto(CASELOAD_URL, wait_until="domcontentloaded")
                except Exception as e:
                    self.on_status(
                        f"  [debug] goto caseload (attempt {attempt + 1}): {e}"
                    )
                    last_error = str(e)
                    continue
            try:
                tables = (
                    target.locator("table")
                    .filter(has=target.locator('th:has-text("Course Code")'))
                    .filter(has=target.locator('th:has-text("Name")'))
                )
                tables.first.wait_for(state="visible", timeout=20_000)
                return target, tables.first
            except Exception as e:
                last_error = str(e)
                continue
        if target is not None and self._looks_like_login(target):
            # Not signed in yet (common on a fresh launch) — a friendly nudge,
            # NOT the raw Playwright timeout dump.
            self.on_status(
                "⚠ Not signed in to Salesforce yet — sign in in the browser "
                "window (opening now), then click ↻ Caseload to load your "
                "students.")
            try:
                self._raise_browser_window()
            except Exception:
                pass
        else:
            first_line = (last_error or "").splitlines()[0] if last_error else ""
            self.on_status(
                f"Caseload table didn't load after retries: {first_line}")
        return None, None

    def _looks_like_login(self, target) -> bool:
        """True if the active page looks like a Salesforce sign-in page — by
        URL (login host / *.salesforce.com/login) or a visible username +
        password field pair. Used to surface 'please sign in' in the log
        (the browser is usually minimized, so a silent failure is confusing)."""
        try:
            url = (target.url or "").lower()
        except Exception:
            url = ""
        if any(s in url for s in (
                "login.salesforce.com", "/login", "/_ui/login", "secur/login",
                # WGU single-sign-on chain + Salesforce session redirectors the
                # browser passes through mid-login (SAML → PingID OTP → frontdoor).
                "access.wgu.edu", "pingfed", "pingone.com", "pingid",
                "authenticator.ping", "secur/frontdoor", "secur/contentdoor")):
            return True
        try:
            u = target.locator('input[name="username"], input#username')
            p = target.locator('input[type="password"]')
            if u.count() > 0 and p.count() > 0:
                return True
        except Exception:
            pass
        return False

    def _grid_note_lookup(self, sid: str):
        """(contact_id, course, logged_in_user_id) for a Student ID, read from
        the captured caseload grid. (None, None, None) if not found / no grid."""
        grid = self._grid_data
        if not grid or not sid:
            return None, None, None
        for (gsid, gcourse), row in (grid.get("by_key") or {}).items():
            if gsid == sid:
                cid = str(row.get("contactID") or "").strip()
                uid = str(row.get("LoggedInUserId") or "").strip()
                return ((cid if cid.startswith("003") else None),
                        gcourse, (uid or None))
        return None, None, None

    def _fetch_notes_via_api(self, target, contact_id, course, user_id,
                             max_notes):
        """Read the CURRENT USER's notes for a student+course via the
        getAllStudentCourseNotes Aura action (fetch, no navigation). Returns
        notes in the SAME shape the scrape produces (but only this CM's notes,
        course-scoped — server filters by CMUserId + CourseCode), or None on any
        failure."""
        import json as _json
        creds = self._aura_creds
        if not (creds and contact_id and user_id and target is not None):
            return None
        params = {"StudentContactId": contact_id, "CMUserId": user_id,
                  "CourseCode": course or ""}
        message = _json.dumps({"actions": [{
            "id": "1;a",
            "descriptor":
                "apex://MentorForceAuraMethods/ACTION$getAllStudentCourseNotes",
            "callingDescriptor": "markup://c:MentorCMGridAndDetailGrid",
            "params": params}]})
        page_uri = f"/lightning/r/Contact/{contact_id}/view"
        js = """async ([message, ctxs, token, pageUri]) => {
            const body = new URLSearchParams();
            body.set('message', message);
            body.set('aura.context', ctxs);
            body.set('aura.pageURI', pageUri);
            body.set('aura.token', token);
            const resp = await fetch(
                '/aura?other.MentorForceAuraMethods.getAllStudentCourseNotes=1',
                {method: 'POST', credentials: 'include',
                 headers: {'Content-Type':
                           'application/x-www-form-urlencoded;charset=UTF-8'},
                 body: body.toString()});
            return {status: resp.status, body: await resp.text()};
        }"""
        try:
            res = target.evaluate(
                js, [message, creds["context"], creds["token"], page_uri])
        except Exception:
            return None
        body = (res or {}).get("body", "") or ""
        i = body.find("{")
        try:
            env = _json.loads(body[i:]) if i >= 0 else {}
        except Exception:
            return None
        actions = env.get("actions") or []
        if not actions or actions[0].get("state") != "SUCCESS":
            return None
        rv = actions[0].get("returnValue")
        if not isinstance(rv, list):
            return None
        out = []
        for n in rv[:max_notes]:
            out.append({
                "type": (n.get("Type__c") or ""),
                "course": (n.get("CourseCode__c") or ""),
                "subject": (n.get("Name") or ""),
                "text": (n.get("Text__c") or n.get("ShortText__c") or ""),
                "author": ((n.get("Author__r") or {}).get("Name") or ""),
                "date": (n.get("WGUCreationDateTime__c")
                         or n.get("CreatedDate") or ""),
                "url": "",
            })
        return out

    def _fetch_my_notes(self, ctx, query: str, max_notes: int = 60) -> dict:
        """Fast first-pass: the current user's notes for the student, via the
        API. {"notes": [...], "source": "mine"} on success, or {"notes": None,
        "reason": ...} to signal the caller to load the full all-author scrape.
        Carries timing to diagnose the cold-first-read slowness."""
        import time as _t
        sid = (query or "").strip()
        if not sid.isdigit():
            return {"notes": None, "reason": "non-numeric query"}
        cid, course, uid = self._grid_note_lookup(sid)
        if not (cid and uid):
            return {"notes": None, "reason": "no grid contact-id/user-id"}
        # A note read right after startup can beat the Aura-creds harvest — wait
        # briefly for it rather than falling back to the slow all-author scrape
        # (the cause of the one-off ~15s first read).
        deadline = _t.time() + 3.0
        while not self._aura_creds and _t.time() < deadline:
            _t.sleep(0.15)
        if not self._aura_creds:
            return {"notes": None, "reason": "no Aura creds harvested yet"}
        t_eval = _t.time()
        notes = self._fetch_notes_via_api(
            self._active_page(ctx), cid, course, uid, max_notes)
        eval_ms = int((_t.time() - t_eval) * 1000)
        if notes is None:
            return {"notes": None, "reason": f"API call failed ({eval_ms}ms)"}
        return {"notes": notes, "source": "mine", "course": course or "",
                "eval_ms": eval_ms}

    def _fetch_student_notes(
        self, ctx, query: str, max_notes: int = 60, contact_id: str = "",
    ) -> dict:
        """Open the student's record and scrape their note history.

        The notes live in a ShortText datatable that lazy-loads AFTER the
        record opens, so we poll the *visible* ShortText cells until they
        appear (the global caseload Notes-History table is in the DOM too
        but stays hidden when a record subtab is foreground, so `:visible`
        isolates this student's notes). Returns
        {notes:[{type,subject,course,date,author,text}], count, timings}.
        """
        import time as _t
        timings: dict = {}
        t0 = _t.time()
        # 1. Navigate to the student's record (foregrounds the subtab so
        #    the hidden global table drops out of :visible). Prefer a deep-link
        #    by Contact id — works even when the Student ID column isn't in the
        #    caseload view (which breaks the row-filter search); fall back to
        #    the search when no id / the deep-link doesn't render a record.
        try:
            navigated = False
            if (contact_id or "").startswith("003"):
                navigated = self._navigate_to_contact(ctx, contact_id)
            if not navigated:
                self._handle_find(ctx, query, new_tab=False, raise_after=False)
        except Exception as e:
            return {"error": f"navigation failed: {e}", "timings": timings}
        timings["nav_ms"] = int((_t.time() - t0) * 1000)
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page", "timings": timings}

        try:
            timings["url"] = (target.url or "")[:90]
        except Exception:
            timings["url"] = ""
        # 2. The record opens on the "Essential Actions" tab; the per-student
        #    note table lives in the *Notes History* scoped tab, which
        #    Lightning doesn't render until it's activated. Click it.
        t_tab = _t.time()
        timings["tab"] = "not found"
        for _ in range(20):  # wait up to ~5s for the tab to exist
            try:
                tab = target.locator(
                    'a[data-tab-value="NotesHistoryTab"]').first
                if tab.count() > 0:
                    tab.click(timeout=2000)
                    timings["tab"] = "clicked"
                    break
            except Exception as e:
                timings["tab"] = f"click error: {e}"
            try:
                target.wait_for_timeout(250)
            except Exception:
                break
        timings["tab_ms"] = int((_t.time() - t_tab) * 1000)

        sel = 'td[data-col-key-value*="ShortText__c"]:visible'
        sel_all = 'td[data-col-key-value*="ShortText__c"]'
        # 3. Poll until the per-student notes table populates inside the tab.
        t1 = _t.time()
        count = 0
        for _ in range(40):  # ~10s ceiling (250ms * 40)
            try:
                count = target.locator(sel).count()
            except Exception:
                count = 0
            if count > 0:
                break
            try:
                target.wait_for_timeout(250)
            except Exception:
                break
        timings["notes_load_ms"] = int((_t.time() - t1) * 1000)
        try:
            timings["all_cells"] = target.locator(sel_all).count()
        except Exception:
            timings["all_cells"] = -1

        # 3. Scrape every visible note row in ONE page.evaluate() round-trip
        #    (vs. ~1600 per-cell locator calls — cuts scrape from ~5s to
        #    ~50ms). Column keys from the live DOM: Type__c / CourseCode__c /
        #    Name(=Subject) / ShortText__c / Author__c / WGUCreationDateTime__c.
        #    Full body lives in each cell's data-cell-value attr.
        t2 = _t.time()
        notes: list[dict] = []
        # IMPORTANT: scrape via locator.evaluate_all, NOT page.evaluate.
        # The Notes History datatable is a Lightning Web Component whose
        # cells live inside shadow roots. Playwright's selector engine
        # pierces shadow DOM (so the locator resolves all 270 cells), then
        # hands those elements to the JS; a plain document.querySelectorAll
        # in page.evaluate does NOT pierce shadow DOM and returns nothing.
        js = """
        (cells, maxNotes) => {
          const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
          const out = [];
          const seen = new Set();
          for (const cell of cells) {
            if (out.length >= maxNotes) break;
            const row = cell.closest('tr');
            if (!row) continue;
            const pick = (key, prefix) => {
              const sel = prefix
                ? 'td[data-col-key-value^="' + key + '"]'
                : 'td[data-col-key-value*="' + key + '"]';
              const td = row.querySelector(sel);
              if (!td) return "";
              return norm(td.getAttribute("data-cell-value") || td.textContent);
            };
            // The Record ID column renders an anchor to the note record,
            // where the full Text lives. Grab its href.
            let url = "";
            const a = row.querySelector('a[href]');
            if (a) url = a.href;
            const rec = {
              type: pick("Type__c", false),
              course: pick("CourseCode__c", false),
              subject: pick("Name-", true),
              text: pick("ShortText__c", false),
              author: pick("Author__c", false),
              date: pick("WGUCreationDateTime__c", false),
              url: url,
            };
            // De-dup if the same table appears twice in the DOM.
            const k = rec.date + "|" + rec.subject + "|" + rec.text.slice(0, 40);
            if (seen.has(k)) continue;
            seen.add(k);
            out.push(rec);
          }
          return out;
        }
        """
        try:
            notes = target.locator(sel_all).evaluate_all(js, max_notes) or []
        except Exception as e:
            timings["scrape_error"] = str(e)
        timings["scrape_ms"] = int((_t.time() - t2) * 1000)
        timings["total_ms"] = int((_t.time() - t0) * 1000)
        return {"notes": notes, "count": count, "timings": timings}

    @staticmethod
    def _clean_caseload_headers(table) -> list[str]:
        """Strip the 'sorting options' / 'column actions' UI noise off
        Lightning's <th> text and dedupe, returning a clean list of
        column names in left-to-right order."""
        import re as _re
        raw = table.locator("th").all_text_contents()
        out: list[str] = []
        for h in raw:
            h = h.strip()
            if not h or h.startswith("Sort by:"):
                continue
            h = _re.sub(
                r"\s*(sorting options|column actions).*$",
                "", h, flags=_re.IGNORECASE,
            ).strip()
            if h and h not in out:
                out.append(h)
        return out

    def _read_caseload_columns(self, ctx) -> list[dict]:
        """Return list of `{name, type}` dicts for every visible column
        in the user's caseload list view. Type is sniffed from a sample
        of cells in each column."""
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return []
        headers = self._clean_caseload_headers(table)
        if not headers:
            return []
        rows = table.locator("tr")
        n_rows = min(rows.count(), 11)  # 1 header + up to 10 data rows
        samples: list[list[str]] = [[] for _ in headers]
        for r in range(1, n_rows):
            try:
                cells = rows.nth(r).locator("td").all_text_contents()
            except Exception:
                continue
            for i in range(len(headers)):
                if i < len(cells):
                    v = cells[i].strip()
                    if v:
                        samples[i].append(v)
        self.on_status(f"Caseload columns refreshed: {len(headers)} visible.")
        return [
            {"name": h, "type": caseload_filter.sniff_column_type(s)}
            for h, s in zip(headers, samples)
        ]

    def _download_caseload_csv(
        self, ctx, save_path: Path,
    ) -> tuple[bool, str]:
        """Click WGU's custom Download button on the Caseload list
        view and save the resulting CSV to `save_path`. Returns
        (success, message).

        The Download button (`title="Download"`) lives directly to
        the left of the Mass-email button in the list view toolbar
        — confirmed unique via the saved Caseload.html snapshot.
        Playwright's `expect_download` context catches the file
        before Edge dumps it into its temp artifacts folder.

        IMPORTANT: clears Salesforce's row filter before clicking
        Download. The Export honors the current filter, so a leftover
        value (from a prior fast-find) would emit a single-row CSV
        and silently corrupt the cache."""
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return False, "caseload table didn't load"

        # Clear any leftover row filter so the export covers the
        # whole caseload, not whatever a previous fast-find narrowed
        # the view to.
        try:
            filter_input = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if filter_input.count() > 0:
                filter_input.focus()
                filter_input.fill("")
                filter_input.press("Enter")
                _wait_grid_settled(target, 800)
        except Exception as e:
            self.on_status(f"  [export] couldn't clear row filter: {e}")

        try:
            btn = target.locator('button[title="Download"]').first
            btn.wait_for(state="visible", timeout=10_000)
        except Exception as e:
            return False, f"Download button not found: {e}"

        try:
            with target.expect_download(timeout=30_000) as dl_info:
                btn.click()
                self.on_status("  [export] clicked Download button")
            download = dl_info.value
        except Exception as e:
            return False, f"download did not start: {e}"

        try:
            sp = Path(save_path)
            sp.parent.mkdir(parents=True, exist_ok=True)
            tmp = sp.with_name(sp.name + ".new")
            download.save_as(str(tmp))
            # Anti-clobber guard: if the new export has FEWER columns than
            # the existing cache (e.g. the browser view lost columns), keep
            # the previous file as .bak and warn — silent data loss here is
            # how a wrong view quietly breaks viewer features.
            #
            # HARDENING: the caseload "Download" occasionally drifts its
            # column set between runs (a render/timing hiccup). If a run drops
            # a CRITICAL join column — above all StudentID, which every CSV
            # fallback path keys on — we REJECT the new file and keep the
            # previous good CSV in place, so the CSV fallback stays valid even
            # when the export hiccups. (The JSON grid feed already carries
            # StudentID, so the primary path is unaffected either way.)
            dropped: list[str] = []
            critical_dropped: list[str] = []
            if sp.exists():
                try:
                    old_h = caseload_csv.csv_header(sp)
                    new_h = caseload_csv.csv_header(tmp)
                    dropped = caseload_csv.dropped_columns(old_h, new_h)
                    critical_dropped = caseload_csv.critical_columns_dropped(
                        old_h, new_h, self._CSV_CRITICAL_COLUMNS)
                except Exception:
                    dropped = []
                    critical_dropped = []
            if critical_dropped:
                # Keep the existing good CSV; stash the bad export for
                # inspection rather than overwriting.
                try:
                    tmp.replace(sp.with_name(sp.name + ".rejected"))
                except Exception:
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                self.on_status(
                    "  [export] WARNING: new download dropped critical "
                    f"column(s) ({', '.join(critical_dropped)}) — KEPT the "
                    f"previous CSV instead of overwriting it. Bad export "
                    f"saved as {sp.name}.rejected. Check your Caseload view "
                    "if unintended. (The JSON grid feed is unaffected.)")
            else:
                if sp.exists():
                    try:
                        sp.replace(sp.with_name(sp.name + ".bak"))
                    except Exception:
                        pass
                tmp.replace(sp)
                if dropped:
                    self.on_status(
                        "  [export] WARNING: new download dropped "
                        f"{len(dropped)} column(s) vs the previous CSV "
                        f"({', '.join(dropped[:6])}"
                        f"{'…' if len(dropped) > 6 else ''}). Previous saved "
                        f"as {sp.name}.bak — check your Caseload view if "
                        "unintended.")
        except Exception as e:
            return False, f"download save failed: {e}"

        # WGU's Download action occasionally drifts the page off the
        # Caseload list URL. If it did, re-activate the live list now —
        # this runs during the (backgrounded) startup auto-refresh, so the
        # user's FIRST student search starts from the fast path
        # (on_caseload True) instead of paying a wasted Shift+X poll + full
        # reload. No-op when the export already ended on Caseload, so it
        # costs nothing in the common case.
        try:
            if "Caseload_App_Page" not in (target.url or ""):
                self._ensure_caseload_list(target)
        except Exception:
            pass
        return True, f"saved to {Path(save_path).name}"

    def _download_outcomes_archive(
        self, ctx, save_path: Path,
    ) -> tuple[bool, str]:
        """Switch the caseload list-view picker to 'Archive (Last 30 Days)',
        click the same Download button, save the CSV to `save_path`, then
        RESTORE the prior list view. Grid-data capture is suppressed for the
        whole window so the archive's passed-student rows can't pollute the
        My-Students grid (pass/fail + contact-id map). Returns (ok, msg).

        The list-view picker is a native <select class="uiInputSelect"> whose
        options include 'My Students' and 'Archive (Last 30 Days)' (confirmed
        from the saved Caseload.html snapshot), so select_option drives it."""
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return False, "caseload table didn't load"
        # Find the list-view <select>: the uiInputSelect that has an option
        # matching /archive.*30/i. (Other selects on the page won't.)
        sel = None
        try:
            cands = target.locator("select.uiInputSelect")
            for i in range(min(cands.count(), 8)):
                c = cands.nth(i)
                has = c.evaluate(
                    "(s) => [...s.options].some(o => /archive/i.test(o.text)"
                    " && /30/.test(o.text))")
                if has:
                    sel = c
                    break
        except Exception as e:
            return False, f"list-view picker not found: {e}"
        if sel is None:
            return False, "couldn't find the 'Archive (Last 30 Days)' view picker"

        # Remember the currently-selected view so we restore exactly what the
        # user had, and resolve the archive option's value robustly by text.
        try:
            prev_value = sel.input_value()
        except Exception:
            prev_value = ""
        try:
            archive_value = sel.evaluate(
                "(s) => { const o = [...s.options].find(o =>"
                " /archive/i.test(o.text) && /30/.test(o.text));"
                " return o ? o.value : ''; }")
        except Exception:
            archive_value = ""
        if not archive_value:
            return False, "Archive option missing from the view picker"

        self._suppress_grid_capture = True
        try:
            try:
                sel.select_option(value=archive_value)
            except Exception as e:
                return False, f"couldn't switch to the Archive view: {e}"
            # Let the Archive list load before exporting (the export honors the
            # currently-shown view). Give the re-fetch a beat, then settle.
            try:
                target.wait_for_timeout(700)
                _wait_grid_settled(target, 4000)
            except Exception:
                pass
            # Same Download button as the My-Students export.
            try:
                btn = target.locator('button[title="Download"]').first
                btn.wait_for(state="visible", timeout=10_000)
            except Exception as e:
                return False, f"Download button not found in Archive view: {e}"
            try:
                with target.expect_download(timeout=30_000) as dl_info:
                    btn.click()
                    self.on_status("  [archive] clicked Download (Archive view)")
                download = dl_info.value
            except Exception as e:
                return False, f"archive download did not start: {e}"
            try:
                sp = Path(save_path)
                sp.parent.mkdir(parents=True, exist_ok=True)
                tmp = sp.with_name(sp.name + ".new")
                download.save_as(str(tmp))
                tmp.replace(sp)
            except Exception as e:
                return False, f"archive save failed: {e}"
        finally:
            # Always restore the prior list view, even on an error above, so the
            # app is never left sitting on the Archive view.
            try:
                if prev_value:
                    sel.select_option(value=prev_value)
                    target.wait_for_timeout(500)
                    _wait_grid_settled(target, 4000)
            except Exception as e:
                self.on_status(f"  [archive] couldn't restore the view: {e}")
            self._suppress_grid_capture = False
        return True, f"archive saved to {Path(save_path).name}"

    def _scroll_datatable_to_bottom(self, table) -> None:
        """Force the caseload datatable's scroll CONTAINER to its bottom to
        trigger the next lazy-load chunk. More reliable than scroll-into-view
        of the last row, which stops as soon as that row is visible and can
        stall the infinite-load before the whole list is in the DOM (the cause
        of the bulk scrapes intermittently loading only part of the caseload).
        Walks up from the table to the first scrollable ancestor. No-op on
        failure (the scroll-into-view fallback still runs)."""
        try:
            table.evaluate(
                "(tbl) => { let el = tbl;"
                " for (let i = 0; i < 10 && el; i++) {"
                "   const oy = getComputedStyle(el).overflowY;"
                "   if (el.scrollHeight > el.clientHeight + 4 &&"
                "       (oy === 'auto' || oy === 'scroll')) {"
                "     el.scrollTop = el.scrollHeight; return; }"
                "   el = el.parentElement; } }")
        except Exception:
            pass

    def _read_all_caseload_rows(
        self, ctx,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> list[dict]:
        """Scroll the caseload table to load every row, then return
        them as a list of `{column_name: cell_text}` dicts. Lightning
        lazy-loads rows on scroll; we drive the last `<tr>` into view
        in a loop until the row count is stable for two checks.
        Skips rows that are entirely empty (placeholder shells)."""
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return []
        # Clear any active row filter so we see the whole caseload.
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click(); fi.fill("")
                fi.press("Enter")
                _wait_grid_settled(target, 1500)
        except Exception:
            pass

        # Start at the top so the first window's rows aren't skipped if the
        # list was left scrolled down.
        try:
            r0 = table.locator("tr")
            if r0.count() > 0:
                r0.nth(0).scroll_into_view_if_needed(timeout=2000)
                target.wait_for_timeout(250)
        except Exception:
            pass

        last_count = -1
        stable = 0
        MAX_ITERS = 300
        for _ in range(MAX_ITERS):
            rows = table.locator("tr")
            count = rows.count()
            if on_progress:
                try:
                    on_progress(max(count - 1, 0))  # subtract header
                except Exception:
                    pass
            # Stop only after the row count holds for THREE checks (was two) —
            # the list lazy-loads in chunks and a single slow chunk could
            # otherwise truncate the load (the same race that under-read the
            # bulk task scrape).
            if count == last_count:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            last_count = count
            self._scroll_datatable_to_bottom(table)
            try:
                last_row = rows.nth(count - 1)
                last_row.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                target.wait_for_timeout(400)
            except Exception:
                pass

        headers = self._clean_caseload_headers(table)
        if not headers:
            return []
        rows = table.locator("tr")
        n_rows = rows.count()
        out: list[dict] = []
        for r in range(1, n_rows):
            try:
                cells = rows.nth(r).locator("td").all_text_contents()
            except Exception:
                continue
            row_dict = {}
            for i, h in enumerate(headers):
                row_dict[h] = cells[i].strip() if i < len(cells) else ""
            if any(v for v in row_dict.values()):
                out.append(row_dict)
        self.on_status(f"Caseload loaded: {len(out)} rows.")
        return out

    def _fetch_task_status(self, ctx, query: str) -> dict:
        """Row-filter the live Caseload list to `query` (a Student ID) and
        read the per-task pass/fail from the cell color/title — the bit the
        CSV export drops. Returns {"statuses": {"1": {...}, ...}} or
        {"error": msg}. Restores the row filter afterward so the live list
        isn't left narrowed."""
        from src.student_lookup import lookup_task_status
        q = (query or "").strip()
        if not q:
            return {"error": "no student id"}
        # Prefer the live grid JSON we already captured — it holds every
        # student's per-task pass/fail, so no row filter (and no Student ID
        # column) is needed. Fall through to the list scrape if this student
        # isn't in the grid yet.
        try:
            rows_for_sid = [r for (sid, _c), r in self.grid_rows_by_key().items()
                            if sid == q]
            if rows_for_sid:
                st = self._grid_rows_to_task_status(rows_for_sid).get(q)
                if st:
                    return {"statuses": st}
        except Exception:
            pass
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return {"error": "caseload table didn't load"}
        fi = None
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click()
                fi.fill(q)
                fi.press("Enter")
                _wait_grid_settled(target, 1200)
        except Exception as e:
            return {"error": f"row filter failed: {e}"}
        err = ""
        statuses: dict = {}
        try:
            statuses = lookup_task_status(target, q)
        except Exception as e:
            err = str(e)
        # Always try to clear the filter so the live list is whole again.
        try:
            if fi is not None and fi.count() > 0:
                fi.click()
                fi.fill("")
                fi.press("Enter")
                target.wait_for_timeout(400)
        except Exception:
            pass
        if err:
            return {"error": err}
        return {"statuses": statuses}

    def _set_followup_date(self, ctx, query: str, date_str: str) -> dict:
        """Row-filter the live Caseload list to `query` (a Student ID), set
        that row's Followup Date to `date_str`, then restore the filter.
        Returns {ok, value, error}."""
        from src.student_lookup import set_followup_date
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "no student id"}
        # Fast path: persist via the Aura API (saveCourseFollowupDateToSF) —
        # same fix as the note (the inline edit could commit a bad value). Only
        # for a non-empty date; clearing still uses the inline "Clear" button.
        acac = self._grid_acacourse_id(q)
        if (date_str or "").strip() and acac and self._aura_creds:
            api = self._save_followup_via_api(ctx, acacourse_id=acac,
                                              date=date_str)
            if api.get("ok"):
                return {"ok": True, "value": date_str,
                        "committed_via": "API"}
            self.on_status(
                f"  ↳ follow-up date API save failed "
                f"({api.get('error')}); using inline edit")
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return {"ok": False, "error": "caseload table didn't load"}
        fi = None
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click(); fi.fill(q); fi.press("Enter")
                _wait_grid_settled(target, 1200)
        except Exception as e:
            return {"ok": False, "error": f"row filter failed: {e}"}
        cap: list = []
        _stop = self._arm_flup_save_capture(target, cap)
        try:
            res = set_followup_date(target, date_str)
        except Exception as e:
            res = {"ok": False, "value": "", "error": str(e)}
        finally:
            _stop()
        self._dump_flup_save_capture(cap, date_str, "date")
        self._persist_followup_diag(res, "date")
        # Restore the filter so the live list is whole again.
        try:
            if fi is not None and fi.count() > 0:
                fi.click(); fi.fill(""); fi.press("Enter")
                target.wait_for_timeout(400)
        except Exception:
            pass
        return res

    def _set_followup_note(self, ctx, query: str, note_text: str) -> dict:
        """Row-filter the live Caseload list to `query` (a Student ID), set
        that row's Followup Note to `note_text`, then restore the filter."""
        from src.student_lookup import set_followup_note
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "no student id"}
        # Fast path: persist via the Aura API (saveCourseFollowupNoteToSF). This
        # fixes the flaky inline list-cell edit that sometimes committed a null
        # value (the 'follow-up doesn't persist' bug). No nav/filter needed.
        acac = self._grid_acacourse_id(q)
        if acac and self._aura_creds:
            api = self._save_followup_via_api(ctx, acacourse_id=acac,
                                              note=note_text)
            if api.get("ok"):
                return {"ok": True, "value": note_text,
                        "committed_via": "API"}
            self.on_status(
                f"  ↳ follow-up note API save failed "
                f"({api.get('error')}); using inline edit")
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return {"ok": False, "error": "caseload table didn't load"}
        fi = None
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click(); fi.fill(q); fi.press("Enter")
                _wait_grid_settled(target, 1200)
        except Exception as e:
            return {"ok": False, "error": f"row filter failed: {e}"}
        cap: list = []
        _stop = self._arm_flup_save_capture(target, cap)
        try:
            res = set_followup_note(target, note_text)
        except Exception as e:
            res = {"ok": False, "value": "", "error": str(e)}
        finally:
            _stop()
        self._dump_flup_save_capture(cap, note_text, "note")
        self._persist_followup_diag(res, "note")
        try:
            if fi is not None and fi.count() > 0:
                fi.click(); fi.fill(""); fi.press("Enter")
                target.wait_for_timeout(400)
        except Exception:
            pass
        return res

    def _arm_flup_save_capture(self, target, sink: list):
        """Attach a temporary /aura POST request-body capture (for discovering
        the follow-up SAVE Aura action so it can be replayed via the API). Returns
        a stop() that detaches the listener. Best-effort."""
        def _cap(req):
            try:
                if "/aura" in (req.url or "") and (req.method or "") == "POST":
                    pd = req.post_data or ""
                    if pd:
                        sink.append(pd)
            except Exception:
                pass
        try:
            target.on("request", _cap)
        except Exception:
            return lambda: None

        def _stop():
            try:
                target.remove_listener("request", _cap)
            except Exception:
                pass
        return _stop

    def _dump_flup_save_capture(self, captured: list, needle: str,
                               kind: str) -> None:
        """Write the Aura actions captured during a follow-up save to
        flup_save_probe.txt, flagging the one carrying the value we set — so we
        can build the API write. One-shot discovery; gitignored + auto-cleaned."""
        if not captured:
            return
        from src.config import USER_CONFIG_DIR
        import urllib.parse as _up
        import json as _json
        nd = (needle or "").strip().lower()
        lines = [f"=== follow-up {kind} save capture — {len(captured)} /aura "
                 f"POST(s); value set = {needle!r} ===", ""]
        for pd in captured:
            try:
                params = _up.parse_qs(pd)
                msg = (params.get("message") or [""])[0]
                env = _json.loads(msg) if msg else {}
            except Exception:
                env = {}
            for a in (env.get("actions") or []):
                desc = a.get("descriptor", "")
                ps = _json.dumps(a.get("params", {}))
                low = ps.lower()
                flag = ""
                if nd and nd in low:
                    flag = "   <<<<< CARRIES THE VALUE WE SET"
                elif any(k in low for k in ("followup", "follow_up",
                                            "smfollowup")):
                    flag = "   <<< follow-up field"
                elif any(k in desc.lower() for k in ("save", "update",
                                                     "commit")):
                    flag = "   <<< save/update action"
                lines.append(f"descriptor: {desc}{flag}")
                cd = a.get("callingDescriptor", "")
                if cd:
                    lines.append(f"  callingDescriptor: {cd}")
                lines.append(f"  params: {ps[:900]}")
                lines.append("")
        try:
            path = USER_CONFIG_DIR / "flup_save_probe.txt"
            path.write_text("\n".join(lines), encoding="utf-8")
            self.on_status(f"  [flup] save capture → {path.name} "
                           f"({len(captured)} aura POST(s))")
        except Exception:
            pass

    def _persist_followup_diag(self, res: dict, kind: str) -> None:
        """When a Followup write fails to commit, dump the captured editor DOM
        to flup_probe.txt so the inline-edit save control can be identified."""
        if not (res and res.get("diag")):
            return
        try:
            import json
            from src.config import USER_CONFIG_DIR
            p = USER_CONFIG_DIR / "flup_probe.txt"
            p.write_text(
                f"=== followup {kind} commit failed ===\n"
                + json.dumps(res["diag"], indent=2, ensure_ascii=False),
                encoding="utf-8")
            res["diag_path"] = str(p)
        except Exception:
            pass

    def _scrape_all_task_status(self, ctx, expected_sids=None) -> dict:
        """Bulk '2a' scrape: scroll-load the whole live Caseload list, then
        read every task cell's pass/fail (the colour the CSV export drops),
        keyed by Student ID. Returns {"by_sid": {sid: {tnum: {...}}},
        "count": N} or {"error": msg}. Runs in the background after a refresh
        (App._maybe_bulk_scrape_task_status); the ~5-9s cost is the scroll-
        load, the read itself is ~instant.

        `expected_sids` (optional): the Student IDs known from the CSV to have a
        task. When supplied, the scroll-load stops the moment it has scrolled
        PAST all of them — we've already captured every task-bearer, so the
        trailing 'stable' passes are wasted work. Safe against under-reading:
        the first time a task-bearer's row is seen its cells are read, so
        'scrolled past all expected' implies 'captured all that have cells'."""
        want = {s for s in (expected_sids or []) if s}
        from src.student_lookup import scan_task_status_window
        t_start = time.perf_counter()   # timing: total + nav/setup vs loop
        # PHASE-1 API FAST PATH: the caseload page already fetched
        # getCaseLoadMainGridData (per-task pass/fail for the whole caseload).
        # If that capture is fresh + covers the expected task-bearers, read
        # pass/fail straight from it and skip the ~30-40s scroll-scrape. Any
        # doubt (stale / partial / parse miss) falls through to the proven
        # scrape below — never a regression.
        api = self._task_status_from_grid_api(want, t_start)
        if api is not None:
            return api
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return {"error": "caseload table didn't load"}
        # Clear any active row filter so we scroll the WHOLE caseload.
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click(); fi.fill(""); fi.press("Enter")
                _wait_grid_settled(target, 1500)
        except Exception:
            pass
        # Start at the top so the first window's rows aren't skipped.
        try:
            r0 = table.locator("tr")
            if r0.count() > 0:
                r0.nth(0).scroll_into_view_if_needed(timeout=2000)
                target.wait_for_timeout(250)
        except Exception:
            pass
        # Scroll-load + READ each window as we go, ACCUMULATING pass/fail by
        # Student ID. The list lazy-loads (and may virtualize) rows on scroll;
        # the old "scroll to the bottom, then read once" stopped as soon as the
        # row count held for two 400ms checks and read only what was rendered —
        # which intermittently truncated (e.g. 137 of 244 students). Reading
        # every window and stopping only after the row count is stable for
        # THREE passes captures rows before they recycle and survives a slow
        # lazy-load chunk.
        #
        # Each step is now ONE page.evaluate (read new rows + count + scroll,
        # via scan_task_status_window) that SKIPS rows whose Student ID we
        # already have — so each student's task cells are parsed exactly once
        # across the whole load, not re-scanned every pass (the O(n²) work that
        # pegged the machine). And we drop the browser's CPU priority for the
        # duration so this background pass doesn't starve the rest of the
        # machine; it's restored when the `with` block exits.
        by_sid: dict = {}
        seen_all: set = set()
        last_count, stable = -1, 0
        iters, stop_reason = 0, "max_iters"
        MAX_ITERS = 300
        # The browser priority/affinity drop was REMOVED from the scan: it ran
        # at IDLE priority confined to a 5-core slice, which STARVED the browser
        # so every scroll pass crawled (~5s/pass → 58s total). The lean-column
        # hide is what actually keeps the stutter down, so the scan now runs at
        # the browser's normal priority — ~10x faster.
        with self._lean_scan_columns(table, target) as lean:
            t_loop = time.perf_counter()   # nav/setup is everything before here
            for _ in range(MAX_ITERS):
                iters += 1
                # Yield to the user: this background scroll-load is the slow part
                # (~5-9s), and the worker is single-threaded, so a note/email/
                # text action fired mid-scrape would otherwise wait for the whole
                # thing. The moment any user command is queued, bail out — the
                # caller re-runs the scrape once the worker is free again.
                if not self.q.empty():
                    return {"interrupted": True, "lean": bool(lean),
                            "secs": round(time.perf_counter() - t_start, 1),
                            "setup_secs": round(t_loop - t_start, 1),
                            "loop_secs": round(time.perf_counter() - t_loop, 1)}
                # Read this window (skips already-seen Student IDs — the CPU win)
                try:
                    new_status, count, sids = scan_task_status_window(
                        table, by_sid.keys())
                except Exception:
                    new_status, count, sids = {}, last_count, []
                for sid, st in new_status.items():
                    by_sid.setdefault(sid, st)
                seen_all.update(sids)
                # Early stop: once we've scrolled past every task-bearer the CSV
                # told us about, we're done — skip the trailing 'stable' passes.
                if want and want <= seen_all:
                    stop_reason = "all_tasks_found"
                    break
                if count == last_count:
                    stable += 1
                    if stable >= 3:
                        stop_reason = "stable"
                        break
                else:
                    stable = 0
                last_count = count
                # Scroll to pull the next lazy-load chunk — the ORIGINAL proven
                # pair (container-to-bottom + the last row into view). The bare
                # JS scrollTop alone under-triggered Lightning's load.
                self._scroll_datatable_to_bottom(table)
                try:
                    rows = table.locator("tr")
                    rc = rows.count()
                    if rc > 0:
                        # 800ms (was 2000): this is just a backstop nudge after
                        # the container-to-bottom scroll above. With lean columns
                        # the last row is reachable fast, so a long timeout only
                        # wastes time on the passes where it would have stalled.
                        rows.nth(rc - 1).scroll_into_view_if_needed(timeout=800)
                except Exception:
                    pass
                try:
                    target.wait_for_timeout(400)
                except Exception:
                    pass
        return {"by_sid": by_sid, "count": len(by_sid),
                "rows": max(last_count, 0),
                "lean": bool(lean),
                "secs": round(time.perf_counter() - t_start, 1),
                "setup_secs": round(t_loop - t_start, 1),
                "loop_secs": round(time.perf_counter() - t_loop, 1),
                "iters": iters, "stop": stop_reason}

    MONGOOSE_DASHBOARD_URL = "https://sms.mongooseresearch.com/legacy-dashboard"

    def _open_mongoose(self, ctx, *, focus: bool = True) -> dict:
        """Open (or focus) the Mongoose texting dashboard in the launcher's OWN
        persistent context, so the probe / texting automation can see it.
        Reuses an existing mongoose tab if one is already open; otherwise spawns
        a fresh page and navigates. `focus` brings the tab to the foreground —
        pass focus=False at startup so it doesn't steal focus from the
        Salesforce login. Returns {ok, url} or {error}.

        NOTE: a USER-opened new tab while Salesforce is the main page hangs at
        about:blank (the known popup hang — see the startup TODO). A
        Playwright-driven new_page()+goto() does NOT, because the goto is the
        action that unsticks it. So the reliable time to spawn this is startup;
        afterwards this just focuses the already-open tab."""
        # Reuse an existing Mongoose tab if present.
        for page in ctx.pages:
            try:
                if not page.is_closed() and "mongoose" in (page.url or "").lower():
                    if focus:
                        try:
                            page.bring_to_front()
                        except Exception:
                            pass
                    return {"ok": True, "url": page.url or ""}
            except Exception:
                continue
        try:
            page = ctx.new_page()
            self._mongoose_warmed = False  # fresh tab — compose needs warming
            page.goto(self.MONGOOSE_DASHBOARD_URL, wait_until="domcontentloaded")
            if focus:
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            return {"ok": True, "url": page.url or ""}
        except Exception as e:
            return {"error": str(e)}

    def _mongoose_page(self, ctx):
        """Return the open Mongoose tab in the launcher's context, or None.
        (Do NOT use _active_page — it prefers the Salesforce tab.)"""
        for page in ctx.pages:
            try:
                if not page.is_closed() and "mongoose" in (page.url or "").lower():
                    return page
            except Exception:
                continue
        return None

    def _mongoose_login_check(self, ctx, surface: bool = True) -> dict:
        """Open Mongoose if needed and report whether it's signed in. When
        `surface` (the fire-time check), open with focus + bring the window
        forward if not signed in so the user can log in. When not (the startup
        heads-up), open in the background and don't steal focus. Returns
        {ok: bool, error?}."""
        page = self._mongoose_page(ctx)
        if page is None:
            res = self._open_mongoose(ctx, focus=surface)
            if res.get("error"):
                return {"ok": False, "error": res["error"]}
            page = self._mongoose_page(ctx)
            if page is None:
                return {"ok": False, "error": "Mongoose didn't open."}
            try:
                page.wait_for_timeout(1500)  # let SSO / the SPA settle
            except Exception:
                pass
        from src import text_message as tm
        logged_in = tm.mongoose_logged_in(page)
        if not logged_in and surface:
            try:
                self._bring_browser_forward(page)
            except Exception:
                pass
        return {"ok": logged_in}

    def _salesforce_login_check(self, ctx, surface: bool = True) -> dict:
        """Report whether the Salesforce session looks alive, WITHOUT
        navigating (so an open record / batch position isn't disturbed) —
        mirrors _mongoose_login_check. A live Lightning page (the caseload or
        an open record) means the session is good; if SSO has bounced it to a
        sign-in page there's no live Lightning page, so we treat that as logged
        out, bring the browser forward (when `surface`) and report it. Lets a
        fire abort BEFORE sending texts/emails instead of discovering the
        logout at note-filing time. Returns {ok: bool}."""
        live_sf = False
        login_page = None
        for page in list(ctx.pages):
            try:
                if page.is_closed():
                    continue
                url = (page.url or "").lower()
            except Exception:
                continue
            if "lightning.force.com" in url or "caseload_app_page" in url:
                live_sf = True
            elif self._looks_like_login(page):
                login_page = page
        if live_sf:
            return {"ok": True}
        if surface:
            try:
                if login_page is not None:
                    self._bring_browser_forward(login_page)
                else:
                    self._raise_browser_window()
            except Exception:
                pass
        return {"ok": False}

    def _arm_text_send_capture(self, page, sink: list):
        """Attach temporary request+response listeners to the Mongoose page,
        recording non-GET / XHR-fetch traffic (method, url, auth-relevant
        headers, post body, response status) into `sink` — discovery for a
        send/schedule API replay. Returns a stop() that detaches. Best-effort."""
        def _interesting(method, rtype):
            return (method or "GET").upper() != "GET" or rtype in ("xhr", "fetch")

        def _on_req(req):
            try:
                if not _interesting(req.method, req.resource_type):
                    return
                h = {}
                for k, v in (req.headers or {}).items():
                    lk = k.lower()
                    if lk in ("authorization", "x-csrf-token", "x-xsrf-token",
                              "x-requested-with", "content-type"):
                        h[lk] = v
                    elif lk == "cookie":
                        h["cookie"] = f"(present, {len(v)} chars)"
                sink.append({
                    "kind": "req", "method": (req.method or ""),
                    "url": (req.url or ""), "rtype": (req.resource_type or ""),
                    "headers": h, "post_data": (req.post_data or "")[:4000],
                })
            except Exception:
                pass

        def _on_resp(resp):
            try:
                rq = resp.request
                if not _interesting(rq.method, rq.resource_type):
                    return
                rec = {"kind": "resp", "status": resp.status,
                       "url": (resp.url or "")}
                # KEEP the Response object and read its body at DUMP time — the
                # sync Playwright API can't read a body inside this event
                # callback (it returns empty / "unavailable").
                if "sms-api.mongooseresearch.com/api/" in (resp.url or ""):
                    rec["_resp"] = resp
                sink.append(rec)
            except Exception:
                pass
        try:
            page.on("request", _on_req)
            page.on("response", _on_resp)
        except Exception:
            return lambda: None

        def _stop():
            try: page.remove_listener("request", _on_req)
            except Exception: pass
            try: page.remove_listener("response", _on_resp)
            except Exception: pass
        return _stop

    def _dump_text_send_capture(self, sink: list, needle: str):
        """Write captured Mongoose send traffic to text_send_probe.txt, flagging
        the request whose body carries the message (the send endpoint) and
        including the sms-api RESPONSE bodies. Bearer JWTs are REDACTED so the
        file is safe to share. Returns the path str, or None."""
        if not sink:
            return None
        from src.config import USER_CONFIG_DIR
        import urllib.parse as _up
        import re as _re

        def _redact(s):
            # Any JWT (three base64url segments starting eyJ…) → placeholder.
            return _re.sub(
                r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}",
                "<JWT redacted>", str(s or ""))

        frag = (needle or "").strip().lower()[:24]
        reqs = [e for e in sink if e.get("kind") == "req"]
        resps = [e for e in sink if e.get("kind") == "resp"]
        status_by_url = {e.get("url"): e.get("status") for e in resps}
        lines = [
            "=== Mongoose text-send network capture ===",
            f"message needle: {needle!r}",
            f"{len(reqs)} request(s), {len(resps)} response(s) captured.",
            "Bearer JWTs REDACTED — safe to share (student ids/phones may remain).",
            "", "--- distinct endpoints ---",
        ]
        seen = []
        for e in reqs:
            try:
                u = _up.urlparse(e["url"])
                key = f'{e["method"]:6} {u.netloc}{u.path}'
            except Exception:
                key = f'{e.get("method", "")} {e.get("url", "")}'
            if key not in seen:
                seen.append(key)
        lines += [f"  {k}" for k in seen]
        lines += ["", "--- requests ---"]
        for e in reqs:
            body = e.get("post_data", "") or ""
            flag = ("   <<<<< CARRIES THE MESSAGE — the SEND endpoint"
                    if frag and frag in body.lower() else "")
            st = status_by_url.get(e["url"])
            lines.append(f'{e["method"]} {e["url"]}   [{e.get("rtype", "")}]'
                         + (f'  -> {st}' if st is not None else '') + flag)
            for hk, hv in (e.get("headers") or {}).items():
                lines.append(f'    {hk}: {_redact(hv)}')
            if body:
                lines.append(f'    body: {_redact(body)[:1800]}')
            lines.append("")
        # sms-api RESPONSE bodies — read NOW (outside the event callback) so the
        # body is actually available. Dedupe by URL; focus on the endpoints that
        # carry the maps we need (search → numeric id, inbox + messageType maps).
        KEY = ("students/search", "groupaccounts", "messagetypes", "profile",
               "staff", "smartmessages", "customfields", "templates")
        seen_urls, body_lines = set(), []
        for e in resps:
            r = e.get("_resp")
            url = e.get("url", "")
            if r is None or url in seen_urls:
                continue
            if not any(k in url.lower() for k in KEY):
                continue
            seen_urls.add(url)
            try:
                body = r.text()
            except Exception as ex:
                body = f"(unreadable: {ex})"
            body_lines += [f'{e.get("status")}  {url}',
                           f'    {_redact(body)[:3000]}', ""]
        if body_lines:
            lines += ["--- sms-api response bodies (key endpoints) ---"]
            lines += body_lines
        try:
            path = USER_CONFIG_DIR / "text_send_probe.txt"
            path.write_text("\n".join(lines), encoding="utf-8")
            return str(path)
        except Exception:
            return None

    def _dom_payload_to_api(self, payload) -> dict:
        """Map a compose-modal text payload to the API send payload. Builds the
        team-local 'YYYY-MM-DDTHH:mm' scheduleDate from the slot dict (date_str
        is MM/DD/YYYY + hour12/ampm/minute); recipients carry over as the search
        terms (Contact id or mobile), resolved to Mongoose ids in the API send."""
        sch = payload.get("schedule") or {}
        schedule_date = None
        try:
            ds = (sch.get("date_str") or "").strip()   # MM/DD/YYYY
            if ds:
                mm, dd, yyyy = ds.split("/")
                h12 = int(sch.get("hour12", 10) or 10)
                mnt = int(sch.get("minute", 0) or 0)
                ap = (sch.get("ampm", "AM") or "AM").upper()
                h24 = (h12 % 12) + (12 if ap == "PM" else 0)
                schedule_date = (f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
                                 f"T{h24:02d}:{mnt:02d}")
        except Exception:
            schedule_date = None
        return {
            "course": payload.get("course", ""),
            "text": payload.get("body", ""),
            "contact_ids": list(payload.get("recipients") or []),
            "schedule_date": schedule_date,
            "schedule_name": (payload.get("schedule_name")
                              or payload.get("inbox_label") or "API send"),
        }

    def _send_text(self, ctx, payload: dict) -> dict:
        """Drive the Mongoose compose modal from a fired text action. `payload`
        carries the rendered body, recipient mobiles, inbox label, an optional
        schedule-slot dict, the schedule name, and the commit flag. Returns
        {ok} or {error}."""
        page = self._mongoose_page(ctx)
        if page is None:
            # Auto-open Mongoose in-context if it isn't open yet. (Reliable
            # mid-session — by now the caseload load has cleared the
            # about:blank popup hang.)
            self.on_status("Opening Mongoose…")
            res = self._open_mongoose(ctx, focus=True)
            if res.get("error"):
                return {"error": f"couldn't open Mongoose: {res['error']}"}
            page = self._mongoose_page(ctx)
            if page is None:
                return {"error": "Mongoose didn't open."}
            # Let SSO / the SPA settle before composing.
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        from src import text_message as tm
        # Fail fast if the Mongoose session has expired (the page is the SSO /
        # login screen, not the dashboard). Otherwise switch_department would
        # just time out 10s per group. Bring the window forward so the user can
        # sign in, and signal "not logged in" so the caller aborts cleanly
        # (incl. a combined action's email/note loop).
        if not tm.mongoose_logged_in(page):
            try:
                self._bring_browser_forward(page)
            except Exception:
                pass
            return {"error": "Mongoose isn't logged in (the browser is showing "
                    "the sign-in page). Sign in to Mongoose, then re-fire.",
                    "not_logged_in": True}
        # API path (opt-in): replay the send endpoint instead of driving the
        # compose modal — no warm-up / bring-to-front needed. On any hard error
        # it falls through to the modal below, so texting never breaks. A clean
        # "nobody opted in" result (ok, 0 sent) is NOT a fallback.
        if (self.text_api_enabled and payload.get("commit")
                and not (self._mongoose_token or {}).get("token")):
            # No token yet (e.g. first send of the session, browser minimized) —
            # harvest one so we use the reliable API path, not the flaky modal.
            self.on_status("  text: harvesting Mongoose API token…")
            self._ensure_mongoose_token(page)
        if (self.text_api_enabled and payload.get("commit")
                and (self._mongoose_token or {}).get("token")):
            api = self._send_text_via_api(ctx, self._dom_payload_to_api(payload))
            if api.get("ok"):
                sent = api.get("sent", 0)
                skipped = api.get("skipped") or []
                self.on_status(
                    f"  text: scheduled via API — {sent} recipient(s)"
                    + (f"; {len(skipped)} skipped (not opted in / not in "
                       "Mongoose)" if skipped else "") + ".")
                return {"ok": True, "via": "api", "sent": sent,
                        "skipped_recipients": skipped}
            self.on_status(
                f"  text: API send didn't apply ({api.get('error')}) — falling "
                "back to the compose modal.")
        # The first compose of a session is flaky (cold renderer); warm it once
        # with a throwaway open/search/close so the first real group goes through.
        if not self._mongoose_warmed:
            try:
                tm.warm_up_compose(page)
            except Exception:
                pass
            self._mongoose_warmed = True
        sch = payload.get("schedule")
        slot = None
        if sch:
            slot = tm.ScheduleSlot(
                team_dt=None,
                date_str=sch.get("date_str", ""),
                hour12=int(sch.get("hour12", 10)),
                minute=int(sch.get("minute", 0)),
                ampm=sch.get("ampm", "AM"),
                student_local_str=sch.get("student_local_str", ""),
            )
        msg = tm.TextMessage(
            body=payload.get("body", ""),
            recipients_mobile=list(payload.get("recipients") or []),
            inbox_label=payload.get("inbox_label", ""),
            schedule=slot,
            schedule_name=payload.get("schedule_name", ""),
            course=payload.get("course", ""),
            commit=bool(payload.get("commit", False)),
        )
        try:
            tm.send_text(page, msg, on_status=self.on_status,
                         should_stop=self.stop_event.is_set,
                         emit_timing=bool(payload.get("emit_timing")))
            result = {"ok": True}
        except tm.TextAborted:
            # User hit STOP mid-compose — close the half-built modal so the next
            # action starts clean, and report the abort (NOT a failure).
            try:
                tm.close_compose(page)
            except Exception:
                pass
            result = {"aborted": True}
        except Exception as e:
            result = {"error": str(e)}
        # If a `textapi:` capture is armed, the persistent listener already
        # recorded this send — auto-dump it (flagged by the message body) so a
        # tool-driven send needs no separate `textapidump:`. Listener stays
        # attached for further (e.g. manual) sends.
        if self._text_capture_sink is not None:
            try:
                p = self._dump_text_send_capture(self._text_capture_sink,
                                                 payload.get("body", ""))
                if p:
                    self.on_status(f"  [textapi] send capture → {p}")
            except Exception:
                pass
        return result

    def _arm_text_capture_persistent(self, ctx) -> dict:
        """Attach the persistent Mongoose network recorder to the open Mongoose
        tab. Captures every send (tool-driven or manually composed) until
        dumped. Requires Mongoose already open. Returns {armed, error?}."""
        if self._text_capture_stop:
            try:
                self._text_capture_stop()
            except Exception:
                pass
            self._text_capture_stop = None
        page = self._mongoose_page(ctx)
        if page is None:
            self._text_capture_sink = None
            return {"armed": False,
                    "error": "Mongoose isn't open — open it (🐭 Mongoose) first."}
        sink: list = []
        self._text_capture_sink = sink
        self._text_capture_stop = self._arm_text_send_capture(page, sink)
        return {"armed": True}

    def _dump_text_capture_persistent(self, needle: str) -> dict:
        """Write the armed capture's recording to text_send_probe.txt, flagging
        the request carrying `needle` (the message text). Keeps recording."""
        sink = self._text_capture_sink
        if not sink:
            return {"path": None, "count": 0}
        path = self._dump_text_send_capture(sink, needle or "")
        n = sum(1 for e in sink if e.get("kind") == "req")
        return {"path": path, "count": n}

    def _export_segments(self, ctx, courses: list) -> dict:
        """Auto-export each course's Mongoose contacts segment to a CSV in the
        config dir (the launcher then joins them to the caseload for Contact
        ids). Opens Mongoose if needed. Returns {ok, exported:[...], errors:[...]}."""
        page = self._mongoose_page(ctx)
        if page is None:
            self.on_status("Opening Mongoose…")
            res = self._open_mongoose(ctx, focus=True)
            if res.get("error"):
                return {"error": f"couldn't open Mongoose: {res['error']}"}
            page = self._mongoose_page(ctx)
            if page is None:
                return {"error": "Mongoose didn't open."}
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        from src import text_message as tm
        from src.config import CASELOAD_CSV_PATH
        dest_dir = CASELOAD_CSV_PATH.parent
        exported, missing, errors = [], [], []
        for course in courses:
            try:
                dest = dest_dir / f"mongoose_{course}.csv"
                tm.export_segment_csv(
                    page, course, dest, on_status=self.on_status)
                exported.append(course)
            except tm.SegmentNotFound as e:
                # Setup gap, not a failure: the user must create this segment.
                missing.append({"course": e.course,
                                "segment_name": e.segment_name,
                                "available": e.available})
                self.on_status(f"  segment: none named {e.segment_name!r} "
                               f"in {course} yet.")
            except Exception as e:
                errors.append(f"{course}: {e}")
                self.on_status(f"  segment export failed [{course}]: {e}")
        return {"ok": True, "exported": exported, "missing": missing,
                "errors": errors}

    def _read_essential_actions(self, ctx) -> dict:
        """Read the active student's open Essential Actions for the
        fire-time attach dialog."""
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        from src.student_lookup import read_essential_actions
        try:
            return {"eas": read_essential_actions(target)}
        except Exception as e:
            return {"error": str(e)}

    def _read_ea_dashboard(self, ctx) -> dict:
        """Navigate to the Essential Actions dashboard, scroll-load + read
        all EAs, then return to the caseload list so subsequent finds/fires
        start from the right page."""
        from src.config import ESSENTIAL_ACTIONS_URL
        from src.student_lookup import (read_ea_dashboard_rows,
                                        ea_rows_from_records,
                                        ea_view_missing_student_id)
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        if not ESSENTIAL_ACTIONS_URL:
            return {"error": "ESSENTIAL_ACTIONS_URL not set"}
        err, rows, source = "", [], "scrape"
        view_missing_sid = False
        t0 = time.perf_counter()
        t_read = t0
        self._ea_data = None
        self._ea_capture_armed = True
        try:
            target.goto(ESSENTIAL_ACTIONS_URL, wait_until="domcontentloaded")
            # Prefer the JSON feed the page fetches (path 2) — independent of the
            # EA view's displayed columns. Wait briefly for it, else DOM-scrape.
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and not self._ea_data:
                target.wait_for_timeout(200)
            feed_n = len(self._ea_data) if self._ea_data else 0
            # Always log the feed outcome so the JSON-vs-scrape decision is never
            # ambiguous (and a MISSING [EA feed] line means stale code ran).
            if feed_n:
                sample = self._ea_data[0] if isinstance(
                    self._ea_data[0], dict) else {}
                c = sample.get("Contact__r") or {}
                self.on_status(
                    f"  [EA feed] captured {feed_n} record(s); Contact__r keys: "
                    f"{', '.join(list(c)[:10]) or 'none'}; "
                    f"StudentID__c={str(c.get('StudentID__c') or '')!r}")
            else:
                self.on_status(
                    "  [EA feed] not captured within the wait window.")
            if self._ea_data:
                rows = ea_rows_from_records(self._ea_data)
                source = "JSON"
                if not rows:
                    # The feed arrived but every record mapped to no usable row
                    # — almost always because Contact__r has no StudentID__c
                    # (the feed's field set follows the EA view's columns).
                    self.on_status(
                        "  [EA feed] produced 0 usable rows — falling back to "
                        "DOM scrape.")
            if not rows:                       # feed missing → proven scrape
                target.wait_for_timeout(400)
                rows = read_ea_dashboard_rows(target)
                source = "scrape"
                # If the scrape found nothing, tell "0 EAs" apart from "the EA
                # view dropped the Student ID column" (which the scrape keys on)
                # so the app can warn the user to re-add it for the fallback.
                if not rows:
                    view_missing_sid = ea_view_missing_student_id(target)
            t_read = time.perf_counter()   # before the nav back to caseload
        except Exception as e:
            err = str(e)
        finally:
            self._ea_capture_armed = False
        try:
            if CASELOAD_URL:
                target.goto(CASELOAD_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        if err:
            return {"error": err}
        return {"eas": rows, "source": source,
                "view_missing_sid": view_missing_sid,
                "secs": round(time.perf_counter() - t0, 1),
                "read_secs": round(t_read - t0, 1)}

    def _probe_ea_feed(self, ctx) -> dict:
        """DISCOVERY (EA JSON path 2): navigate to the Essential Actions
        dashboard with a response listener and record every /aura action it
        fetches, flagging the one whose returnValue looks like EA rows (Reason /
        Intervention / Event Progress / Student…). Writes ea_feed_probe.txt so we
        can build a JSON reader instead of scraping the table. Read-only."""
        from src.config import ESSENTIAL_ACTIONS_URL, USER_CONFIG_DIR
        import json as _json
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        captured = []

        def _cap(resp):
            try:
                if "/aura" in (resp.url or ""):
                    captured.append((resp.url, resp.text()))
            except Exception:
                pass

        target.on("response", _cap)
        try:
            target.goto(ESSENTIAL_ACTIONS_URL, wait_until="domcontentloaded")
            target.wait_for_timeout(5500)   # let the datatable's actions fire
            # nudge a scroll in case rows lazy-load on scroll
            try:
                target.mouse.wheel(0, 2000)
                target.wait_for_timeout(1500)
            except Exception:
                pass
        except Exception:
            pass
        try:
            target.remove_listener("response", _cap)
        except Exception:
            pass

        # The DATA rows are a list of dicts whose KEYS are EA field API names
        # (the earlier detector wrongly flagged the column SCHEMA, which only has
        # those names as string VALUES). Score each list-of-dicts by how many EA
        # field keys its records carry, and dump the structure of every data list
        # so the feed is unambiguous.
        EA_KEYS = {"EventProgress__c", "Intervention__c", "CourseCode__c",
                   "Contact__r", "VisibilityStartDate__c", "TermProgress__c",
                   "Student__c", "StudentID__c", "FollowUpDate__c"}

        def _struct(v):
            if isinstance(v, list):
                if v and isinstance(v[0], dict):
                    return f"list[{len(v)}] of dict; keys={list(v[0].keys())[:16]}"
                return f"list[{len(v)}]"
            if isinstance(v, dict):
                return f"dict; keys={list(v.keys())[:16]}"
            return type(v).__name__

        lines = [f"=== EA feed probe @ {ESSENTIAL_ACTIONS_URL} ===",
                 f"captured {len(captured)} /aura response(s)", "",
                 "--- data-bearing actions (returnValue that is a list/dict) ---"]
        best = None      # (score, list) — most EA-key-bearing list of records
        idx = 0
        for _url, body in captured:
            try:
                i = (body or "").find("{")
                env = _json.loads(body[i:]) if i >= 0 else {}
            except Exception:
                env = {}
            for a in (env.get("actions") or []):
                rv = a.get("returnValue")
                # candidate row lists: top-level list, or a list nested one deep
                cands = []
                if isinstance(rv, list):
                    cands.append(rv)
                elif isinstance(rv, dict):
                    for v in rv.values():
                        if isinstance(v, list):
                            cands.append(v)
                if not cands:
                    continue
                idx += 1
                lines.append(f"[{idx}] state={a.get('state')} :: {_struct(rv)}")
                for c in cands:
                    if c and isinstance(c[0], dict):
                        score = len(EA_KEYS & set(c[0].keys()))
                        if score >= 2 and (best is None or len(c) > len(best[1])
                                           or score > best[0]):
                            best = (score, c)
        if best:
            rec = best[1][0]
            lines += ["", f">>> EA DATA FEED: list of {len(best[1])} records, "
                      f"EA-key score {best[0]}", "",
                      "first record:", _json.dumps(rec, indent=1)[:5000]]
        else:
            lines += ["", "No list-of-records with EA field keys found — "
                      "structures listed above; paste them and I'll pick it."]
        path = USER_CONFIG_DIR / "ea_feed_probe.txt"
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass
        try:
            if CASELOAD_URL:
                target.goto(CASELOAD_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        return {"path": str(path), "captured": len(captured),
                "found": bool(best), "descriptor": best[1] if best else ""}

    def _probe_aura(self, ctx) -> dict:
        """PROBE (Aura-replay phase): on the active Salesforce Lightning page,
        run a battery of `$A` / aura-context expressions to learn whether the
        token + context needed to replay the `saveNoteCmpValues` Aura action are
        reachable from page JS. Read-only — evaluates JS, changes nothing.
        Writes the full result to aura_probe.txt and returns a short summary."""
        from src.config import USER_CONFIG_DIR
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        js = r'''() => {
          const out = {};
          const probe = (name, fn) => {
            try {
              const v = fn();
              if (v === undefined) out[name] = '<undefined>';
              else if (typeof v === 'string')
                out[name] = 'str(' + v.length + '): ' + v.slice(0, 800);
              else if (typeof v === 'object')
                out[name] = 'obj: ' + JSON.stringify(v).slice(0, 800);
              else out[name] = typeof v + ': ' + String(v);
            } catch (e) { out[name] = 'ERR: ' + ((e && e.message) || e); }
          };
          probe('typeof_$A', () => typeof window.$A);
          probe('getContext', () => typeof ($A && $A.getContext && $A.getContext()));
          probe('getToken()', () => $A.getContext().getToken());
          probe('encodeForServer()', () => $A.getContext().encodeForServer());
          probe('fwuid', () => $A.getContext().fwuid);
          probe('getMode()', () => $A.getContext().getMode());
          probe('getPathPrefix()', () => $A.getContext().getPathPrefix());
          probe('getApp()', () => $A.getContext().getApp());
          probe('typeof_clientService', () => typeof $A.clientService);
          probe('clientService._token', () => $A.clientService && $A.clientService._token);
          probe('clientService.token', () => $A.clientService && $A.clientService.token);
          probe('typeof_enqueueAction', () => typeof ($A && $A.enqueueAction));
          probe('meta_aura_token', () => { const m = document.querySelector('meta[name="aura.token"]'); return m && m.content; });
          probe('location', () => location.pathname + location.search);
          probe('href', () => location.href);
          return out;
        }'''
        try:
            res = target.evaluate(js)
        except Exception as e:
            return {"error": f"evaluate failed: {e}"}
        lines = [f"=== Aura probe @ {target.url} ===", ""]
        for k, v in (res or {}).items():
            lines.append(f"{k}: {v}")
        path = USER_CONFIG_DIR / "aura_probe.txt"
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass
        tok = str((res or {}).get("getToken()", ""))
        ctxs = str((res or {}).get("encodeForServer()", ""))
        reachable = tok.startswith("str(") and ctxs.startswith("str(")
        return {"path": str(path), "reachable": reachable,
                "has_aura": str((res or {}).get("typeof_$A", "")),
                "token": tok[:70], "context": ctxs[:70]}

    def _probe_unlock_action(self, ctx) -> dict:
        """TEMP probe for the task-unlock feature. Does NOT navigate — run it
        on the EA dashboard with the 'Approve or Reject' popup already OPEN.
        Dumps (A) the open modal's tabs / buttons / inputs / headings, and (B)
        the leading clickable icons in the EA rows (to find the silhouette
        selector). Writes unlock_probe.txt."""
        from src.config import USER_CONFIG_DIR
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        lines: list[str] = []

        def cap(s=""):
            lines.append(str(s))

        cap("=== task-unlock probe ===")
        try:
            cap(f"URL: {target.url}")
        except Exception:
            pass

        # A) Any OPEN modal/dialog (shadow-pierced).
        try:
            modal = target.evaluate(
                """() => {
                  const out = {found:false, tabs:[], buttons:[], inputs:[], headings:[]};
                  const roots = [];
                  const findD = (root) => {
                    for (const d of root.querySelectorAll('[role="dialog"], section.slds-modal__container, .slds-modal')) {
                      roots.push(d); out.found = true;
                    }
                    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) findD(el.shadowRoot);
                  };
                  findD(document);
                  const scan = (root) => {
                    for (const t of root.querySelectorAll('[role="tab"], a.slds-tabs_default__link, li.slds-tabs_default__item')) {
                      const tx=(t.textContent||'').trim(); if(tx) out.tabs.push(tx.slice(0,60));
                    }
                    for (const b of root.querySelectorAll('button')) {
                      const tx=((b.textContent||'').trim())||b.getAttribute('title')||b.getAttribute('aria-label')||'';
                      if(tx) out.buttons.push({text:tx.slice(0,50), cls:(b.className||'').toString().slice(0,90)});
                    }
                    for (const i of root.querySelectorAll('input,textarea,select')) {
                      out.inputs.push({tag:i.tagName.toLowerCase(), type:i.getAttribute('type')||'', ph:i.getAttribute('placeholder')||'', aria:i.getAttribute('aria-label')||''});
                    }
                    for (const h of root.querySelectorAll('h1,h2,h3,legend,[role="heading"]')) {
                      const tx=(h.textContent||'').trim(); if(tx) out.headings.push(tx.slice(0,80));
                    }
                    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) scan(el.shadowRoot);
                  };
                  for (const r of roots) scan(r);
                  out.tabs=[...new Set(out.tabs)]; out.headings=[...new Set(out.headings)];
                  return out;
                }""")
        except Exception as e:
            modal = {}
            cap(f"modal scan error: {e}")
        cap(f"\n-- open modal/dialog: found={modal.get('found')} --")
        cap(f"tabs: {modal.get('tabs')}")
        cap(f"headings: {modal.get('headings')}")
        cap("buttons:")
        for b in (modal.get("buttons") or [])[:30]:
            cap(f"  '{b.get('text')}'   cls={b.get('cls')}")
        cap("inputs:")
        for i in (modal.get("inputs") or [])[:20]:
            cap(f"  <{i.get('tag')}> type={i.get('type')} ph={i.get('ph')!r} aria={i.get('aria')!r}")

        # B) EA rows: leading clickable icons (silhouette candidates).
        try:
            rows = target.evaluate(
                """() => {
                  const out = [];
                  const trs = [...document.querySelectorAll('tr')].filter(
                    tr => tr.querySelector('td[data-label="Student ID"]')).slice(0,3);
                  for (const tr of trs) {
                    const c = tr.querySelector('td[data-label="Student ID"]');
                    const sid = c ? (c.textContent||'').trim() : '';
                    const els = [];
                    for (const el of tr.querySelectorAll('button, a, lightning-icon, lightning-primitive-icon, [role="button"], svg')) {
                      let icon='';
                      const u = el.querySelector ? el.querySelector('use') : null;
                      if (u) icon = u.getAttribute('xlink:href') || u.getAttribute('href') || '';
                      els.push({tag:el.tagName.toLowerCase(), iconName:el.getAttribute('icon-name')||'', icon:icon, title:el.getAttribute('title')||'', aria:el.getAttribute('aria-label')||'', cls:(el.className||'').toString().slice(0,70)});
                    }
                    out.push({sid:sid, n:els.length, els: els.slice(0,18)});
                  }
                  return out;
                }""")
        except Exception as e:
            rows = []
            cap(f"row-icon scan error: {e}")
        cap("\n-- EA row leading clickables (silhouette candidates) --")
        for r in (rows or []):
            cap(f"row sid={r.get('sid')!r}  ({r.get('n')} clickable els):")
            for el in r.get("els", []):
                cap(f"  <{el.get('tag')}> icon-name={el.get('iconName')!r} "
                    f"icon={el.get('icon')!r} title={el.get('title')!r} "
                    f"aria={el.get('aria')!r} cls={el.get('cls')!r}")

        report = "\n".join(lines)
        path = USER_CONFIG_DIR / "unlock_probe.txt"
        try:
            path.write_text(report, encoding="utf-8")
        except Exception:
            pass
        return {"path": str(path), "modal_found": bool(modal.get("found"))}

    def _handle_run(
        self, ctx, scenario: ScenarioConfig, override: str,
        clipboard: str = "",
        custom_bodies: Optional[dict[int, str]] = None,
        prompt_vars: Optional[dict[str, str]] = None,
        ea: Optional[tuple] = None,
    ) -> bool:
        """Return True iff the note ran without errors (regardless of
        whether all sub-notes were auto-submitted). The batch driver
        uses the return value to track processed-vs-skipped honestly."""
        target = self._active_page(ctx)
        if target is None:
            self.on_status("No browser pages open.")
            return False
        # Note: when scenario.find_first is True, the main thread has
        # already driven the LIST_MATCHES + CLICK_MATCH sequence before
        # queueing this RUN — so by the time we get here, the active
        # student is already loaded.
        # Always try to capture student name — used for auto-detect and
        # for the session log entry on success. Defensive try/except:
        # the page can race-die between _active_page's liveness check
        # and the locator query (especially right after the auto-
        # download download-tab closed). Treat any failure as "no
        # student visible" rather than crashing the run.
        from src import selectors
        # The note panel's PRESENCE is the note BODY EDITOR (the field
        # fill_note needs) — NOT the student-name header, which doesn't parse
        # on the standalone Contact record view (where off-caseload students
        # opened via Salesforce global search land). So gate on the editor,
        # and read the name only best-effort (for logging + caseload lookup).
        def _panel_ready() -> bool:
            try:
                loc = selectors.note_body_editor(target)
                return loc.count() > 0 and loc.first.is_visible()
            except Exception:
                return False

        try:
            student = get_active_student_name(target)
        except Exception:
            student = None
        if not _panel_ready() and not student:
            # No note panel here. If we're on a Contact record (e.g. the user
            # navigated to an off-caseload student via Salesforce global
            # search), re-ground via the deep-link, which reloads the record
            # and polls until the note panel renders — the same reliable path
            # caseload notes use.
            cid = ""
            try:
                m = re.search(r"/Contact/(003[0-9A-Za-z]{12,15})",
                              target.url or "")
                cid = m.group(1) if m else ""
            except Exception:
                cid = ""
            if cid and self._navigate_to_contact(ctx, cid):
                target = self._active_page(ctx) or target
                try:
                    student = get_active_student_name(target)
                except Exception:
                    student = None
        if not _panel_ready():
            self.on_status(
                "No note panel open. Either select a student in the viewer and "
                "fire from there, or open a student's New Note panel in "
                "Salesforce first — then fire again.")
            return False
        # Look up the Caseload row once: gets course code, student ID,
        # and email in a single pass. Tolerates the same kind of
        # transient page-state error — fall back to empty info.
        try:
            info = lookup_caseload_student(target, student) if student else {}
        except Exception:
            info = {}
        if override:
            course_code = override
            self.on_status(f"Using course code (manual): {course_code}")
            if student:
                self.on_status(f"Active student: {student}")
        else:
            if student:
                self.on_status(f"Active student: {student}")
            detected = info.get("course_code", "")
            if detected:
                course_code = detected
                self.on_status(f"Auto-detected course code: {course_code}")
            elif any((getattr(n, "course_code_override", "") or "").strip()
                     for n in scenario.notes):
                # Off-caseload student (no caseload row → no auto-detect). Fall
                # back to the per-note course code(s) set in the fire-time edit
                # dialog or the action config. run_scenario applies
                # note.course_code_override per note, so a course is still
                # available even though we couldn't detect one here.
                course_code = ""
                self.on_status(
                    "No caseload row for this student — using the per-note "
                    "course code(s).")
            else:
                self.on_status(
                    f"Could not auto-detect for {student}. Type a code in the "
                    "course field (or set it in the fire-time note dialog).")
                return False
        # No caseload row matched for this fire (e.g. a student opened via
        # Salesforce global search who's assigned to another instructor).
        # Essential Actions are keyed to YOUR assigned caseload, so they aren't
        # shown/offered here — make that explicit so an empty EA offer doesn't
        # look like a bug. The note itself still files normally.
        if not info and ea is None:
            self.on_status(
                "ℹ Essential Actions aren't shown for students outside your "
                "assigned caseload — filing the note(s) only.")
        # Essential-Action path: open the note form via the EA's row action
        # ("Add Note to EA" / "& Close EA") so the note is tied to the EA.
        # run_scenario then fills that form just like the embedded panel.
        if ea:
            from src.student_lookup import open_ea_note_form
            ea_reason, ea_course, ea_close = ea
            self.on_status(
                f"Opening Essential Action note form: {ea_reason!r}"
                f"{' (& close)' if ea_close else ''}…")
            try:
                opened = open_ea_note_form(target, ea_reason, ea_course, ea_close)
            except Exception as e:
                opened = False
                self.on_status(f"EA note form error: {e}")
            if not opened:
                self.on_status(
                    f"Couldn't open the Essential Action note form for "
                    f"{ea_reason!r}. Note not filed.")
                return False
            # Small settle; fill_note then waits for the form's Submit
            # button to be visible, so no long blind sleep is needed.
            target.wait_for_timeout(200)
        # Resolve the SF Contact id (003…) for the API note-save path: prefer
        # the record URL we're grounded on (it IS the student), else the grid's
        # Student→Contact map. Empty → the API path is simply skipped (form used).
        api_contact_id = ""
        try:
            m = re.search(r"/Contact/(003[0-9A-Za-z]{12,15})", target.url or "")
            api_contact_id = m.group(1) if m else ""
        except Exception:
            api_contact_id = ""
        if not api_contact_id:
            sid = (info.get("student_id") or "").strip()
            if sid:
                try:
                    api_contact_id = (
                        self.grid_student_contact_map().get(sid) or "").strip()
                except Exception:
                    api_contact_id = ""

        def _api_save_note(note, idx: int) -> bool:
            """Try to file `note` via Salesforce's note-save endpoint (skips the
            form's cold-start Academic-Activity gate). Returns True if filed,
            False to fall back to the on-page form. Eligible only when: the
            opt-in is on, we have creds + a Contact id, the note auto-submits
            (the API commits immediately, so an unsubmitted note must stay in
            the form), it has an explicit note type, and it isn't EA-attached
            (EA junctions aren't handled here yet)."""
            if not getattr(self, "note_api_enabled", True):
                return False
            if ea is not None:
                return False
            if not getattr(note, "submit", False):
                return False
            if not (note.interaction_type or "").strip():
                return False
            if not api_contact_id or not self._aura_creds:
                return False
            res = self._save_note_via_api(
                ctx, contact_id=api_contact_id,
                note_type=note.interaction_type,
                course_code=note.course_code,
                subject=note.subject,
                body_html=note_body_to_html(note.body),
                activities=note.academic_activities,
            )
            if res.get("ok"):
                self.on_status(
                    f"  ✓ Note {idx + 1} filed via Salesforce API "
                    f"(no form — bypasses the Academic-Activity gate).")
                return True
            self.on_status(
                f"  Note {idx + 1}: API save unavailable "
                f"({res.get('error', '?')}) — using the form.")
            return False

        self.on_status(f"Running {scenario.name!r}...")
        try:
            all_submitted = run_scenario(
                target, scenario, course_code,
                clipboard=clipboard, custom_bodies=custom_bodies,
                prompt_vars=prompt_vars,
                on_status=self.on_status,
                api_save=_api_save_note,
            )
            tail = "" if all_submitted else "  (left open — submit unchecked)"
            self.on_status(f"Done: {scenario.name!r} (course {course_code!r}).{tail}")
            self.on_note_filed(NoteLogEntry(
                timestamp=datetime.now(),
                scenario=scenario.name,
                course_code=course_code,
                student=student or "(unknown)",
                student_id=info.get("student_id", ""),
                student_email=info.get("student_email", ""),
                pm_name=info.get("pm_name", ""),
                pm_email=info.get("pm_email", ""),
                submitted=all_submitted,
            ))
            return True
        except RuntimeError as e:
            self.on_status(f"Failed: {e}")
            return False
