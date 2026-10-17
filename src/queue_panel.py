"""The Action-queue panel — a log-window tab listing queued batch actions with
add/review/run/pause controls. A thin view over the App's ActionQueue; all
state + execution live on the App (passed in as `app`).
"""
import customtkinter as ctk

from src.action_queue import QueueItem, QueueStatus
from src.ui_common import SECONDARY_BTN_KWARGS, _attach_tooltip


class QueuePanel:
    """The 'Queue' tab: review multiple BATCH actions up front, then run them
    in sequence. Review happens when an action is ADDED (its edited payload is
    stored on the QueueItem); the actual sending/saving happens only on Run.

    Stage 1 builds the shell — the control bar and the row list rendered from
    ``app.action_queue`` — with the model-only interactions wired (check /
    uncheck / remove a PENDING row). Add-to-queue (stage 2), Run (stage 3),
    and Pause/Continue/Cancel (stage 4) fill in the inert controls."""

    # status -> (glyph, (light_color, dark_color))
    _STATUS_ICON = {
        QueueStatus.PENDING: ("○", ("gray45", "gray60")),
        QueueStatus.RUNNING: ("●", ("#1f6feb", "#4a9eff")),
        QueueStatus.DONE:    ("✓", ("#2e7d32", "#3fb950")),
        QueueStatus.ERROR:   ("✗", ("#c62828", "#e0524f")),
    }

    def __init__(self, app) -> None:
        self.app = app
        self.tab = None
        self.ctrls = None
        self.listbox = None          # scrollable frame holding the rows
        self._add_btn = None
        self._start_btn = None
        self._pause_btn = None
        self._cancel_btn = None
        self._clear_done_btn = None
        self.pl_bar = None
        self.playlist_var = None
        self._playlist_menu = None
        self._load_btn = None
        self._manage_btn = None

    # ---- mount -----------------------------------------------------------
    def attach(self, tab) -> None:
        self.tab = tab
        self.mount(tab)

    def mount(self, parent) -> None:
        for w in parent.winfo_children():
            w.destroy()
        parent.grid_columnconfigure(0, weight=1)
        # Only the row list (row 2) may stretch/shrink. The tab container may
        # arrive with weight on row 0 (the launcher's generic one-widget tab
        # setup) — clear it, or a height squeeze collapses the playlist bar.
        parent.grid_rowconfigure(0, weight=0)
        parent.grid_rowconfigure(1, weight=0)
        parent.grid_rowconfigure(2, weight=1)

        # Playlist bar: pick a saved playlist and load its actions into the
        # queue in one step, or open the manager to create/edit playlists.
        self.pl_bar = ctk.CTkFrame(parent, fg_color="transparent")
        self.pl_bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
        ctk.CTkLabel(self.pl_bar, text="🎵 Playlist:").pack(side="left")
        self.playlist_var = ctk.StringVar(value="")
        self._playlist_menu = ctk.CTkOptionMenu(
            self.pl_bar, variable=self.playlist_var, values=["(none)"],
            width=220)
        self._playlist_menu.pack(side="left", padx=(6, 0))
        self._load_btn = ctk.CTkButton(
            self.pl_bar, text="▶ Load", width=76, command=self._on_load_playlist)
        self._load_btn.pack(side="left", padx=(8, 0))
        try:
            _attach_tooltip(
                self._load_btn,
                "Review + add this playlist's actions to the queue. They land "
                "as checked rows — press ▶ Start to run them.")
        except Exception:
            pass
        self._manage_btn = ctk.CTkButton(
            self.pl_bar, text="⚙ Manage playlists", width=150,
            command=self._on_manage_playlists, **SECONDARY_BTN_KWARGS)
        self._manage_btn.pack(side="right")

        # Control bar: Add-to-queue toggle | Start | Pause | Cancel.
        self.ctrls = ctk.CTkFrame(parent, fg_color="transparent")
        self.ctrls.grid(row=1, column=0, sticky="ew", padx=6, pady=(4, 2))
        self._add_btn = ctk.CTkButton(
            self.ctrls, text="➕ Add to queue", width=130,
            command=self._toggle_add_mode,
        )
        self._add_btn.pack(side="left")
        self._start_btn = ctk.CTkButton(
            self.ctrls, text="▶ Start", width=80,
            command=self._on_start, state="disabled",
        )
        self._start_btn.pack(side="left", padx=(8, 0))
        self._pause_btn = ctk.CTkButton(
            self.ctrls, text="⏸ Pause", width=90,
            command=self._on_pause, state="disabled", **SECONDARY_BTN_KWARGS,
        )
        self._pause_btn.pack(side="left", padx=(8, 0))
        self._cancel_btn = ctk.CTkButton(
            self.ctrls, text="✕ Cancel", width=90,
            command=self._on_cancel, state="disabled", **SECONDARY_BTN_KWARGS,
        )
        self._cancel_btn.pack(side="left", padx=(8, 0))
        # Clear the completed (✓ DONE) rows — tidies the list after a run.
        # Right-aligned, away from the run controls. Kept-ERROR rows stay.
        self._clear_done_btn = ctk.CTkButton(
            self.ctrls, text="🧹 Clear completed", width=140,
            command=self._on_clear_done, state="disabled",
            **SECONDARY_BTN_KWARGS,
        )
        self._clear_done_btn.pack(side="right")

        # Scrollable row list.
        self.listbox = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        self.listbox.grid(row=2, column=0, sticky="nsew", padx=6, pady=(2, 6))
        self.listbox.grid_columnconfigure(0, weight=1)
        self.refresh_playlists()
        self.refresh()

    # ---- render ----------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the row list from app.action_queue and sync control state."""
        if self.listbox is None:
            return
        for w in self.listbox.winfo_children():
            w.destroy()

        items = self.app.action_queue.items
        if not items:
            ctk.CTkLabel(
                self.listbox,
                text=("No actions queued.\n\nTurn on “➕ Add to queue”, then "
                      "click the batch actions you want to run in sequence."),
                justify="left", text_color=("gray40", "gray60"),
            ).grid(row=0, column=0, sticky="w", padx=8, pady=12)
        else:
            for i, it in enumerate(items):
                self._render_row(i, it)

        self._sync_controls()

    def _render_row(self, row: int, it: QueueItem) -> None:
        frame = ctk.CTkFrame(self.listbox, fg_color=("gray92", "gray17"))
        frame.grid(row=row, column=0, sticky="ew", pady=3)
        frame.grid_columnconfigure(3, weight=1)

        # checkbox — (un)checkable for PENDING/ERROR (ERROR = retry); RUNNING
        # locked, DONE locked (can't re-send).
        var = ctk.BooleanVar(value=it.checked)
        chk = ctk.CTkCheckBox(
            frame, text="", width=24, variable=var,
            command=lambda n=it.action_name, v=var: self._toggle_check(n, v),
        )
        if not it.status.can_check:
            chk.configure(state="disabled")
        chk.grid(row=0, column=0, padx=(8, 4), pady=6)

        # color chip
        chip = ctk.CTkFrame(frame, width=12, height=12, corner_radius=3,
                            fg_color=it.color or ("gray70", "gray45"))
        chip.grid(row=0, column=1, padx=(0, 6))
        chip.grid_propagate(False)

        # status icon
        glyph, col = self._STATUS_ICON.get(
            it.status, self._STATUS_ICON[QueueStatus.PENDING])
        ctk.CTkLabel(frame, text=glyph, width=18, text_color=col,
                     font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=2, padx=(0, 4))

        # name (+ error detail on failed rows, so 'Start' = retry is obvious)
        label = it.display_name
        if it.status == QueueStatus.ERROR and it.error_detail:
            label += f"   —  ✗ {it.error_detail} (re-check + Start to retry)"
        name_lbl = ctk.CTkLabel(frame, text=label, anchor="w")
        if it.status == QueueStatus.ERROR:
            name_lbl.configure(text_color=("#c0392b", "#e0524f"))
        name_lbl.grid(row=0, column=3, sticky="ew", padx=2)

        # preview 👁 — scope the caseload viewer to this action's target students
        # (resolved + stored at add time), so the user can inspect exactly who
        # it'll act on before running. Only shown when targets were captured.
        targets = (it.payload or {}).get("confirmed") \
            if isinstance(it.payload, dict) else None
        if targets:
            prev = ctk.CTkButton(
                frame, text="👁", width=28, height=24,
                command=lambda t=targets, n=it.display_name, c=it.color:
                    self.app._preview_queue_item_targets(t, n, c),
                **SECONDARY_BTN_KWARGS,
            )
            try:
                _attach_tooltip(
                    prev, f"Show these {len(targets)} student(s) in the "
                          "caseload viewer.")
            except Exception:
                pass
            prev.grid(row=0, column=4, padx=(0, 2))

        # remove — allowed for anything except the currently-running row.
        rm = ctk.CTkButton(
            frame, text="✕", width=28, height=24,
            command=lambda n=it.action_name: self._remove(n),
            **SECONDARY_BTN_KWARGS,
        )
        if not it.status.can_remove:
            rm.configure(state="disabled")
        rm.grid(row=0, column=5, padx=(4, 8))

    def _sync_controls(self) -> None:
        """Enable/disable the control bar. Start runs when there's at least one
        checked, still-PENDING item and no run is in progress. Pause/Cancel come
        alive in stage 4."""
        running = getattr(self.app, "_queue_running", False)
        can_start = bool(self.app._queue_run_set()) and not running
        if self._start_btn is not None:
            self._start_btn.configure(
                state="normal" if can_start else "disabled")
        # add-mode button reflects its toggle state; disabled during a run
        if self._add_btn is not None:
            on = getattr(self.app, "_queue_add_mode", False)
            self._add_btn.configure(
                text="➕ Adding… (click actions)" if on else "➕ Add to queue",
                state="disabled" if running else "normal")
        # clear-completed: enabled when there's a DONE row and no run in progress
        if self._clear_done_btn is not None:
            self._clear_done_btn.configure(
                state="normal" if (self.app.action_queue.has_done()
                                   and not running) else "disabled")
        # playlist Load: needs a real playlist selected and an idle queue (a
        # load reviews + adds actions, which can't happen mid-run or mid-task).
        if self._load_btn is not None:
            has_pl = bool(getattr(self.app, "playlists", None))
            busy = getattr(self.app, "_is_busy", False)
            self._load_btn.configure(
                state="normal" if (has_pl and not running and not busy)
                else "disabled")

    def on_run_state_changed(self) -> None:
        """Reflect the run state on Pause/Cancel (and, via _sync_controls,
        Start/Add). Pause reads Continue once actually paused, and shows a
        disabled 'Pausing…' while the in-flight action is still finishing."""
        running = getattr(self.app, "_queue_running", False)
        paused = getattr(self.app, "_queue_paused", False)
        busy = getattr(self.app, "_is_busy", False)
        if self._pause_btn is not None:
            if not running:
                self._pause_btn.configure(state="disabled", text="⏸ Pause")
            elif paused and busy:
                self._pause_btn.configure(state="disabled", text="⏸ Pausing…")
            elif paused:
                self._pause_btn.configure(state="normal", text="▶ Continue")
            else:
                self._pause_btn.configure(state="normal", text="⏸ Pause")
        if self._cancel_btn is not None:
            self._cancel_btn.configure(
                state="normal" if running else "disabled")
        self._sync_controls()

    # ---- playlists -------------------------------------------------------
    def refresh_playlists(self) -> None:
        """Repopulate the playlist dropdown from app.playlists. Call after the
        manager saves (via App) and once at mount."""
        if self._playlist_menu is None:
            return
        names = [p.name for p in getattr(self.app, "playlists", None) or []]
        if names:
            self._playlist_menu.configure(values=names, state="normal")
            if self.playlist_var.get() not in names:
                self.playlist_var.set(names[0])
        else:
            self._playlist_menu.configure(values=["(no playlists)"],
                                          state="disabled")
            self.playlist_var.set("(no playlists)")
        self._sync_controls()

    def _on_load_playlist(self) -> None:
        name = self.playlist_var.get()
        if not name or not (getattr(self.app, "playlists", None) or []):
            return
        self.app._queue_load_playlist(name)

    def _on_manage_playlists(self) -> None:
        self.app._open_playlist_manager()

    # ---- interactions ---------------------------------------------------
    def _toggle_check(self, action_name: str, var) -> None:
        it = self.app.action_queue.get(action_name)
        if it is None or not it.status.can_check:
            return
        it.checked = bool(var.get())
        self._sync_controls()

    def _remove(self, action_name: str) -> None:
        it = self.app.action_queue.get(action_name)
        if it is None or not it.status.can_remove:
            return
        # (Stage 5 adds the "unsaved review will be discarded" warning.)
        self.app.action_queue.remove(action_name)
        self.refresh()
        self.app._refresh_queue_add_affordance()

    def _toggle_add_mode(self) -> None:
        # Full button-mode switching arrives in stage 2; stage 1 just flips the
        # flag + its own label so the control is visibly wired.
        self.app._queue_add_mode = not getattr(self.app, "_queue_add_mode", False)
        self._sync_controls()
        self.app._refresh_queue_add_affordance()

    def _on_start(self) -> None:
        self.app._queue_start()

    def _on_pause(self) -> None:
        # One button toggles Pause <-> Continue.
        if getattr(self.app, "_queue_paused", False):
            self.app._queue_continue()
        else:
            self.app._queue_pause()

    def _on_cancel(self) -> None:
        self.app._queue_cancel()

    def _on_clear_done(self) -> None:
        """Remove the completed (✓ DONE) rows from the queue. ERROR rows stay
        (they may still be retried)."""
        if getattr(self.app, "_queue_running", False):
            return
        gone = self.app.action_queue.remove_done()
        if gone:
            self.app._append_log(
                f"Queue: cleared {len(gone)} completed action(s).")
        self.refresh()
        self.app._refresh_queue_add_affordance()
