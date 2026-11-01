"""Background inbox labeler — the in-app engine behind the Settings toggle.

Periodically scans the configured Outlook folder(s) and applies the
course + CI categories resolved by `inbox_triage` (identification) via
`outlook_inbox` (COM). The interactive dev/test harness for the same
pipeline is `scripts/label_inbox.py`; this module is what the launcher
runs on a daemon thread while the app is open.

Non-destructive: the only write is adding categories (never removes,
moves, edits, or sends). Apply policy (agreed with the user):
  - confident email matches → course + CI categories
  - unmatched senders       → ``Unidentified`` (never silence)
  - name-only matches       → nothing applied; logged for human review

State/log files are shared with the script harness so manual runs and
app runs never double-label:
  - inbox_label_state.json — processed-message keys
  - inbox_label_log.jsonl  — every decision (the accuracy-review artifact)

Threading: Outlook COM objects are apartment-threaded, so the labeler
thread calls `pythoncom.CoInitialize()` and creates its OWN Outlook
dispatch (via outlook_inbox) — it never shares COM objects with the UI
thread's email code.
"""
import datetime as dt
import json
import threading
from pathlib import Path
from typing import Callable, Optional

from src.config import USER_CONFIG_DIR
from src import inbox_triage

STATE_PATH = USER_CONFIG_DIR / "inbox_label_state.json"
LOG_PATH = USER_CONFIG_DIR / "inbox_label_log.jsonl"
ROSTER_PATH = USER_CONFIG_DIR / "coursescan_roster.json"

# Built-in defaults, used when the settings fields are blank. The team
# shared box (labeled at its top-level Inbox ONLY — per-CI subfolders are
# each member's own space) and the team's course list (user, 2026-09-01:
# C868 dropped — retired; D370 dropped — it has its own inbox, and its
# 2,066-student roster made scans slow for mail that never lands here).
DEFAULT_FOLDERS = ["UG Capstone IT"]
DEFAULT_COURSES = ["C769", "C964", "D342", "D502", "D424"]


def parse_items(text: str, default: list) -> list:
    """Comma/newline-separated settings text → clean list; blank → default."""
    items = [p.strip() for chunk in (text or "").splitlines()
             for p in chunk.split(",")]
    items = [p for p in items if p]
    return items or list(default)


def plan_categories(result: dict) -> list:
    """The apply policy: what actually gets written for a triage result.
    Name-only matches are review candidates, not labels — they get
    nothing (not even Unidentified, which would misfile a probable
    student)."""
    if result["method"] == inbox_triage.METHOD_NAME:
        return []
    return result["categories"]


def state_entry(resolved: bool, ts_iso: str) -> str:
    """Seen-state value: 'ok|<ts>' for a fully identified message,
    'U|<ts>' for one still carrying a placeholder (Unidentified /
    Unassigned / name-match) that a fresher roster might resolve."""
    return ("ok|" if resolved else "U|") + ts_iso


def parse_state_entry(value: str) -> tuple:
    """(resolved, labeled_ts_iso). Legacy plain-timestamp entries
    (written before self-correction existed) parse as UNresolved so
    they get one re-check against a newer roster, then rewrite in the
    new format."""
    v = value or ""
    if v.startswith("ok|"):
        return True, v[3:]
    if v.startswith("U|"):
        return False, v[2:]
    return False, v


def is_resolved(result: dict) -> bool:
    """Fully identified — nothing a fresher roster could improve."""
    return (result["method"] == inbox_triage.METHOD_EMAIL
            and inbox_triage.UNASSIGNED_CATEGORY not in result["categories"])


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=0)


def run_pass(*, folders: list, courses: list, lookback_days: float,
             apply: bool = True, roster_path: Optional[Path] = None) -> dict:
    """One labeling pass over `folders`. Returns a summary dict:
    ``{"scanned", "labeled", "identified", "review", "unidentified",
    "folder_errors": [str]}``.

    Messages already in the seen-state are skipped — EXCEPT ones that
    were left with a placeholder (Unidentified / Unassigned / name-match)
    and were labeled before the roster file's current timestamp: those
    are re-checked, and if the fresher roster now identifies them the
    real categories are applied and the labeler's own stale placeholder
    markers are removed (self-correction; human categories untouched).
    Every processed message is recorded in the state (in apply mode) and
    logged to the JSONL. Raises FileNotFoundError (actionable message)
    if the roster is missing — callers surface that to the user.
    """
    from src import outlook_inbox

    rp = Path(roster_path or ROSTER_PATH)
    if not rp.exists():
        raise FileNotFoundError(
            f"Inbox labeler: no roster at {rp.name} — run 'coursescan: "
            "capture' then 'coursescan: export' first.")
    roster = inbox_triage.load_roster(rp)
    roster_iso = dt.datetime.fromtimestamp(
        rp.stat().st_mtime).isoformat(timespec="seconds")
    # Learner360 team-roster overlay (current instructor assignments), read
    # READ-ONLY from history.db and ignored if stale/missing — the course
    # scan stays the base. A newer overlay counts as a fresher roster, so
    # placeholder ("Unassigned") messages are re-checked against it.
    overlay_stats = None
    try:
        from src import history, l360_roster
        team_rows, team_at = history.load_l360_team_roster()
        if team_rows:
            roster, overlay_stats = l360_roster.overlay(roster, team_rows,
                                                        courses)
            roster_iso = max(roster_iso, team_at[:19])
    except Exception:
        overlay_stats = None
    index = inbox_triage.build_index(roster, courses)

    since = dt.datetime.now() - dt.timedelta(days=lookback_days)
    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    state = load_state()
    summary = {"scanned": 0, "labeled": 0, "identified": 0,
               "review": 0, "unidentified": 0, "corrected": 0,
               "folder_errors": [], "l360_overlay": overlay_stats}

    with open(LOG_PATH, "a", encoding="utf-8") as log:
        for fpath in folders:
            try:
                folder = outlook_inbox.open_folder(fpath)
            except OSError as e:
                summary["folder_errors"].append(str(e))
                continue
            for info, msg in outlook_inbox.iter_messages(folder, since=since):
                key = info["message_key"]
                recheck = False
                if key and key in state:
                    resolved, labeled_ts = parse_state_entry(state[key])
                    # ISO timestamps compare lexicographically. Only a
                    # placeholder labeled BEFORE the current roster file
                    # is worth another look.
                    if resolved or labeled_ts >= roster_iso:
                        continue
                    recheck = True
                result = inbox_triage.triage(
                    index, info["sender_smtp"], info["sender_name"])
                summary["scanned"] += 1
                summary[{inbox_triage.METHOD_EMAIL: "identified",
                         inbox_triage.METHOD_NAME: "review",
                         inbox_triage.METHOD_NONE: "unidentified"}[
                    result["method"]]] += 1

                to_apply = plan_categories(result)
                remove = None
                if recheck and to_apply:
                    # Upgrading: strip our own stale placeholders (only
                    # ones not part of the new label set). Never touches
                    # course/CI/human categories.
                    remove = [c for c in (inbox_triage.UNIDENTIFIED_CATEGORY,
                                          inbox_triage.UNASSIGNED_CATEGORY)
                              if c not in to_apply]
                applied = False
                if apply and to_apply:
                    applied = outlook_inbox.apply_categories(
                        msg, to_apply, remove=remove)
                if applied:
                    summary["labeled"] += 1
                    if recheck and result["confident"]:
                        summary["corrected"] += 1
                if apply and key:
                    state[key] = state_entry(is_resolved(result), now_iso)

                log.write(json.dumps({
                    "ts": now_iso, "mailbox": fpath,
                    "message_key": key,
                    "received": str(info["received"] or ""),
                    "sender_name": info["sender_name"],
                    "sender_smtp": info["sender_smtp"],
                    "subject": info["subject"],
                    "method": result["method"],
                    "confident": result["confident"],
                    "categories": result["categories"],
                    "students": result["students"],
                    "note": result["note"],
                    "existing_categories": info["categories"],
                    "applied": applied, "dry_run": not apply,
                    "source": "app",
                }) + "\n")

    if apply:
        save_state(state)
    return summary


def summary_line(summary: dict) -> str:
    """One activity-log line for a pass that did something."""
    bits = [f"{summary['labeled']} labeled"]
    if summary.get("corrected"):
        bits.append(f"{summary['corrected']} corrected after roster update")
    if summary["identified"]:
        bits.append(f"{summary['identified']} identified")
    if summary["review"]:
        bits.append(f"{summary['review']} name-match (review)")
    if summary["unidentified"]:
        bits.append(f"{summary['unidentified']} unidentified")
    return "Inbox labeler: " + ", ".join(bits) + "."


class LabelerThread(threading.Thread):
    """Daemon polling thread. `get_params` is called before each pass and
    returns the kwargs for `run_pass` plus ``interval_min`` — or None to
    idle (feature toggled off without restarting the thread). `on_log`
    is called with (msg, level) where level is "info" | "ok" | "error";
    the launcher marshals it onto the UI thread.
    """

    def __init__(self, get_params: Callable[[], Optional[dict]],
                 on_log: Callable[[str, str], None]):
        super().__init__(daemon=True, name="inbox-labeler")
        self.get_params = get_params
        self.on_log = on_log
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()  # set() to run a pass NOW
        self._last_error = ""

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def run_now(self) -> None:
        """Ask the thread to run a pass immediately (Label now button)."""
        self.wake_event.set()

    def run(self) -> None:
        try:
            import pythoncom
        except ImportError:  # non-Windows dev box; nothing to do
            return
        pythoncom.CoInitialize()
        try:
            first = True
            while not self.stop_event.is_set():
                interval_min = 5
                params = None
                try:
                    params = self.get_params()
                except Exception:
                    pass
                if params:
                    interval_min = params.pop("interval_min", 5)
                    try:
                        s = run_pass(**params)
                        self._last_error = ""
                        if first:
                            self.on_log(
                                "Inbox labeler: catch-up pass — "
                                + summary_line(s)[len("Inbox labeler: "):],
                                "ok" if s["labeled"] else "info")
                        elif s["labeled"] or s["folder_errors"]:
                            self.on_log(summary_line(s), "ok")
                        for err in s["folder_errors"]:
                            if err != self._last_error:
                                self._last_error = err
                                self.on_log(err, "error")
                    except Exception as e:  # noqa: BLE001 — keep polling
                        msg = str(e)
                        if msg != self._last_error:
                            self._last_error = msg
                            self.on_log(msg, "error")
                    first = False
                self.wake_event.clear()
                self.wake_event.wait(max(60, int(interval_min) * 60))
        finally:
            pythoncom.CoUninitialize()
