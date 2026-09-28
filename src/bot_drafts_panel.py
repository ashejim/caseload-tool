"""The Bot Drafts tab — review AI-generated reply drafts, grade, edit, send.

Phase B, human-in-the-loop. The bot backend (a separate machine) files reply
drafts into an Outlook folder; a faculty member reviews each one here and
either sends it in their own voice (edited) or, unedited, sends it labeled as
an automated bot reply. Grading captures feedback; sending offers a note step.

Layout: list of drafts on the left; a banner/header bar across the top of the
detail; two columns — the student's question (read-only) | the editable bot
reply. Below the reply: GRADE buttons -> reveal Undo + Send options -> sending
reveals a NOTE step. The panel can **pop out** into its own resizable window
(the in-tab area is cramped), and the reading font is adjustable (A- / A+).

Fidelity: the in-app editor is an approximation — Outlook is the source of
truth for how the sent mail (and its signature) actually renders, so a
"Preview in Outlook" button opens the real draft. All parsing / stripping /
reassembly is in src.bot_drafts (pure, tested); this module is only the widget
plus the two Outlook side effects (send the draft item, tag a verdict
category). Sending is the one genuinely outward-facing action, so it always
goes through an explicit confirm.
"""
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from src import bot_drafts
from src.rich_text import RichTextEditor
from src.ui_common import SECONDARY_BTN_KWARGS

try:
    from src import outlook_inbox
except Exception:  # pragma: no cover - COM/module optional at import time
    outlook_inbox = None

# Verdict categories mirror the chatbot's VERDICT_CATEGORIES so the backend's
# --read-verdicts can pick them up if they ride along.
_GRADES = [("\U0001F44D Good", "good", "Bot: good"),
           ("\U0001F7E8 Needs work", "needed_fixes", "Bot: needed fixes"),
           ("\U0001F44E Wrong", "wrong", "Bot: wrong")]

# Outlook's default compose font, so the editing view resembles the real mail.
_EDIT_FONT_FAMILY = "Calibri"


class BotDraftsPanel:
    """Review/grade/edit/send bot reply drafts. Shown as a gated tab; can pop
    out into its own resizable window."""

    MIN_FONT, MAX_FONT = 9, 22

    def __init__(self, app) -> None:
        self.app = app
        self.tab = None
        self.window = None              # pop-out Toplevel when popped
        self.popped = False
        self.font_size = 12
        self.drafts: list = []
        self.selected = None            # the current BotDraft
        self.editor = None              # RichTextEditor for the reply body
        self._qbox = None               # student-question textbox
        self._grade = None              # verdict key once graded
        self._edited = False            # reply changed from the bot's original
        # rebuilt per mount
        self.list_frame = None
        self.detail = None
        self.status = None
        self.count_lbl = None

    # ---- mount / pop-out ------------------------------------------------
    def attach(self, tab) -> None:
        self.tab = tab
        self.mount(tab)

    def mount(self, parent) -> None:
        """(Re)build the whole panel into ``parent`` (the tab or a pop-out)."""
        for w in parent.winfo_children():
            w.destroy()
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        ctk.CTkButton(bar, text="↻ Refresh", width=90,
                      command=self.refresh, **SECONDARY_BTN_KWARGS).pack(
            side="left")
        self.count_lbl = ctk.CTkLabel(bar, text="", anchor="w")
        self.count_lbl.pack(side="left", padx=10)
        # right side: pop-out/dock + font steppers
        if self.popped:
            ctk.CTkButton(bar, text="⧉ Dock", width=80, command=self._dock,
                          **SECONDARY_BTN_KWARGS).pack(side="right")
        else:
            ctk.CTkButton(bar, text="⧉ Pop out", width=90,
                          command=self._pop_out,
                          **SECONDARY_BTN_KWARGS).pack(side="right")
        ctk.CTkButton(bar, text="A+", width=36,
                      command=lambda: self._bump_font(1),
                      **SECONDARY_BTN_KWARGS).pack(side="right", padx=(0, 6))
        ctk.CTkButton(bar, text="A−", width=36,
                      command=lambda: self._bump_font(-1),
                      **SECONDARY_BTN_KWARGS).pack(side="right")

        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 2))
        body.grid_columnconfigure(0, weight=0, minsize=210)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self.list_frame = ctk.CTkScrollableFrame(body, label_text="Drafts",
                                                 width=200)
        self.list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.detail = ctk.CTkFrame(body, fg_color="transparent")
        self.detail.grid(row=0, column=1, sticky="nsew")
        self.detail.grid_columnconfigure(0, weight=1)
        self.detail.grid_rowconfigure(1, weight=1)

        self.status = ctk.CTkLabel(
            parent, text="", font=ctk.CTkFont(size=12), anchor="w",
            text_color=("gray25", "gray80"), wraplength=900, justify="left")
        self.status.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 4))

        self.refresh()

    def _pop_out(self) -> None:
        win = ctk.CTkToplevel(self.app.root)
        win.title("Bot Drafts — review")
        win.minsize(760, 480)
        win.geometry("1150x760")
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(0, weight=1)
        win.protocol("WM_DELETE_WINDOW", self._dock)
        self.window = win
        self.popped = True
        holder = ctk.CTkFrame(win)
        holder.grid(row=0, column=0, sticky="nsew")
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(1, weight=1)
        self.mount(holder)
        self._show_tab_placeholder()
        win.after(80, win.lift)

    def _dock(self) -> None:
        win = self.window
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self.window = None
        self.popped = False
        if self.tab is not None:
            self.mount(self.tab)

    def _show_tab_placeholder(self) -> None:
        if self.tab is None:
            return
        for w in self.tab.winfo_children():
            w.destroy()
        box = ctk.CTkFrame(self.tab, fg_color="transparent")
        box.grid(row=0, column=0)
        ctk.CTkLabel(box, text="Bot Drafts reviewer is in its own window.",
                     text_color=("gray40", "gray65")).pack(pady=(20, 8))
        ctk.CTkButton(box, text="⧉ Dock", width=90, command=self._dock,
                      **SECONDARY_BTN_KWARGS).pack()

    def _bump_font(self, delta: int) -> None:
        self.font_size = max(self.MIN_FONT,
                             min(self.MAX_FONT, self.font_size + delta))
        # In-place so an in-progress edit isn't lost; construction also uses
        # the new size the next time a draft is selected.
        if self.editor is not None:
            try:
                self.editor.text.configure(
                    font=(_EDIT_FONT_FAMILY, self.font_size))
            except Exception:
                pass
        if self._qbox is not None:
            try:
                self._qbox.configure(font=ctk.CTkFont(size=self.font_size))
            except Exception:
                pass

    # ---- data -----------------------------------------------------------
    def refresh(self) -> None:
        """Reload drafts from the watch folder and rebuild the list."""
        self.drafts = []
        self.selected = None
        try:
            folder = bot_drafts.open_watch_folder(self.app.settings)
            if folder is not None:
                self.drafts = bot_drafts.load_pending(folder)
        except Exception as exc:  # keep the UI alive on any COM hiccup
            self._set_status(f"Couldn't read drafts: {exc}", error=True)
        self._build_list()
        if self.count_lbl is not None:
            self.count_lbl.configure(text=f"{len(self.drafts)} pending")
        self._clear_detail()
        if not self.drafts:
            self._set_status(
                "No bot drafts in the watch folder "
                f"(Drafts\\{bot_drafts.watch_folder_name(self.app.settings)})."
                " Enable/point it in Settings, and make sure the bot has "
                "filed drafts there.")

    def _build_list(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        for d in self.drafts:
            name = (d.to or d.message_key or "student").split("@")[0]
            conf = f"  {d.best_sim:.2f}" if d.best_sim is not None else ""
            flag = " ⚠" if d.low_confidence else ""
            label = f"{name}\n{d.action.replace('_draft', '')}{conf}{flag}"
            ctk.CTkButton(
                self.list_frame, text=label, anchor="w", height=44,
                command=lambda dd=d: self.select(dd),
                **SECONDARY_BTN_KWARGS,
            ).pack(fill="x", pady=2)

    # ---- selection / detail --------------------------------------------
    def select(self, draft) -> None:
        self.selected = draft
        self._grade = None
        self._edited = False
        self._render_detail()

    def _clear_detail(self) -> None:
        if self.detail is None:
            return
        for w in self.detail.winfo_children():
            w.destroy()
        self.editor = None
        self._qbox = None

    def _render_detail(self) -> None:
        d = self.selected
        self._clear_detail()

        # --- banner / header bar across the top ---
        head = ctk.CTkFrame(self.detail)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        head.grid_columnconfigure(0, weight=1)
        who = (d.to or "student").split("@")[0]
        conf = (f"conf {d.best_sim:.2f}" if d.best_sim is not None
                else "conf —")
        ctk.CTkLabel(
            head, anchor="w", justify="left",
            font=ctk.CTkFont(size=13, weight="bold"),
            text=f"{who}  ·  {d.subject}  ·  "
                 f"{d.action.replace('_draft', '')}  ·  {conf}"
                 + ("  ⚠ low confidence" if d.low_confidence else ""),
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        ctk.CTkButton(
            head, text="👁 Preview in Outlook", width=160,
            command=self._preview, **SECONDARY_BTN_KWARGS,
        ).grid(row=0, column=1, sticky="e", padx=8, pady=6)

        # --- two columns: student question | editable reply ---
        cols = ctk.CTkFrame(self.detail, fg_color="transparent")
        cols.grid(row=1, column=0, sticky="nsew")
        cols.grid_columnconfigure(0, weight=1, uniform="c")
        cols.grid_columnconfigure(1, weight=1, uniform="c")
        cols.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(cols)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(left, text="Student's email", anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
        self._qbox = ctk.CTkTextbox(left, wrap="word",
                                    font=ctk.CTkFont(size=self.font_size))
        self._qbox.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self._qbox.insert("1.0", d.question or "(question text unavailable — "
                          "click Preview in Outlook to see the quoted "
                          "original)")
        self._qbox.configure(state="disabled")

        right = ctk.CTkFrame(cols)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(right, text="Bot reply (editable)", anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
        self.editor = RichTextEditor(right, base_size=self.font_size)
        self.editor.frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self.editor.set_html(bot_drafts.editable_body(d.html)
                             or "<p>(no editable body)</p>")
        try:
            self.editor.text.configure(
                font=(_EDIT_FONT_FAMILY, self.font_size))
        except Exception:
            pass
        self._original_body = self.editor.to_html()
        self.editor.text.edit_modified(False)
        self.editor.text.bind("<<Modified>>", self._on_modified)
        ctk.CTkLabel(
            right, anchor="w", justify="left",
            font=ctk.CTkFont(size=11), text_color=("gray40", "gray65"),
            text="Your Outlook signature is included from the draft — use "
                 "Preview in Outlook to see exactly what will be sent.",
        ).grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 2))

        # --- Cc row: PM CC'd by default (resolved from the caseload) ---
        rows = getattr(self.app, "_caseload_rows", None)
        default_cc = ""
        try:
            default_cc = bot_drafts.default_cc_for(rows, d.student_email)
        except Exception:
            default_cc = ""
        self._cc_var = tk.StringVar(value=default_cc)
        cc_row = ctk.CTkFrame(right, fg_color="transparent")
        cc_row.grid(row=3, column=0, sticky="ew", padx=6, pady=(0, 2))
        cc_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cc_row, text="Cc:").grid(row=0, column=0, sticky="w",
                                              padx=(0, 6))
        ctk.CTkEntry(cc_row, textvariable=self._cc_var).grid(
            row=0, column=1, sticky="ew")
        hint = ("PM added automatically — edit or clear as needed"
                if default_cc else "add Cc addresses, separated by ;")
        ctk.CTkLabel(cc_row, text=hint, font=ctk.CTkFont(size=10),
                     text_color=("gray45", "gray60")).grid(
            row=1, column=1, sticky="w")

        # --- grade row (below the reply column) ---
        self._actions = ctk.CTkFrame(right, fg_color="transparent")
        self._actions.grid(row=4, column=0, sticky="ew", padx=6, pady=(0, 6))
        self._build_grade_row()

    def _build_grade_row(self) -> None:
        for w in self._actions.winfo_children():
            w.destroy()
        ctk.CTkLabel(self._actions, text="Grade the reply:", anchor="w").pack(
            side="left", padx=(0, 6))
        for text, key, _cat in _GRADES:
            ctk.CTkButton(
                self._actions, text=text, width=90,
                command=lambda k=key: self._on_grade(k),
                **SECONDARY_BTN_KWARGS).pack(side="left", padx=2)
        ctk.CTkButton(self._actions, text="🗑 Discard", width=90,
                      command=self._discard, **SECONDARY_BTN_KWARGS).pack(
            side="right")
        ctk.CTkButton(self._actions, text="Skip", width=60,
                      command=self._skip, **SECONDARY_BTN_KWARGS).pack(
            side="right", padx=(0, 6))

    # ---- flow -----------------------------------------------------------
    def _on_modified(self, _evt=None) -> None:
        if self.editor is None:
            return
        if self.editor.text.edit_modified():
            self._edited = (self.editor.to_html() != self._original_body)
            self.editor.text.edit_modified(False)
            if self._edited and getattr(self, "_send_bot_btn", None):
                try:
                    self._send_bot_btn.configure(state="disabled")
                except Exception:
                    pass

    def _on_grade(self, key: str) -> None:
        self._grade = key
        tagged = self._record_verdict(key)
        self._reveal_send_row()
        label = dict((k, t) for t, k, _ in _GRADES).get(key, key)
        note = ("" if tagged else
                " (couldn't tag the original email — score not logged)")
        self._set_status(f"Graded {label}{note}. Edit if needed, then send, "
                         "or send yourself a review copy.")

    def _reveal_send_row(self) -> None:
        for w in self._actions.winfo_children():
            w.destroy()
        ctk.CTkButton(self._actions, text="↺ Undo edits", width=100,
                      command=self._undo, **SECONDARY_BTN_KWARGS).pack(
            side="left", padx=(0, 6))
        # Review copy: sends the exact final email to YOU only — never the
        # student — and leaves the draft in place for the real send later.
        ctk.CTkButton(
            self._actions, text="\U0001F4E7 Send me a copy", width=140,
            command=self._send_copy, **SECONDARY_BTN_KWARGS).pack(
            side="left", padx=2)
        ctk.CTkButton(
            self._actions, text="Send as me", width=110,
            command=lambda: self._send(as_bot=False)).pack(side="left", padx=2)
        self._send_bot_btn = ctk.CTkButton(
            self._actions, text="Good / send as bot", width=150,
            command=lambda: self._send(as_bot=True))
        self._send_bot_btn.pack(side="left", padx=2)
        if self._edited:
            self._send_bot_btn.configure(state="disabled")
        ctk.CTkButton(self._actions, text="🗑 Discard", width=90,
                      command=self._discard, **SECONDARY_BTN_KWARGS).pack(
            side="right")

    def _discard(self) -> None:
        """Remove a bot draft from the queue WITHOUT sending — moves it to
        Deleted Items (recoverable). The student is not emailed; the original
        inbound is left for the reviewer to handle manually."""
        d = self.selected
        if d is None or d.item is None:
            self._set_status("No draft selected.", error=True)
            return
        if not messagebox.askyesno(
                "Discard draft?",
                "Remove this bot draft from the queue WITHOUT sending?\n\n"
                "It goes to Deleted Items (recoverable), the student is NOT "
                "emailed, and the original email is left for you to handle."):
            return
        try:
            d.item.Delete()
        except Exception as exc:
            self._set_status(f"Couldn't discard: {exc}", error=True)
            return
        self._set_status("Draft discarded (moved to Deleted Items). Not sent.")
        self.refresh()

    def _send_copy(self) -> None:
        """Send the exact final email to the reviewer ONLY (never the
        student), leaving the original draft untouched for a later real send.
        The grade was already recorded when the reviewer graded."""
        d = self.selected
        if d is None or d.item is None:
            self._set_status("No draft selected.", error=True)
            return
        reviewer = bot_drafts.reviewer_address()
        if not reviewer:
            self._set_status("Couldn't determine your Outlook email address.",
                             error=True)
            return
        if not messagebox.askyesno(
                "Send review copy?",
                f"Send a REVIEW COPY to you ({reviewer}) only?\n\n"
                "The student is NOT emailed, and the draft stays in the "
                "folder for later."):
            return
        edited = self.editor.to_html() if self._edited else None
        as_bot = not self._edited       # mirror the mode a real send would use
        html = bot_drafts.rebuild_for_send(d.html, edited, as_bot)
        try:
            copy = d.item.Copy()        # a throwaway; the original is untouched
            try:                        # strip ALL recipients (incl. the student)
                while copy.Recipients.Count > 0:
                    copy.Recipients.Remove(1)
            except Exception:
                pass
            copy.To = reviewer
            try:
                copy.Recipients.ResolveAll()
            except Exception:
                pass
            copy.Subject = "[REVIEW COPY] " + (copy.Subject or "")
            copy.HTMLBody = html
            copy.Send()
        except Exception as exc:
            self._set_status(f"Review copy failed: {exc}", error=True)
            return
        self._set_status(
            f"Review copy sent to you ({reviewer}). The student was NOT "
            "emailed; the draft is still in the folder.")

    def _undo(self) -> None:
        if self.editor is not None and self.selected is not None:
            self.editor.set_html(bot_drafts.editable_body(self.selected.html)
                                 or "<p>(no editable body)</p>")
            try:
                self.editor.text.configure(
                    font=(_EDIT_FONT_FAMILY, self.font_size))
            except Exception:
                pass
            self._original_body = self.editor.to_html()
            self.editor.text.edit_modified(False)
            self._edited = False
            if getattr(self, "_send_bot_btn", None):
                self._send_bot_btn.configure(state="normal")

    def _skip(self) -> None:
        self._set_status("Skipped — left in the folder for later.")
        self.selected = None
        self._clear_detail()

    def _preview(self) -> None:
        d = self.selected
        if d is None or d.item is None:
            self._set_status("No draft to preview.", error=True)
            return
        try:
            d.item.Display(False)  # non-modal Outlook inspector
            self._set_status("Opened the draft in Outlook — that's the true "
                             "rendering + signature. The red internal box is "
                             "NOT included when you Send from here.")
        except Exception as exc:
            self._set_status(f"Couldn't open in Outlook: {exc}", error=True)

    # ---- the outward-facing action -------------------------------------
    def _send(self, as_bot: bool) -> None:
        d = self.selected
        if d is None or d.item is None:
            self._set_status("No draft selected (or it can't be opened).",
                             error=True)
            return
        to = d.to or "the student"
        mode = ("an automated BOT reply" if as_bot
                else "a reply in YOUR name")
        cc = (self._cc_var.get() if getattr(self, "_cc_var", None) else "")
        cc = cc.strip()
        cc_line = f"\nCc: {cc}" if cc else ""
        if not messagebox.askyesno(
                "Send reply?",
                f"Send this to {to} as {mode}?{cc_line}\n\nThis emails them "
                "now."):
            return
        edited = self.editor.to_html() if self._edited else None
        html = bot_drafts.rebuild_for_send(d.html, edited, as_bot)
        try:
            d.item.HTMLBody = html
            if cc:
                d.item.CC = cc          # PM by default; reviewer-editable
            d.item.Send()
        except Exception as exc:
            self._set_status(f"Send failed: {exc}", error=True)
            return
        self._set_status(f"Sent to {to}. Add a note if you'd like, then "
                         "Refresh.", error=False)
        self._reveal_note_step(d)

    def _record_verdict(self, key: str) -> bool:
        """Record the grade so it reaches the backend scorecard: tag the
        ORIGINAL student email with the verdict category (which
        --read-verdicts logs). Returns True if the original was found + tagged.
        Never blocks the flow."""
        d = self.selected
        cat = dict((k, c) for _, k, c in _GRADES).get(key)
        if not (d and cat):
            return False
        try:
            return bot_drafts.tag_original_verdict(
                self.app.settings, d.message_key, cat)
        except Exception:
            return False

    # ---- note step (v1: suggested text + clipboard; full note-file TODO) -
    def _reveal_note_step(self, d) -> None:
        for w in self._actions.winfo_children():
            w.destroy()
        ctk.CTkLabel(self._actions, text="Note (optional) — edit, then copy:",
                     anchor="w").pack(fill="x")
        box = ctk.CTkTextbox(self._actions, height=56, wrap="word",
                             font=ctk.CTkFont(size=self.font_size))
        box.pack(fill="x", pady=(2, 4))
        box.insert("1.0",
                   f"Replied to {d.to} re: {d.subject}. Bot-drafted answer "
                   f"sent{' (edited)' if self._edited else ' (as bot)'}.")
        btnrow = ctk.CTkFrame(self._actions, fg_color="transparent")
        btnrow.pack(fill="x")

        def _copy():
            try:
                txt = box.get("1.0", "end").strip()
                self.app.root.clipboard_clear()
                self.app.root.clipboard_append(txt)
                self._set_status("Note text copied to clipboard.")
            except Exception:
                pass

        ctk.CTkButton(btnrow, text="\U0001F4CB Copy note", width=120,
                      command=_copy, **SECONDARY_BTN_KWARGS).pack(
            side="left", padx=2)
        ctk.CTkButton(btnrow, text="Done", width=70,
                      command=self.refresh).pack(side="left", padx=2)

    # ---- helpers --------------------------------------------------------
    def _set_status(self, msg: str, error: bool = False) -> None:
        if self.status is not None:
            self.status.configure(
                text=msg,
                text_color=("#c0392b" if error else ("gray25", "gray80")))
