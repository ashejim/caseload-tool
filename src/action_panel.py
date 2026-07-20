"""The main-window action pane — the scenario ("action") buttons.

Layout (when the user has defined groups):
  - **Pinned groups** render as collapsible, color-outlined boxes stacked on top
    (the classic style). "Ungrouped" (actions in no group) is a pinned box by
    default; the user can unpin it and empty it by moving actions into groups.
  - **Unpinned groups** become a **wrapping tab strip**; selecting a tab shows
    that group's actions in a color-outlined content box below. Each tab carries
    its own +, ⚙, and a pin toggle.
Pin state + the selected tab persist in settings.json.

App-coupled: reaches back through `app` for the action library
(`app.scenarios` / `app.groups`), firing (`app._fire`), the group dialogs
(`app._new_scenario_in_group` / `app._edit_group`), the display-name helper
(`app._action_display_name`), the persisted settings, and the queue-add
affordance. launcher keeps a thin `_rebuild_scenario_buttons` delegator plus
`button_frame` / `scenario_buttons` aliases onto this panel.
"""
import json
from typing import Optional

import customtkinter as ctk

from src.colors import (
    hover_color_for as _hover_color_for,
    text_color_for_bg as _text_color_for_bg,
)
from src.config import save_settings
from src.ui_common import SECONDARY_BTN_KWARGS, _attach_tooltip

# Sentinel key for the pseudo-"Ungrouped" section (won't collide with a real,
# user-entered group name).
UNGROUPED_KEY = "\x00ungrouped"
_UNGROUPED_COLOR = "#7a7a7a"


class ActionPanel:
    """Renders the scenario action buttons: pinned boxes + a group tab strip."""

    def __init__(self, app, parent):
        self.app = app
        self.frame = ctk.CTkFrame(parent)
        self.frame.grid_columnconfigure(0, weight=1)
        # name -> button (only the CURRENTLY-VISIBLE buttons: pinned boxes + the
        # active tab's content), so the App can reflect queue-add state on them.
        self.buttons: dict = {}
        self._collapsed: dict[str, bool] = {}   # pinned-box collapse, per session
        self._pinned: Optional[set] = None      # loaded lazily from settings
        self._active_tab: str = ""
        # Tab-strip wrapping state.
        self._tab_strip = None
        self._tab_specs: list = []
        self._relayout_after = None
        self._last_strip_w = -1

    # ------------------------------------------------------------------ state
    def _load_state(self) -> None:
        s = self.app.settings
        raw = (getattr(s, "action_pinned_groups", "") or "").strip()
        if not raw:
            self._pinned = {UNGROUPED_KEY}      # default: Ungrouped pinned
        else:
            try:
                self._pinned = set(json.loads(raw))
            except Exception:
                self._pinned = {UNGROUPED_KEY}
        self._active_tab = (getattr(s, "action_active_tab", "") or "")

    def _save_state(self) -> None:
        s = self.app.settings
        try:
            s.action_pinned_groups = json.dumps(sorted(self._pinned or []))
            s.action_active_tab = self._active_tab or ""
            save_settings(s)
        except Exception:
            pass

    # --------------------------------------------------------------- sections
    def _sections(self) -> list:
        """Ordered sections: the pseudo-Ungrouped one (if any loose actions),
        then each real group. Each is a uniform dict the renderer consumes."""
        app = self.app
        grouped: set = set()
        for g in app.groups:
            grouped.update(s for s in g.scenarios if s in app.scenarios)
        ungrouped = [n for n in app.scenarios if n not in grouped]
        secs: list = []
        if ungrouped:
            secs.append({"key": UNGROUPED_KEY, "label": "Ungrouped", "short": "",
                         "color": _UNGROUPED_COLOR, "names": ungrouped,
                         "group": None})
        for g in app.groups:
            secs.append({
                "key": g.name, "label": g.name, "short": g.short_name,
                "color": g.color, "group": g,
                "names": [s for s in g.scenarios if s in app.scenarios]})
        return secs

    # --------------------------------------------------------------- buttons
    def _scenario_btn(self, parent, name: str, sc,
                      color: Optional[str] = None) -> ctk.CTkButton:
        app = self.app
        label = app._action_display_name(sc) + (
            f"  ({sc.hotkey})" if sc.hotkey else "")
        kwargs: dict = dict(
            text=label, command=lambda s=sc: app._fire(s),
            width=160, height=36,
        )
        if color:
            kwargs["fg_color"] = color
            kwargs["text_color"] = _text_color_for_bg(color)
            kwargs["hover_color"] = _hover_color_for(color)
        btn = ctk.CTkButton(parent, **kwargs)
        self.buttons[name] = btn
        return btn

    def _grid_buttons(self, box, sec, start_row: int) -> int:
        """Grid a section's action buttons into `box` as a 2-column grid,
        indented. Returns the next free row."""
        box.grid_columnconfigure(0, weight=1)
        box.grid_columnconfigure(1, weight=1)
        color = sec["color"] if sec["group"] is not None else None
        names = sec["names"]
        for i, name in enumerate(names):
            btn = self._scenario_btn(box, name, self.app.scenarios[name],
                                     color=color)
            btn.grid(row=start_row + i // 2, column=i % 2,
                     padx=((14, 6) if i % 2 == 0 else (6, 14)),
                     pady=4, sticky="ew")
        return start_row + (len(names) + 1) // 2

    # ----------------------------------------------------------------- render
    def rebuild(self) -> None:
        app = self.app
        if self._relayout_after:
            try:
                self.frame.after_cancel(self._relayout_after)
            except Exception:
                pass
            self._relayout_after = None
        for w in self.frame.winfo_children():
            w.destroy()
        self.buttons.clear()
        self._tab_strip = None
        if self._pinned is None:
            self._load_state()

        # No groups at all → flat 2-column grid (legacy behavior).
        if not app.groups:
            self.frame.grid_columnconfigure(1, weight=1)
            for i, (name, sc) in enumerate(app.scenarios.items()):
                btn = self._scenario_btn(self.frame, name, sc)
                btn.grid(row=i // 2, column=i % 2, padx=6, pady=6, sticky="ew")
            self._reapply_queue_affordance()
            return
        self.frame.grid_columnconfigure(1, weight=0)

        secs = self._sections()
        keys = {s["key"] for s in secs}
        self._pinned = (self._pinned or set()) & keys   # prune stale pins
        pinned = [s for s in secs if s["key"] in self._pinned]
        tabs = [s for s in secs if s["key"] not in self._pinned]

        row = 0
        for sec in pinned:
            self._render_pinned_box(sec, row)
            row += 1

        if tabs:
            tabkeys = [s["key"] for s in tabs]
            if self._active_tab not in tabkeys:
                self._active_tab = tabkeys[0]
            self._tab_specs = tabs
            self._tab_strip = ctk.CTkFrame(self.frame, fg_color="transparent")
            self._tab_strip.grid(row=row, column=0, sticky="ew",
                                  padx=4, pady=(8, 2))
            self._tab_strip.grid_columnconfigure(0, weight=1)
            self._tab_strip.bind("<Configure>", self._on_strip_configure)
            row += 1
            self._layout_tabs()

            active = next(s for s in tabs if s["key"] == self._active_tab)
            content = ctk.CTkFrame(
                self.frame, fg_color="transparent", border_width=2,
                border_color=active["color"], corner_radius=8)
            content.grid(row=row, column=0, sticky="ew", padx=4, pady=(2, 6))
            row += 1
            end = self._grid_buttons(content, active, 0)
            ctk.CTkFrame(content, fg_color="transparent", height=4).grid(
                row=end, column=0, columnspan=2)
        self._reapply_queue_affordance()

    def _render_pinned_box(self, sec: dict, row: int) -> None:
        app = self.app
        key = sec["key"]
        collapsed = self._collapsed.get(key, False)
        box = ctk.CTkFrame(
            self.frame, fg_color="transparent", border_width=2,
            border_color=sec["color"], corner_radius=8)
        box.grid(row=row, column=0, sticky="ew", padx=4, pady=(8, 2))
        box.grid_columnconfigure(0, weight=1)
        box.grid_columnconfigure(1, weight=1)

        hdr = ctk.CTkFrame(box, fg_color="transparent")
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 2))
        hdr.grid_columnconfigure(0, weight=1)
        arrow = "▶" if collapsed else "▼"
        ctk.CTkButton(
            hdr, text=f"{arrow}  {sec['label']}", anchor="w", height=28,
            fg_color="transparent", text_color=("gray10", "gray90"),
            hover_color=("gray85", "gray25"),
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda k=key: self._toggle_collapse(k),
        ).grid(row=0, column=0, sticky="ew")
        col = 1
        unpin = ctk.CTkButton(
            hdr, text="📌", width=32, height=28,
            command=lambda k=key: self._toggle_pin(k), **SECONDARY_BTN_KWARGS)
        unpin.grid(row=0, column=col, padx=(4, 0))
        _attach_tooltip(unpin, "Unpin — move back to a tab")
        col += 1
        if sec["group"] is not None:
            g = sec["group"]
            ctk.CTkButton(
                hdr, text="+", width=32, height=28,
                command=lambda gn=g.name: app._new_scenario_in_group(gn),
                **SECONDARY_BTN_KWARGS).grid(row=0, column=col, padx=(4, 0))
            col += 1
            ctk.CTkButton(
                hdr, text="⚙", width=32, height=28,
                command=lambda gg=g: app._edit_group(gg),
                **SECONDARY_BTN_KWARGS).grid(row=0, column=col, padx=(4, 0))
        if collapsed:
            return
        end = self._grid_buttons(box, sec, 1)
        ctk.CTkFrame(box, fg_color="transparent", height=4).grid(
            row=end, column=0, columnspan=2)

    # ------------------------------------------------------------- tab strip
    def _make_tab(self, parent, sec: dict) -> ctk.CTkFrame:
        app = self.app
        key = sec["key"]
        color = sec["color"]
        active = (key == self._active_tab)
        chip = ctk.CTkFrame(parent, fg_color="transparent")
        label = sec["short"] or sec["label"]
        ctk.CTkButton(
            chip, text=label, height=28,
            fg_color=color if active else "transparent",
            text_color=(_text_color_for_bg(color) if active
                        else ("gray10", "gray90")),
            hover_color=_hover_color_for(color),
            border_width=0 if active else 2, border_color=color,
            font=ctk.CTkFont(size=12, weight="bold" if active else "normal"),
            command=lambda k=key: self._select_tab(k),
        ).pack(side="left")
        if sec["group"] is not None:
            g = sec["group"]
            ctk.CTkButton(
                chip, text="+", width=24, height=28,
                command=lambda gn=g.name: app._new_scenario_in_group(gn),
                **SECONDARY_BTN_KWARGS).pack(side="left", padx=(2, 0))
            ctk.CTkButton(
                chip, text="⚙", width=24, height=28,
                command=lambda gg=g: app._edit_group(gg),
                **SECONDARY_BTN_KWARGS).pack(side="left", padx=(2, 0))
        pin = ctk.CTkButton(
            chip, text="📌", width=24, height=28,
            command=lambda k=key: self._toggle_pin(k), **SECONDARY_BTN_KWARGS)
        pin.pack(side="left", padx=(2, 0))
        _attach_tooltip(pin, "Pin — show above the tabs")
        return chip

    def _layout_tabs(self) -> None:
        """(Re)flow the tab chips into rows, wrapping when they run out of
        horizontal room. Chips are recreated each pass (cheap; a handful of
        groups), so overflow is measured, not estimated."""
        strip = self._tab_strip
        if strip is None:
            return
        for w in strip.winfo_children():
            w.destroy()
        avail = strip.winfo_width()
        if avail <= 1:
            avail = self.frame.winfo_width()
        if avail <= 1:
            avail = 600           # not mapped yet; <Configure> reflows once sized
        avail = max(avail - 8, 120)

        rows = [0]
        rowf = [None]

        def _new_row():
            rowf[0] = ctk.CTkFrame(strip, fg_color="transparent")
            rowf[0].grid(row=rows[0], column=0, sticky="w")
            rows[0] += 1

        _new_row()
        for sec in self._tab_specs:
            chip = self._make_tab(rowf[0], sec)
            chip.pack(side="left", padx=(0, 6), pady=2)
            strip.update_idletasks()
            if (rowf[0].winfo_reqwidth() > avail
                    and len(rowf[0].winfo_children()) > 1):
                chip.destroy()
                _new_row()
                chip = self._make_tab(rowf[0], sec)
                chip.pack(side="left", padx=(0, 6), pady=2)

    def _on_strip_configure(self, event) -> None:
        # Reflow only on a real width change (avoid the feedback loop from our
        # own relayout, which changes the strip's height).
        if abs(event.width - self._last_strip_w) < 8:
            return
        self._last_strip_w = event.width
        if self._relayout_after:
            try:
                self.frame.after_cancel(self._relayout_after)
            except Exception:
                pass
        self._relayout_after = self.frame.after(80, self._layout_tabs)

    # ------------------------------------------------------------- callbacks
    def toggle_group(self, group_name: str) -> None:
        """Back-compat name (App had _toggle_group). Flip a pinned box's
        collapse state."""
        self._toggle_collapse(group_name)

    def _toggle_collapse(self, key: str) -> None:
        self._collapsed[key] = not self._collapsed.get(key, False)
        self.rebuild()

    def _toggle_pin(self, key: str) -> None:
        if self._pinned is None:
            self._load_state()
        if key in self._pinned:
            self._pinned.discard(key)
            self._active_tab = key   # show it as the active tab after unpinning
        else:
            self._pinned.add(key)
        self._save_state()
        self.rebuild()

    def _select_tab(self, key: str) -> None:
        self._active_tab = key
        self._save_state()
        self.rebuild()

    def _reapply_queue_affordance(self) -> None:
        # Keep the queue-add highlight consistent after any rebuild (e.g. a tab
        # switch while in add-mode rebuilds the buttons).
        fn = getattr(self.app, "_refresh_queue_add_affordance", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
