"""The main-window action pane — the scenario ("action") buttons, grouped.

Extracted from scripts/launcher.py as part of the launcher decomposition, and
the seam for the upcoming tabbed/pinnable redesign. Owns the button container,
the per-action buttons, and the grouped/collapsible rendering.

App-coupled: it reaches back through `app` for the action library
(`app.scenarios` / `app.groups`), firing (`app._fire`), the group dialogs
(`app._new_scenario_in_group` / `app._edit_group`), and the display-name helper
(`app._action_display_name`). launcher.py keeps a thin `_rebuild_scenario_
buttons` delegator plus `button_frame` / `scenario_buttons` aliases onto this
panel, so its many call sites stay unchanged.
"""
from typing import Optional

import customtkinter as ctk

from src.colors import (
    hover_color_for as _hover_color_for,
    text_color_for_bg as _text_color_for_bg,
)
from src.ui_common import SECONDARY_BTN_KWARGS


class ActionPanel:
    """Renders the scenario action buttons into its own frame."""

    def __init__(self, app, parent):
        self.app = app
        self.frame = ctk.CTkFrame(parent)
        # name -> button, so the App can reflect queue-add state on them.
        self.buttons: dict = {}
        # Per-group collapsed flag, per-session (not persisted).
        self._group_collapsed: dict[str, bool] = {}

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

    def rebuild(self) -> None:
        """Render the scenario button list. Layout depends on whether the user
        has defined any groups:

        - No groups: flat 2-column grid (legacy behavior).
        - With groups: Ungrouped section at top (only if non-empty), followed by
          each group as a collapsible color-coded section."""
        app = self.app
        for w in self.frame.winfo_children():
            w.destroy()
        self.buttons.clear()
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_columnconfigure(1, weight=1)

        # No groups → flat grid (original behavior preserved).
        if not app.groups:
            for i, (name, sc) in enumerate(app.scenarios.items()):
                btn = self._scenario_btn(self.frame, name, sc)
                btn.grid(row=i // 2, column=i % 2, padx=6, pady=6, sticky="ew")
            return

        # With groups → sectioned layout.
        row = 0
        grouped_names: set = set()
        for g in app.groups:
            grouped_names.update(s for s in g.scenarios if s in app.scenarios)
        ungrouped = [n for n in app.scenarios if n not in grouped_names]

        if ungrouped:
            ctk.CTkLabel(
                self.frame, text="Ungrouped",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=("gray40", "gray70"),
                anchor="w",
            ).grid(row=row, column=0, columnspan=2,
                   sticky="ew", padx=6, pady=(4, 2))
            row += 1
            for i, name in enumerate(ungrouped):
                btn = self._scenario_btn(self.frame, name, app.scenarios[name])
                btn.grid(row=row + i // 2, column=i % 2,
                         padx=6, pady=4, sticky="ew")
            row += (len(ungrouped) + 1) // 2

        for group in app.groups:
            collapsed = self._group_collapsed.get(group.name, False)
            # Each group is a box outlined in its own color. The header mirrors
            # the note-editor dropdown (transparent fill, bold text, ▼/▶ arrow)
            # so it reads as a section title; member buttons sit indented inside.
            box = ctk.CTkFrame(
                self.frame, fg_color="transparent",
                border_width=2, border_color=group.color, corner_radius=8,
            )
            box.grid(row=row, column=0, columnspan=2,
                     sticky="ew", padx=4, pady=(8, 2))
            box.grid_columnconfigure(0, weight=1)
            box.grid_columnconfigure(1, weight=1)
            row += 1

            header = ctk.CTkFrame(box, fg_color="transparent")
            header.grid(row=0, column=0, columnspan=2,
                        sticky="ew", padx=6, pady=(4, 2))
            header.grid_columnconfigure(0, weight=1)
            arrow = "▶" if collapsed else "▼"
            ctk.CTkButton(
                header, text=f"{arrow}  {group.name}",
                anchor="w", height=28,
                fg_color="transparent",
                text_color=("gray10", "gray90"),
                hover_color=("gray85", "gray25"),
                font=ctk.CTkFont(size=13, weight="bold"),
                command=lambda gn=group.name: self.toggle_group(gn),
            ).grid(row=0, column=0, sticky="ew")
            # '+' adds a new action directly into this group.
            ctk.CTkButton(
                header, text="+", width=32, height=28,
                command=lambda gn=group.name: app._new_scenario_in_group(gn),
                **SECONDARY_BTN_KWARGS,
            ).grid(row=0, column=1, padx=(4, 0))
            ctk.CTkButton(
                header, text="⚙", width=32, height=28,
                command=lambda g=group: app._edit_group(g),
                **SECONDARY_BTN_KWARGS,
            ).grid(row=0, column=2, padx=(4, 0))
            if collapsed:
                continue
            valid = [s for s in group.scenarios if s in app.scenarios]
            for i, name in enumerate(valid):
                btn = self._scenario_btn(
                    box, name, app.scenarios[name], color=group.color,
                )
                # Extra left/right inset so buttons read as indented children of
                # the group rather than full-width rows.
                btn.grid(row=1 + i // 2, column=i % 2,
                         padx=((14, 6) if i % 2 == 0 else (6, 14)),
                         pady=4, sticky="ew")
            # Trailing inner pad so the last row doesn't touch the border.
            ctk.CTkFrame(box, fg_color="transparent", height=4).grid(
                row=1 + (len(valid) + 1) // 2, column=0, columnspan=2)

    def toggle_group(self, group_name: str) -> None:
        """Flip a group's collapsed flag and re-render. Per-session (not
        persisted)."""
        self._group_collapsed[group_name] = (
            not self._group_collapsed.get(group_name, False))
        self.rebuild()
