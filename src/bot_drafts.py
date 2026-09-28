"""Phase B — reading the bot's reply drafts (pure domain, no GUI).

The chatbot pipeline (a separate repo, running on the bot backend
machine) drafts reply emails for routine course questions and files them
in an Outlook folder, each carrying everything this thin review client
needs:

  - machine-readable ``Bot*`` UserProperties (course, message key,
    action, best retrieval similarity, topic, schema version) — so we can
    identify / group / gate / grade a draft without any sidecar file;
  - the student-facing body fenced with invisible HTML-comment markers so
    we can strip the internal reviewer block (always, before any send)
    and the automated-reply banner (only when a reviewer edits and sends
    "as me", so the reply reads in the faculty member's own voice).

This module is the reader/parser/stripper. It has NO Outlook or Tk state
of its own beyond a thin ``load_pending`` that walks a folder — the panel
(``bot_drafts_panel``) drives it and owns the UI. Everything the tests
touch is pure string work.

Contract note: the fence markers + ``Bot*`` property names below MUST
match what the chatbot's ``phase7_email_agent.py`` writes (its
``BOT_DRAFT_SCHEMA``). If that schema is bumped, mirror it here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# --- the chatbot review-client contract (mirror of phase7_email_agent) -----
BOT_DRAFT_SCHEMA = "1"
BANNER_MARK_START = "<!--BOT_BANNER-->"
BANNER_MARK_END = "<!--/BOT_BANNER-->"
INTERNAL_MARK_START = "<!--BOT_INTERNAL-->"
INTERNAL_MARK_END = "<!--/BOT_INTERNAL-->"
BODY_MARK_START = "<!--BOT_BODY-->"
BODY_MARK_END = "<!--/BOT_BODY-->"

_ALL_MARKS = (
    INTERNAL_MARK_START, INTERNAL_MARK_END, BANNER_MARK_START,
    BANNER_MARK_END, BODY_MARK_START, BODY_MARK_END,
)


def _region_re(start: str, end: str) -> "re.Pattern":
    return re.compile(re.escape(start) + r"(.*?)" + re.escape(end), re.DOTALL)


_INTERNAL_RE = _region_re(INTERNAL_MARK_START, INTERNAL_MARK_END)
_BANNER_RE = _region_re(BANNER_MARK_START, BANNER_MARK_END)
_BODY_RE = _region_re(BODY_MARK_START, BODY_MARK_END)

# The Bot* UserProperties we read off a draft item.
BOT_PROP_NAMES = (
    "BotCourse", "BotMessageKey", "BotAction", "BotBestSim", "BotTopic",
    "BotStudentEmail", "BotQuestion", "BotVersion",
)

# A UI hint only — mirrors the chatbot's GOOD_MATCH_SIM (phase5_3_bot):
# below this the bot itself flags low retrieval confidence in the draft.
LOW_CONF_THRESHOLD = 0.55

# Default Drafts subfolder the bot files into (matches the email agent's
# --draft-folder "BOT drafts"). Used when the setting is left empty.
DEFAULT_WATCH_FOLDER = "BOT drafts"
OL_FOLDER_DRAFTS = 16  # olFolderDrafts


# --- body stripping (pure) -------------------------------------------------

def strip_internal(html: str) -> str:
    """`html` with the internal reviewer block removed. Applied before ANY
    send — nothing inside the fence is ever meant for the student."""
    return _INTERNAL_RE.sub("", html or "")


def strip_banner(html: str) -> str:
    """`html` with the automated-reply banner removed — used only for a
    "send as me" (edited) reply, so it reads as the faculty member's own
    words rather than a labeled bot reply."""
    return _BANNER_RE.sub("", html or "")


def editable_body(draft_html: str) -> str:
    """The student-facing BODY region (answer + footer, or the ack text) —
    what the reviewer edits. '' if the draft carries no body fence (older
    draft): the panel then falls back to plain review-only."""
    m = _BODY_RE.search(draft_html or "")
    return m.group(1) if m else ""


def banner_html(draft_html: str) -> str:
    """The automated-reply banner region (inner), for the panel to show the
    reviewer what a 'send as bot' reply would be labeled with. '' if absent."""
    m = _BANNER_RE.search(draft_html or "")
    return m.group(1) if m else ""


def _strip_marks(html: str) -> str:
    """Remove the leftover comment fence markers themselves (their regions
    are handled separately), so nothing invisible-but-odd ships."""
    for mark in _ALL_MARKS:
        html = html.replace(mark, "")
    return html


def rebuild_for_send(draft_html: str, edited_body: Optional[str],
                     as_bot: bool) -> str:
    """The exact HTML to send.

    Internal block is ALWAYS removed. Banner stays only for ``as_bot``
    (unedited "Good / send"); for a "send as me" it's removed so the reply
    reads in the faculty member's own voice. When ``edited_body`` is given
    it replaces the fenced BODY region in place — so the banner (if kept),
    the quoted original, and everything else are preserved untouched.
    """
    out = strip_internal(draft_html)
    if not as_bot:
        out = strip_banner(out)
    if edited_body is not None:
        if _BODY_RE.search(out):
            out = _BODY_RE.sub(
                lambda _m: BODY_MARK_START + edited_body + BODY_MARK_END,
                out, count=1)
        else:  # older draft without a body fence: append the edit
            out = out + edited_body
    return _strip_marks(out)


def student_html(draft_html: str, as_bot: bool) -> str:
    """The unedited send body (delegates to rebuild_for_send)."""
    return rebuild_for_send(draft_html, None, as_bot)


def internal_block(draft_html: str) -> str:
    """The internal reviewer block (roster snapshot + notices), fences
    removed, for the panel's separate internal display. '' if absent."""
    m = _INTERNAL_RE.search(draft_html or "")
    if not m:
        return ""
    return m.group(0)[len(INTERNAL_MARK_START):-len(INTERNAL_MARK_END)]


def has_bot_markers(html: str) -> bool:
    """Whether `html` carries the fenced structure of a bot draft (a
    belt-and-braces filter beside the folder + UserProperties)."""
    return INTERNAL_MARK_START in (html or "") or BANNER_MARK_START in (
        html or "")


# --- the model -------------------------------------------------------------

@dataclass
class BotDraft:
    """One reviewable bot draft. ``item`` is the live Outlook MailItem
    (None in tests); ``entry_id`` / ``store_id`` reopen it deterministically
    for send/move."""
    entry_id: str
    store_id: str
    to: str
    subject: str
    course: str
    message_key: str
    action: str                       # answer_draft | ack_draft
    best_sim: Optional[float]
    topic: Optional[str]
    student_email: str                # the student's SMTP (for caseload lookup)
    question: str                     # the student's extracted question
    version: str
    html: str                         # full draft HTMLBody
    item: object = None

    # --- convenience views (delegate to the pure helpers) ---
    def student_body(self, as_bot: bool) -> str:
        return student_html(self.html, as_bot)

    def internal_body(self) -> str:
        return internal_block(self.html)

    @property
    def is_answer(self) -> bool:
        return self.action == "answer_draft"

    @property
    def low_confidence(self) -> bool:
        return self.best_sim is not None and self.best_sim < LOW_CONF_THRESHOLD

    @classmethod
    def from_parts(cls, entry_id: str, store_id: str, to: str, subject: str,
                   props: dict, html: str, item=None) -> "BotDraft":
        """Build from a `Bot*` property dict + body — the pure path the
        tests exercise (no COM)."""
        return cls(
            entry_id=entry_id or "",
            store_id=store_id or "",
            to=to or "",
            subject=subject or "",
            course=(props.get("BotCourse") or ""),
            message_key=(props.get("BotMessageKey") or ""),
            action=(props.get("BotAction") or ""),
            best_sim=_as_float(props.get("BotBestSim")),
            topic=(props.get("BotTopic") or None),
            student_email=(props.get("BotStudentEmail") or ""),
            question=(props.get("BotQuestion") or ""),
            version=(props.get("BotVersion") or ""),
            html=html or "",
            item=item,
        )


def _as_float(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_bot_draft(props: dict, html: str = "") -> bool:
    """A draft the bot produced — identified by its schema property (or,
    failing that, the body fences). Lets the loader ignore a reviewer's own
    half-written mail if it ever shares the folder."""
    if props.get("BotVersion") or props.get("BotCourse"):
        return True
    return has_bot_markers(html)


# --- Outlook read (thin COM wrapper; not unit-tested) ----------------------

def read_bot_props(item) -> dict:
    """The item's ``Bot*`` UserProperties as a plain dict. Best-effort — a
    property the item lacks is simply absent."""
    out: dict = {}
    try:
        ups = item.UserProperties
    except Exception:
        return out
    for name in BOT_PROP_NAMES:
        try:
            up = ups.Find(name)
            if up is not None and up.Value not in (None, ""):
                out[name] = str(up.Value)
        except Exception:
            continue
    return out


def load_pending(folder) -> list:
    """Read every bot draft in the Outlook `folder` (a COM MAPIFolder),
    newest first, as ``BotDraft`` objects. Non-bot items in the folder are
    skipped. The live MailItem is kept on each draft for send/move."""
    drafts: list = []
    items = folder.Items
    try:
        items.Sort("[LastModificationTime]", True)
    except Exception:
        pass
    it = items.GetFirst()
    while it is not None:
        try:
            html = getattr(it, "HTMLBody", "") or ""
            props = read_bot_props(it)
            if is_bot_draft(props, html):
                drafts.append(BotDraft.from_parts(
                    entry_id=getattr(it, "EntryID", "") or "",
                    store_id=_store_id(it),
                    to=getattr(it, "To", "") or "",
                    subject=getattr(it, "Subject", "") or "",
                    props=props,
                    html=html,
                    item=it,
                ))
        except Exception:
            pass
        it = items.GetNext()
    return drafts


def _store_id(item) -> str:
    try:
        return item.Parent.StoreID or ""
    except Exception:
        return ""


# --- settings / gating -----------------------------------------------------
# Tab visibility is gated ONLY on the enabled flag — a pure check that needs
# no Outlook at startup (course-agnostic: instructors without a bot-supported
# course simply leave it off). Whether the folder actually holds drafts is a
# panel-refresh concern (a count/empty-state), not a startup COM call.

def feature_enabled(settings) -> bool:
    """Whether the Bot Drafts tab should be shown for this user."""
    return bool(getattr(settings, "bot_drafts_enabled", False))


def watch_folder_name(settings) -> str:
    """The configured Drafts subfolder name, or the built-in default."""
    return (getattr(settings, "bot_drafts_folder", "") or "").strip() \
        or DEFAULT_WATCH_FOLDER


def watch_mailbox(settings) -> str:
    """The configured mailbox SMTP ('' = the personal mailbox)."""
    return (getattr(settings, "bot_drafts_mailbox", "") or "").strip()


def open_watch_folder(settings):
    """Open the configured Drafts subfolder (COM), or None if it can't be
    resolved (Outlook down, mailbox unresolvable, or the subfolder doesn't
    exist yet because no drafts have been filed). Personal mailbox is the
    common case; a shared mailbox is best-effort (later phase)."""
    import win32com.client

    try:
        ns = win32com.client.Dispatch(
            "Outlook.Application").GetNamespace("MAPI")
    except Exception:
        return None
    mbx = watch_mailbox(settings)
    try:
        if mbx:
            recip = ns.CreateRecipient(mbx)
            recip.Resolve()
            drafts = ns.GetSharedDefaultFolder(recip, OL_FOLDER_DRAFTS)
        else:
            drafts = ns.GetDefaultFolder(OL_FOLDER_DRAFTS)
    except Exception:
        return None
    want = watch_folder_name(settings).strip().lower()
    try:
        for f in drafts.Folders:
            if (f.Name or "").strip().lower() == want:
                return f
    except Exception:
        return None
    return None


def count_pending(settings) -> int:
    """Best-effort count of bot drafts in the watch folder — 0 on any COM
    error or missing folder. For the panel badge/empty state; NOT used to
    gate the tab (feature_enabled does that without touching Outlook)."""
    try:
        folder = open_watch_folder(settings)
        return len(load_pending(folder)) if folder is not None else 0
    except Exception:
        return 0


def reviewer_address() -> str:
    """The signed-in Outlook user's SMTP address ('' if unavailable) — where
    a 'send me a copy' review goes."""
    try:
        from src import outlook_email
        return (outlook_email.get_user_info() or {}).get("email", "") or ""
    except Exception:
        return ""


def tag_original_verdict(settings, message_key: str, category: str,
                         lookback_days: int = 45) -> bool:
    """Apply a verdict category (Bot: good / needed fixes / wrong) to the
    ORIGINAL student email — located by its internet message-id in the watch
    mailbox's Inbox — so the backend's --read-verdicts logs it to the
    scorecard. This is how an explicit grade reaches the feedback loop while
    the client still only ever touches Outlook. Best-effort; returns True if
    it found and tagged the original. Reuses outlook_inbox."""
    if not (message_key and category):
        return False
    try:
        from src import outlook_inbox
    except Exception:
        return False
    import datetime as dt
    since = dt.datetime.now() - dt.timedelta(days=lookback_days)
    try:
        inbox = outlook_inbox.open_inbox(watch_mailbox(settings))
    except Exception:
        return False
    try:
        for info, msg in outlook_inbox.iter_messages(inbox, since=since):
            if info.get("message_key") == message_key:
                outlook_inbox.apply_categories(msg, [category])
                return True
    except Exception:
        return False
    return False


# --- caseload resolution (for the default PM Cc) ---------------------------
# Pure: given the app's cached caseload rows + a student email, find the row
# and derive the Program-Mentor Cc. Mirrors launcher's _mailto CC logic so the
# panel CCs the PM the same way the rest of the app does.

def find_caseload_row(rows, email: str):
    """The caseload row whose student email matches `email`
    (case-insensitive), or None. `rows` is app._caseload_rows."""
    e = (email or "").strip().lower()
    if not e:
        return None
    from src import caseload_csv
    for r in rows or []:
        got = caseload_csv.first_present_value(
            r, caseload_csv.STUDENT_EMAIL_COLS).strip().lower()
        if got and got == e:
            return r
    return None


def pm_cc_for_row(row) -> str:
    """The Program-Mentor Cc for a caseload row: a real PM email if the row (or
    the note-log) has one, else the mentor's name as 'First Last' (Outlook
    resolves it against the directory). '' if none. Mirrors the launcher."""
    if not row:
        return ""
    from src import caseload_csv
    pm = caseload_csv.first_present_value(row, caseload_csv.PM_EMAIL_COLS)
    if not pm:
        pm = str(row.get("_pm_email_logged", "") or "").strip()
    if not pm:
        name = str(row.get("MentorName", "") or "").strip()
        if "," in name:  # "Last, First" -> "First Last" (comma isn't a sep)
            bits = [b.strip() for b in name.split(",", 1)]
            if len(bits) == 2 and all(bits):
                name = f"{bits[1]} {bits[0]}"
        pm = name
    return pm


def default_cc_for(rows, student_email: str) -> str:
    """Convenience: the PM Cc for a student email against the caseload rows."""
    return pm_cc_for_row(find_caseload_row(rows, student_email))
