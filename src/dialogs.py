"""Standalone modal dialogs — self-contained ``parent + args -> value`` popups.

Each function here builds a Toplevel/CTk modal, runs it, and returns the user's
result (or None on cancel). None of them touch the app controller or app-wide
mutable state; shared UI primitives come from ``src.ui_common``. Extracted from
``scripts/launcher.py`` so the dialog surface is browsable on its own.

Note: the two rich-text dialogs (the HTML template editor and batch email
review) still live in launcher.py — they depend on the RichTextEditor widget,
which moves out in a later step.
"""
from typing import Callable
from pathlib import Path

import customtkinter as ctk
import tkinter as tk
from tkinter import ttk

from src import hotkeys
from src.note_form import (
    ACADEMIC_ACTIVITY_LABELS, INTERACTION_FORMATS,
    activities_disabled_for, types_for_format,
)
from src.scenarios import render_note_template
from src.ui_common import (
    SECONDARY_BTN_KWARGS, _ADD_BTN_BLUE, _ADD_BTN_BLUE_HOVER,
    _build_checkbox_images, _fit_dialog_to_content,
    _restore_dialog_geometry, _save_dialog_geometry,
)


def open_hotkey_capture(parent, on_done: Callable[[str], None]) -> None:
    """Pop a modal that captures a key combination. Calls on_done with
    the captured string (e.g. 'Ctrl+Shift+A', 'F4') or "" on cancel.
    Modifier keys are not finalized until a non-modifier is pressed."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Press hotkey")
    dialog.geometry("420x240")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    ctk.CTkLabel(
        dialog,
        text=("Press the keys you want as the hotkey.\n"
              "Example: F3, or hold Ctrl+Shift then press A.\n"
              "Esc to cancel.\n\n"
              "Avoid browser-claimed F-keys: F1 (help), F6 (address bar),\n"
              "F11 (fullscreen), F12 (devtools) — Chromium intercepts these\n"
              "before our hook can fire."),
        justify="left",
    ).pack(padx=20, pady=(15, 5))

    preview_var = ctk.StringVar(value="—")
    ctk.CTkLabel(
        dialog, textvariable=preview_var,
        font=ctk.CTkFont(size=18, weight="bold"),
    ).pack(pady=4)

    held: set[str] = set()
    finished = {"done": False}

    def current_mods_str() -> str:
        mods = [m for m in hotkeys.HOTKEY_MOD_ORDER if m in held]
        return "+".join(mods) if mods else "—"

    def finish(combo: str) -> None:
        if finished["done"]:
            return
        finished["done"] = True
        try:
            dialog.grab_release()
        except Exception:
            pass
        try:
            dialog.destroy()
        except Exception:
            pass
        on_done(combo)

    def on_press(event):
        if finished["done"]:
            return
        ks = event.keysym
        if ks == "Escape":
            finish("")
            return
        if ks in ("Control_L", "Control_R"):
            held.add("Ctrl"); preview_var.set(current_mods_str()); return
        if ks in ("Shift_L", "Shift_R"):
            held.add("Shift"); preview_var.set(current_mods_str()); return
        if ks in ("Alt_L", "Alt_R"):
            held.add("Alt"); preview_var.set(current_mods_str()); return
        if ks in ("Super_L", "Super_R", "Win_L", "Win_R", "Caps_Lock", "Num_Lock"):
            return
        mods = [m for m in hotkeys.HOTKEY_MOD_ORDER if m in held]
        combo = "+".join(mods + [hotkeys.keysym_to_hotkey_part(ks)])
        preview_var.set(combo)
        dialog.after(150, lambda: finish(combo))

    def on_release(event):
        ks = event.keysym
        if ks in ("Control_L", "Control_R"): held.discard("Ctrl")
        elif ks in ("Shift_L", "Shift_R"): held.discard("Shift")
        elif ks in ("Alt_L", "Alt_R"): held.discard("Alt")
        if not finished["done"]:
            preview_var.set(current_mods_str())

    dialog.bind("<KeyPress>", on_press)
    dialog.bind("<KeyRelease>", on_release)
    ctk.CTkButton(dialog, text="Cancel", command=lambda: finish(""), width=90).pack(pady=10)
    dialog.focus_set()


def prompt_add_image_dialog(
    parent, templates_dir: Path,
) -> tuple[Optional[str], Optional[str]]:
    """Modal dialog for adding an inline (CID-embedded) image to a
    template. Walks the user through choosing a file, sizing it, and
    optionally linking it; on Insert it copies the file into the
    templates folder (if not already there) and builds an `<img
    src="cid:STEM">` snippet for the editor to drop at the cursor.

    Returns:
        (html_snippet, filename) on Insert — caller drops the
        snippet into the template AND registers `filename` in the
        scenario's inline_images list so the runtime knows to
        attach + bind the CID. Returns (None, None) on Cancel.

    Pillow is used opportunistically to read natural dimensions when
    the user picks a file, so width/height auto-populate."""
    from tkinter import messagebox, filedialog
    import shutil

    dialog = ctk.CTkToplevel(parent)
    dialog.title("Add image")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    result = {"html": None, "filename": None}
    state = {"src_path": None}

    # Row 1: source file picker
    file_row = ctk.CTkFrame(dialog, fg_color="transparent")
    file_row.pack(fill="x", padx=14, pady=(14, 4))
    ctk.CTkLabel(file_row, text="Image file:", width=80, anchor="w").pack(side="left")
    file_entry = ctk.CTkEntry(
        file_row, placeholder_text="click Browse…", width=320,
    )
    file_entry.pack(side="left", padx=(4, 4))

    def on_browse() -> None:
        path = filedialog.askopenfilename(
            parent=dialog,
            title="Choose image",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        p = Path(path)
        state["src_path"] = p
        file_entry.delete(0, "end")
        file_entry.insert(0, str(p))
        # Auto-fill width/height from the image's natural dimensions.
        # Failure (Pillow missing, file unreadable) is silent — the
        # user can still type values manually.
        try:
            from PIL import Image
            with Image.open(p) as im:
                w, h = im.size
            width_entry.delete(0, "end")
            width_entry.insert(0, str(w))
            height_entry.delete(0, "end")
            height_entry.insert(0, str(h))
        except Exception:
            pass

    ctk.CTkButton(
        file_row, text="Browse…", width=90, command=on_browse,
    ).pack(side="left", padx=(4, 0))

    # Row 2: dimensions
    dim_row = ctk.CTkFrame(dialog, fg_color="transparent")
    dim_row.pack(fill="x", padx=14, pady=4)
    ctk.CTkLabel(dim_row, text="Width:", width=80, anchor="w").pack(side="left")
    width_entry = ctk.CTkEntry(
        dim_row, placeholder_text="px (auto)", width=100,
    )
    width_entry.pack(side="left", padx=(4, 12))
    ctk.CTkLabel(dim_row, text="Height:").pack(side="left")
    height_entry = ctk.CTkEntry(
        dim_row, placeholder_text="px (auto)", width=100,
    )
    height_entry.pack(side="left", padx=(4, 0))

    # Row 3: alt text
    alt_row = ctk.CTkFrame(dialog, fg_color="transparent")
    alt_row.pack(fill="x", padx=14, pady=4)
    ctk.CTkLabel(alt_row, text="Alt text:", width=80, anchor="w").pack(side="left")
    alt_entry = ctk.CTkEntry(
        alt_row, placeholder_text="shown if image fails / for accessibility",
        width=380,
    )
    alt_entry.pack(side="left", padx=(4, 0))

    # Row 4: optional clickable link
    link_row = ctk.CTkFrame(dialog, fg_color="transparent")
    link_row.pack(fill="x", padx=14, pady=4)
    ctk.CTkLabel(link_row, text="Link to:", width=80, anchor="w").pack(side="left")
    link_entry = ctk.CTkEntry(
        link_row,
        placeholder_text="optional — clicking the image opens this URL",
        width=380,
    )
    link_entry.pack(side="left", padx=(4, 0))

    # Hint
    ctk.CTkLabel(
        dialog,
        text="The image gets copied to your templates folder and "
             "embedded via cid: so it travels with the email (no "
             "remote-image warning on the recipient's side).",
        font=ctk.CTkFont(size=11),
        text_color=("gray45", "gray65"),
        wraplength=480, justify="left",
    ).pack(padx=14, pady=(6, 4), anchor="w")

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=14, pady=(8, 14))

    def do_insert() -> None:
        src = state["src_path"]
        if not src or not Path(src).exists():
            messagebox.showerror(
                "No image selected",
                "Click Browse… and choose an image file first.",
                parent=dialog,
            )
            return
        target = templates_dir / src.name
        if src.resolve() != target.resolve():
            try:
                templates_dir.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if not messagebox.askyesno(
                        "Overwrite?",
                        f"{src.name} already exists in your templates "
                        f"folder. Overwrite with the new file?",
                        parent=dialog,
                    ):
                        return
                shutil.copyfile(src, target)
            except Exception as e:
                messagebox.showerror(
                    "Copy failed",
                    f"Couldn't copy the image into the templates folder:\n\n{e}",
                    parent=dialog,
                )
                return
        cid = target.stem
        # html.escape would over-escape attribute values; for the
        # subset of chars that matter inside an attribute (`"`) a
        # simple replace is enough.
        def _attr(s: str) -> str:
            return s.replace("&", "&amp;").replace('"', "&quot;")
        attrs = [f'src="cid:{cid}"']
        alt = alt_entry.get().strip()
        if alt:
            attrs.append(f'alt="{_attr(alt)}"')
        w = width_entry.get().strip()
        if w:
            attrs.append(f'width="{_attr(w)}"')
        h = height_entry.get().strip()
        if h:
            attrs.append(f'height="{_attr(h)}"')
        attrs.append('style="display:block; border:0;"')
        img_tag = f"<img {' '.join(attrs)} />"
        link = link_entry.get().strip()
        if link:
            snippet = f'<p>\n  <a href="{_attr(link)}">\n    {img_tag}\n  </a>\n</p>'
        else:
            snippet = f"<p>{img_tag}</p>"
        result["html"] = snippet
        result["filename"] = target.name
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def do_cancel() -> None:
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    ctk.CTkButton(
        btn_row, text="Insert", width=110, command=do_insert,
    ).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", width=90, command=do_cancel,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)
    dialog.protocol("WM_DELETE_WINDOW", do_cancel)
    dialog.bind("<Escape>", lambda _e: do_cancel())
    dialog.lift()
    dialog.focus_force()

    parent.wait_window(dialog)
    return result["html"], result["filename"]


def prompt_calendar_pick(parent, initial_date=None):
    """Small monthly calendar picker. Click a day → returns that
    `datetime.date`. Returns None on cancel. Built from CTk widgets
    + Python's stdlib `calendar` module — no third-party deps.

    Used by FilterRow's date operators (is before / is after / is
    on) so users can click a date instead of typing the format."""
    import calendar as _cal
    from datetime import date as _date

    today = _date.today()
    base = initial_date if initial_date else today
    state = {"year": base.year, "month": base.month, "selected": None}

    dialog = ctk.CTkToplevel(parent)
    dialog.title("Pick a date")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    # Pop up near the mouse so the cursor barely travels (general practice
    # for small popups). Offset slightly up-left so the pointer lands just
    # inside, and clamp to the screen so it never opens off-edge.
    _w, _h = 280, 300
    try:
        _px, _py = parent.winfo_pointerxy()
        _sw, _sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
        _x = min(max(_px - 20, 0), max(_sw - _w, 0))
        _y = min(max(_py - 20, 0), max(_sh - _h, 0))
        dialog.geometry(f"{_w}x{_h}+{_x}+{_y}")
    except Exception:
        dialog.geometry(f"{_w}x{_h}")
    # Topmost-claw-back (same pattern as our other modals so a busy
    # background window can't bury it).
    dialog.lift()
    dialog.focus_force()
    dialog.after(120, lambda: (dialog.lift(), dialog.focus_force()))

    # Header: ◀ Month Year ▶
    header = ctk.CTkFrame(dialog, fg_color="transparent")
    header.pack(fill="x", padx=8, pady=(8, 0))

    def _change_month(delta: int) -> None:
        m = state["month"] + delta
        y = state["year"]
        while m > 12:
            m -= 12
            y += 1
        while m < 1:
            m += 12
            y -= 1
        state["month"] = m
        state["year"] = y
        _refresh()

    ctk.CTkButton(
        header, text="◀", width=32, height=28,
        command=lambda: _change_month(-1),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left")
    month_label = ctk.CTkLabel(
        header, text="", font=ctk.CTkFont(size=13, weight="bold"),
    )
    month_label.pack(side="left", expand=True)
    ctk.CTkButton(
        header, text="▶", width=32, height=28,
        command=lambda: _change_month(1),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="right")

    # Day grid
    grid = ctk.CTkFrame(dialog, fg_color="transparent")
    grid.pack(padx=8, pady=4)
    # Sunday-first matches the US convention WGU students likely
    # use. (Tk's calendar.firstweekday=6 starts on Sunday.)
    dow_labels = ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")
    for i, d in enumerate(dow_labels):
        ctk.CTkLabel(
            grid, text=d, width=32, anchor="center",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray40", "gray70"),
        ).grid(row=0, column=i, padx=1, pady=1)

    day_buttons: list[ctk.CTkButton] = []

    def _select(day: int) -> None:
        state["selected"] = _date(state["year"], state["month"], day)
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def _refresh() -> None:
        month_label.configure(
            text=_date(state["year"], state["month"], 1).strftime("%B %Y")
        )
        for b in day_buttons:
            try: b.destroy()
            except Exception: pass
        day_buttons.clear()
        cal_iter = _cal.Calendar(firstweekday=6).monthdayscalendar(
            state["year"], state["month"],
        )
        for row_idx, week in enumerate(cal_iter, start=1):
            for col_idx, day in enumerate(week):
                if day == 0:
                    continue
                btn_kwargs = dict(SECONDARY_BTN_KWARGS)
                # Highlight today in the primary accent color so it's
                # easy to find on the grid.
                if (state["year"], state["month"], day) == (
                    today.year, today.month, today.day
                ):
                    btn_kwargs.pop("fg_color", None)
                    btn_kwargs.pop("text_color", None)
                btn = ctk.CTkButton(
                    grid, text=str(day), width=32, height=28,
                    command=lambda d=day: _select(d),
                    font=ctk.CTkFont(size=11),
                    **btn_kwargs,
                )
                btn.grid(row=row_idx, column=col_idx, padx=1, pady=1)
                day_buttons.append(btn)

    # Bottom: jump-to-today + cancel.
    bottom = ctk.CTkFrame(dialog, fg_color="transparent")
    bottom.pack(fill="x", padx=8, pady=(4, 8))

    def _jump_today() -> None:
        state["year"] = today.year
        state["month"] = today.month
        _refresh()

    ctk.CTkButton(
        bottom, text="Today", width=70, height=26,
        command=_jump_today, **SECONDARY_BTN_KWARGS,
    ).pack(side="left")
    ctk.CTkButton(
        bottom, text="Cancel", width=70, height=26,
        command=lambda: (
            dialog.grab_release() if dialog.winfo_exists() else None,
            dialog.destroy() if dialog.winfo_exists() else None,
        ),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="right")

    dialog.bind("<Escape>", lambda _e: dialog.destroy())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    _refresh()
    parent.wait_window(dialog)
    return state["selected"]


def prompt_find_and_pick(
    parent,
    do_search: Callable[[str], list[str]],
) -> Optional[str]:
    """Combined find-and-pick dialog: search entry on top, results list
    below. Workflow: user types query → Enter → results appear below;
    user can retype to refine, OR click a name to commit. Returns the
    selected name, or None on cancel.

    `do_search(query)` runs on the main thread but is expected to
    block (via wait_variable inside) while the worker performs the
    actual search. Returns the list of matching names (exact tiers
    first, then fuzzy fallback as the worker decides).

    The dialog reopens at its last on-screen size/position within the
    session (key 'find_and_pick')."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Find student")
    _restore_dialog_geometry(dialog, "find_and_pick")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    result: dict = {"value": None}

    ctk.CTkLabel(
        dialog,
        text="Type the student's name and press Enter. Matches appear below.",
        justify="left",
    ).pack(padx=12, pady=(12, 4), anchor="w")

    entry = ctk.CTkEntry(dialog, placeholder_text="e.g. Joshua Jacobs")
    entry.pack(fill="x", padx=12, pady=(0, 6))
    entry.focus_force()
    dialog.after(50, entry.focus_force)

    results_frame = ctk.CTkScrollableFrame(dialog, label_text="Matches")
    results_frame.pack(fill="both", expand=True, padx=12, pady=4)

    current_widgets: list = []
    searching = {"in_flight": False}
    pending_cancel = {"value": False}

    def alive() -> bool:
        try:
            return bool(dialog.winfo_exists())
        except Exception:
            return False

    def clear_results() -> None:
        for w in current_widgets:
            try:
                w.destroy()
            except Exception:
                pass
        current_widgets.clear()

    def populate(names: list[str], query: str) -> None:
        if not alive():
            return
        clear_results()
        if not names:
            lbl = ctk.CTkLabel(
                results_frame,
                text=(
                    f"No matches for {query!r}. Try a different "
                    "spelling, or use the full name."
                ),
                anchor="w", justify="left",
            )
            lbl.pack(fill="x", padx=4, pady=8)
            current_widgets.append(lbl)
            return
        for n in names:
            btn = ctk.CTkButton(
                results_frame, text=n, anchor="w", height=32,
                command=lambda nm=n: finish(nm),
            )
            btn.pack(fill="x", pady=2)
            current_widgets.append(btn)

    def run_search(_event=None):
        if searching["in_flight"]:
            return
        query = entry.get().strip()
        if not query:
            return
        searching["in_flight"] = True
        clear_results()
        msg = ctk.CTkLabel(
            results_frame, text=f"Searching for {query!r}…",
            anchor="w", justify="left",
        )
        msg.pack(fill="x", padx=4, pady=8)
        current_widgets.append(msg)
        # Disable the entry so a second Enter while searching can't
        # stack a second wait_variable on top of the first.
        try:
            entry.configure(state="disabled")
        except Exception:
            pass
        dialog.update_idletasks()
        try:
            names = do_search(query)
        finally:
            searching["in_flight"] = False
            if alive():
                try:
                    entry.configure(state="normal")
                    entry.focus_force()
                except Exception:
                    pass
        if pending_cancel["value"]:
            finish(None)
            return
        populate(names, query)

    def finish(name: Optional[str]) -> None:
        result["value"] = name
        _save_dialog_geometry(dialog, "find_and_pick")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def cancel(_event=None) -> None:
        # Cancel during an in-flight search defers the close until the
        # worker reports back — we can't kill the search mid-flight.
        if searching["in_flight"]:
            pending_cancel["value"] = True
            return
        finish(None)

    entry.bind("<Return>", run_search)
    dialog.bind("<Escape>", cancel)
    dialog.protocol("WM_DELETE_WINDOW", cancel)

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(pady=(4, 10))
    ctk.CTkButton(btn_row, text="Search", command=run_search, width=110).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", command=cancel, width=90,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    parent.wait_window(dialog)
    return result["value"]


def prompt_quick_note(parent, *, default_type: str = "Admin Note",
                      student_name: str = "", course_code: str = "",
                      note_templates=None, on_manage_templates=None):
    """Quick-note dialog — mirrors the 'Note' action's core fields (interaction
    format, type, academic activities, subject, body) so it feels familiar.
    Files an ad-hoc note for the selected student. Returns a dict
    {interaction_format, interaction_type, subject, body, activities} or None.

    When `note_templates` is given, a 'Choose template ▾' button lets the user
    fill a saved template into the body — the easy 'file a templated note now'
    entry for the silent-fire paths (no configured edit-at-fire note needed)."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Quick note — {student_name}" if student_name else "Quick note")
    dialog.geometry("520x600")
    try:
        dialog.transient(parent)
        dialog.attributes("-topmost", True)
        dialog.after(120, lambda: (dialog.lift(), dialog.focus_force()))
    except Exception:
        pass
    result = {"value": None}

    body_frame = ctk.CTkScrollableFrame(dialog, fg_color="transparent")
    body_frame.pack(fill="both", expand=True, padx=12, pady=(10, 4))
    # Course code: always pre-filled (from the caseload row / active ACI) but
    # editable so the user can override it for this note.
    course_row = ctk.CTkFrame(body_frame, fg_color="transparent")
    course_row.pack(fill="x", pady=(0, 6))
    ctk.CTkLabel(course_row, text="Course:").pack(side="left", padx=(0, 8))
    course_entry = ctk.CTkEntry(course_row, width=120,
                                placeholder_text="course code")
    course_entry.pack(side="left")
    if course_code:
        course_entry.insert(0, course_code)

    # Optional: fill this note from a saved template (picks + fills, drops the
    # rendered text into the body and presets the note type if the template
    # names one). Only shown when templates exist.
    if note_templates:
        tpl_row = ctk.CTkFrame(body_frame, fg_color="transparent")
        tpl_row.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(tpl_row, text="Template:").pack(side="left", padx=(0, 8))

        def _choose_template():
            tmpl = pick_note_template(dialog, note_templates,
                                      course_entry.get().strip(),
                                      manage=on_manage_templates)
            if not tmpl:
                return
            filled = prompt_fill_note_template(dialog, tmpl)
            if filled is None:
                return
            body_box.delete("1.0", "end")
            body_box.insert("1.0", filled)
            if (tmpl.note_type
                    and tmpl.note_type in types_for_format(fmt_var.get())):
                type_var.set(tmpl.note_type)
                _refresh()

        ctk.CTkButton(tpl_row, text="Choose template ▾",
                      command=_choose_template, width=170,
                      **SECONDARY_BTN_KWARGS).pack(side="left")

    fmt_var = ctk.StringVar(value="Single Interaction")
    fmt_row = ctk.CTkFrame(body_frame, fg_color="transparent")
    fmt_row.pack(fill="x", pady=(0, 6))
    ctk.CTkLabel(fmt_row, text="Format:").pack(side="left", padx=(0, 8))
    for f in INTERACTION_FORMATS:
        ctk.CTkRadioButton(fmt_row, text=f, variable=fmt_var, value=f,
                           command=lambda: _refresh()).pack(side="left", padx=6)

    ctk.CTkLabel(body_frame, text="Type:").pack(anchor="w")
    type_var = ctk.StringVar(value=default_type)
    type_menu = ctk.CTkComboBox(body_frame, variable=type_var, width=340,
                                state="readonly",
                                values=types_for_format(fmt_var.get()),
                                command=lambda _v=None: _refresh())
    type_menu.pack(anchor="w", pady=(0, 8))

    ctk.CTkLabel(body_frame, text="Academic activities:").pack(anchor="w")
    act_frame = ctk.CTkFrame(body_frame, fg_color="transparent")
    act_frame.pack(fill="x", pady=(0, 8))
    activity_vars: dict = {}
    activity_cbs: list = []
    for lbl in ACADEMIC_ACTIVITY_LABELS:
        v = ctk.BooleanVar(value=False)
        cb = ctk.CTkCheckBox(act_frame, text=lbl, variable=v)
        cb.pack(anchor="w", pady=1)
        activity_vars[lbl] = v
        activity_cbs.append(cb)

    ctk.CTkLabel(body_frame, text="Subject (optional):").pack(anchor="w")
    subject_entry = ctk.CTkEntry(body_frame, width=440,
                                 placeholder_text="(defaults to the note type)")
    subject_entry.pack(anchor="w", pady=(0, 8))

    ctk.CTkLabel(body_frame, text="Note:").pack(anchor="w")
    body_box = ctk.CTkTextbox(body_frame, height=150, wrap="word")
    body_box.pack(fill="x", pady=(0, 4))

    def _refresh(*_a):
        types = types_for_format(fmt_var.get())
        type_menu.configure(values=types)
        if type_var.get() not in types:
            type_var.set(types[0])
        disabled = activities_disabled_for(fmt_var.get(), type_var.get())
        for cb in activity_cbs:
            cb.configure(state="disabled" if disabled else "normal")
        if disabled:
            for v in activity_vars.values():
                v.set(False)
    _refresh()

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=12, pady=(0, 12))

    def _file():
        body = body_box.get("1.0", "end").strip()
        if not body:
            from tkinter import messagebox
            if not messagebox.askyesno(
                    "Empty note", "The note body is empty. File it anyway?",
                    parent=dialog):
                return
        acts = ([lbl for lbl, v in activity_vars.items() if v.get()]
                if not activities_disabled_for(fmt_var.get(), type_var.get())
                else [])
        result["value"] = {
            "interaction_format": fmt_var.get(),
            "interaction_type": type_var.get(),
            "course_code": course_entry.get().strip(),
            "subject": subject_entry.get().strip(),
            "body": body, "activities": acts,
        }
        try:
            dialog.destroy()
        except Exception:
            pass

    ctk.CTkButton(btn_row, text="File note", command=_file,
                  fg_color=_ADD_BTN_BLUE,
                  hover_color=_ADD_BTN_BLUE_HOVER).pack(side="left")
    ctk.CTkButton(btn_row, text="Cancel", command=dialog.destroy,
                  **SECONDARY_BTN_KWARGS).pack(side="left", padx=8)
    dialog.bind("<Escape>", lambda _e: dialog.destroy())
    body_box.bind("<Control-Return>", lambda _e: (_file(), "break"))
    # Open near the pointer (up-left of it), clamped fully on-screen, so the
    # popup lands where the ＋ Note button was clicked rather than screen-center.
    _fit_dialog_to_content(dialog, min_w=520, min_h=600, near_mouse=True)
    parent.wait_window(dialog)
    return result["value"]


def prompt_additional_text(parent, label: str, prefilled: str,
                           enter_submits: bool = True,
                           note_templates=None, course: str = "",
                           on_manage_templates=None,
                           default_template=None) -> Optional[str]:
    """Blocking modal: multi-line edit of a note body, pre-filled.
    Returns the new body (no strip), or None if cancelled. When
    `enter_submits` (the default), Enter submits and Shift+Enter inserts a
    newline; when False, Enter inserts a newline and only the button submits.
    Esc always cancels.

    When `note_templates` is given, a 'Choose template ▾' button appears above
    the body — picking one opens the fill form and drops the rendered text into
    the body (used for the batch 'fill once, apply to all' path). `course` seeds
    the picker's course auto-suggest.

    Pre-fill rule: if the body doesn't already end in whitespace, a
    single trailing space is added so the user can start typing
    immediately without manually inserting a separator. The cursor
    is placed at end. Last on-screen position is remembered for the
    rest of the session."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Edit body — {label}")
    _restore_dialog_geometry(dialog, "additional_text")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    result: dict = {"value": None}

    hint = ("Enter = submit · Shift+Enter = newline · Esc = cancel"
            if enter_submits else "Esc = cancel")
    ctk.CTkLabel(
        dialog,
        text=f"{label}: edit or add to the body.  {hint}",
        justify="left",
    ).pack(padx=12, pady=(10, 4), anchor="w")

    text_box = ctk.CTkTextbox(dialog, wrap="word")
    text_box.pack(fill="both", expand=True, padx=12, pady=4)
    content = prefilled
    if content and content[-1] not in (" ", "\n", "\t"):
        content += " "
    text_box.insert("1.0", content)
    text_box.focus_force()
    dialog.after(50, text_box.focus_force)
    text_box.mark_set("insert", "end-1c")

    if note_templates or default_template is not None:
        def _apply_template_fill(tmpl):
            filled = prompt_fill_note_template(dialog, tmpl)
            if filled is None:
                return
            text_box.delete("1.0", "end")
            text_box.insert("1.0", filled)
            text_box.mark_set("insert", "end-1c")

    if note_templates:
        tpl_row = ctk.CTkFrame(dialog, fg_color="transparent")
        tpl_row.pack(fill="x", padx=12, pady=(0, 2), before=text_box)
        ctk.CTkLabel(tpl_row, text="Template:", anchor="w").pack(side="left")

        def _choose_template():
            tmpl = pick_note_template(dialog, note_templates, course,
                                      manage=on_manage_templates)
            if tmpl:
                _apply_template_fill(tmpl)

        ctk.CTkButton(
            tpl_row, text="Choose template ▾", command=_choose_template,
            width=170, **SECONDARY_BTN_KWARGS).pack(side="left", padx=(6, 0))

    def submit(_event=None):
        result["value"] = text_box.get("1.0", "end-1c")
        _save_dialog_geometry(dialog, "additional_text")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass
        return "break"

    def cancel(_event=None):
        _save_dialog_geometry(dialog, "additional_text")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass
        return "break"

    def insert_newline(_event):
        text_box.insert("insert", "\n")
        return "break"

    if enter_submits:
        text_box.bind("<Return>", submit)
        text_box.bind("<Shift-Return>", insert_newline)
    dialog.bind("<Escape>", cancel)
    dialog.protocol("WM_DELETE_WINDOW", cancel)

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(pady=(4, 10))
    ctk.CTkButton(btn_row, text="Submit", command=submit, width=110).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", command=cancel, width=90,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    # A bound default template pre-opens its fill form (Cancel it to free-type).
    if default_template is not None:
        dialog.after(60, lambda: _apply_template_fill(default_template))
    parent.wait_window(dialog)
    return result["value"]


def pick_note_template(parent, templates, course="", manage=None):
    """Small popup: choose a NoteTemplate to fill. Templates whose `courses` is
    empty (apply to any course) or include `course` are SUGGESTED at the top; a
    'Show all…' reveals the rest. Returns the chosen NoteTemplate or None.

    When `manage` is given (a callable `manage(start_new: bool, parent) -> list`
    that opens the template editor and returns the refreshed list), '＋ New' and
    '✎ Edit…' buttons appear and the picker updates in place after editing.
    Opens near the mouse and restores the parent's grab on close."""
    templates = list(templates or [])
    if not templates and manage is None:
        return None
    cu = (course or "").strip().upper()

    def _matches(t):
        return (not t.courses) or (cu and cu in [c.upper() for c in t.courses])

    dialog = ctk.CTkToplevel(parent)
    dialog.title("Choose a note template")
    try:
        _px, _py = dialog.winfo_pointerxy()
        dialog.geometry(f"+{max(_px - 30, 0)}+{max(_py - 30, 0)}")
    except Exception:
        pass
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(50, lambda: (dialog.lift(), dialog.focus_force()))
    res = {"value": None}
    state = {"show_all": False}

    def _close():
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass
        try: parent.grab_set()
        except Exception: pass

    def _choose(t):
        res["value"] = t
        _close()

    header = ctk.CTkLabel(
        dialog, text="Templates:", anchor="w",
        font=ctk.CTkFont(size=12, weight="bold"))
    header.pack(fill="x", padx=12, pady=(10, 2))

    listframe = ctk.CTkScrollableFrame(dialog, width=360, height=260)
    listframe.pack(fill="both", expand=True, padx=8, pady=(0, 4))

    def _button(t):
        sub = f"   ·  {t.interaction_type}" if t.interaction_type else ""
        crs = f"   [{', '.join(t.courses)}]" if t.courses else ""
        ctk.CTkButton(
            listframe, text=f"{t.name}{sub}{crs}", anchor="w",
            command=lambda tt=t: _choose(tt), **SECONDARY_BTN_KWARGS,
        ).pack(fill="x", padx=4, pady=2)

    def redraw():
        for w in listframe.winfo_children():
            w.destroy()
        suggested = [t for t in templates if _matches(t)]
        sug_ids = {id(t) for t in suggested}
        others = [t for t in templates if id(t) not in sug_ids]
        header.configure(text=(f"Suggested for {cu}:" if (cu and suggested)
                               else "Templates:"))
        if not templates:
            ctk.CTkLabel(
                listframe, text="No templates yet — click ＋ New below.",
                text_color=("gray40", "gray65"),
            ).pack(fill="x", padx=6, pady=20)
            return
        for t in suggested:
            _button(t)
        show_all = state["show_all"] or not suggested
        if others and show_all:
            if suggested:
                ctk.CTkLabel(
                    listframe, text="Other templates:", anchor="w",
                    text_color=("gray40", "gray65"),
                ).pack(fill="x", padx=6, pady=(6, 0))
            for t in others:
                _button(t)
        elif others:
            ctk.CTkButton(
                listframe, text=f"Show all ({len(others)} more)…", anchor="w",
                command=lambda: (state.update(show_all=True), redraw()),
                **SECONDARY_BTN_KWARGS,
            ).pack(fill="x", padx=4, pady=(6, 2))

    def _manage(start_new):
        nonlocal templates
        try:
            fresh = manage(bool(start_new), dialog)
        except Exception:
            fresh = templates
        templates = list(fresh or [])
        redraw()

    redraw()

    ctrl = ctk.CTkFrame(dialog, fg_color="transparent")
    ctrl.pack(fill="x", padx=10, pady=(2, 10))
    if manage is not None:
        ctk.CTkButton(ctrl, text="＋ New", width=80,
                      command=lambda: _manage(True),
                      **SECONDARY_BTN_KWARGS).pack(side="left")
        ctk.CTkButton(ctrl, text="✎ Edit…", width=80,
                      command=lambda: _manage(False),
                      **SECONDARY_BTN_KWARGS).pack(side="left", padx=(6, 0))
    ctk.CTkButton(ctrl, text="Cancel", command=_close, width=90,
                  **SECONDARY_BTN_KWARGS).pack(side="right")
    dialog.bind("<Escape>", lambda _e: _close())
    dialog.protocol("WM_DELETE_WINDOW", _close)
    _fit_dialog_to_content(dialog, min_w=380, near_mouse=True)
    parent.wait_window(dialog)
    return res["value"]


def prompt_fill_note_template(parent, template, prefill=None):
    """Tab-through fill form for a NoteTemplate: each field renders as its widget
    (text = entry, multiline = box, dropdown = editable combobox seeded with the
    field's choices), pre-filled with its default (or `prefill[label]` when
    given). Tab / Shift-Tab move between fields. Returns the rendered note body,
    or None if cancelled. Restores the parent's grab on close."""
    prefill = prefill or {}
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Fill note — {template.name}")
    try:
        _px, _py = dialog.winfo_pointerxy()
        dialog.geometry(f"+{max(_px - 30, 0)}+{max(_py - 30, 0)}")
    except Exception:
        pass
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(50, lambda: (dialog.lift(), dialog.focus_force()))
    res = {"value": None}

    ctk.CTkLabel(
        dialog, text="Tab between fields · Ctrl+Enter or Continue to finish · "
        "Esc = cancel", anchor="w", text_color=("gray35", "gray70"),
    ).pack(fill="x", padx=12, pady=(10, 2))

    form = ctk.CTkScrollableFrame(dialog, width=480, height=340)
    form.pack(fill="both", expand=True, padx=8, pady=(0, 4))

    rows = []  # (field, widget, kind)
    for f in template.fields:
        seed = prefill.get(f.label, f.default)
        ctk.CTkLabel(
            form, text=f"{f.label}:", anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(fill="x", padx=6, pady=(6, 0))
        if f.kind == "multiline":
            w = ctk.CTkTextbox(form, wrap="word", height=70)
            w.pack(fill="x", padx=6, pady=(0, 2))
            if seed:
                w.insert("1.0", seed)
        elif f.kind == "dropdown":
            w = ctk.CTkComboBox(form, values=list(f.choices))
            w.pack(fill="x", padx=6, pady=(0, 2))
            w.set(seed or "")
        elif f.kind == "date":
            # Editable combobox (choices = relative quick-picks like "1 week")
            # PLUS a 📅 button that drops in a concrete MM/DD/YYYY date.
            drow = ctk.CTkFrame(form, fg_color="transparent")
            drow.pack(fill="x", padx=6, pady=(0, 2))
            w = ctk.CTkComboBox(drow, values=list(f.choices))
            w.pack(side="left", fill="x", expand=True)
            w.set(seed or "")

            def _pick_date(cb=w):
                d = prompt_calendar_pick(dialog)
                if d:
                    cb.set(d.strftime("%m/%d/%Y"))

            ctk.CTkButton(drow, text="📅", width=40, command=_pick_date,
                          **SECONDARY_BTN_KWARGS).pack(side="left", padx=(4, 0))
        else:
            w = ctk.CTkEntry(form)
            w.pack(fill="x", padx=6, pady=(0, 2))
            if seed:
                w.insert(0, seed)
        rows.append((f, w, f.kind))

    widgets = [w for (_f, w, _k) in rows]

    def _focus(delta, ix):
        if not widgets:
            return "break"
        j = (ix + delta) % len(widgets)
        try:
            widgets[j].focus_set()
        except Exception:
            pass
        return "break"

    for ix, w in enumerate(widgets):
        w.bind("<Tab>", lambda _e, i=ix: _focus(1, i))
        w.bind("<Shift-Tab>", lambda _e, i=ix: _focus(-1, i))

    def _val(w, kind):
        if kind == "multiline":
            return w.get("1.0", "end-1c")
        return w.get()

    def _close():
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass
        try: parent.grab_set()
        except Exception: pass

    def _ok(_e=None):
        values = {f.label: _val(w, kind) for (f, w, kind) in rows}
        res["value"] = render_note_template(template, values)
        _close()

    def _cancel(_e=None):
        _close()

    btns = ctk.CTkFrame(dialog, fg_color="transparent")
    btns.pack(pady=(2, 10))
    ctk.CTkButton(btns, text="Continue", command=_ok, width=110).pack(
        side="left", padx=4)
    ctk.CTkButton(btns, text="Cancel", command=_cancel, width=90,
                  **SECONDARY_BTN_KWARGS).pack(side="left", padx=4)
    dialog.bind("<Escape>", _cancel)
    dialog.bind("<Control-Return>", _ok)
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    if widgets:
        dialog.after(60, lambda: widgets[0].focus_set())
    _fit_dialog_to_content(dialog, min_w=500, near_mouse=True)
    parent.wait_window(dialog)
    return res["value"]


def prompt_edit_note(parent, label, body_prefill, course_default,
                     activities_on, eas, enter_submits: bool = True,
                     interaction_type: str = "",
                     interaction_format: str = "Single Interaction",
                     subject_default: str = "", note_templates=None,
                     on_manage_templates=None, default_template=None):
    """Unified fire-time note dialog: edit the body, subject, course code,
    note type, and academic activities, and — when the student has open
    Essential Actions — attach/close one. Returns
    {body, subject, course, type, activities, ea} (ea = (reason, course,
    close) or None) or None if cancelled.

    The Note type dropdown defaults to the action's `interaction_type`;
    picking a type that doesn't take academic activities (e.g. "Email to
    Student", "Admin Note") disables the activity checkboxes.

    When `enter_submits` (the default), Enter in the body submits the note
    and Shift+Enter inserts a newline; when False, Enter inserts a newline
    and the note is submitted only via the Continue button."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Edit note — {label}")
    # Open near the mouse (not where it was last). The final size + on-screen
    # clamp is applied after the content is built, keeping it near the cursor.
    try:
        _px, _py = dialog.winfo_pointerxy()
        dialog.geometry(f"+{max(_px - 30, 0)}+{max(_py - 30, 0)}")
    except Exception:
        pass
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(50, lambda: (dialog.lift(), dialog.focus_force()))
    res = {"value": None}

    ctk.CTkLabel(
        dialog, text="Edit this note before it's filed.  Esc = cancel.",
        anchor="w", text_color=("gray35", "gray70"),
    ).pack(fill="x", padx=12, pady=(10, 2))

    crow = ctk.CTkFrame(dialog, fg_color="transparent")
    crow.pack(fill="x", padx=12, pady=(2, 2))
    ctk.CTkLabel(crow, text="Course code:", width=90, anchor="w").pack(side="left")
    course_entry = ctk.CTkEntry(crow, width=160)
    course_entry.pack(side="left")
    if course_default:
        course_entry.insert(0, course_default)

    # Note type — defaults to the action's type; drives whether the academic
    # activity checkboxes below are usable for this type.
    fmt = interaction_format or "Single Interaction"
    type_choices = types_for_format(fmt)
    trow = ctk.CTkFrame(dialog, fg_color="transparent")
    trow.pack(fill="x", padx=12, pady=(2, 2))
    ctk.CTkLabel(trow, text="Note type:", width=90, anchor="w").pack(side="left")
    type_var = ctk.StringVar(value=interaction_type or "")
    type_combo = ctk.CTkComboBox(
        trow, values=type_choices, variable=type_var, state="readonly",
        width=260, command=lambda _v=None: _sync_activities(),
    )
    type_combo.pack(side="left")

    # Subject — Salesforce's note Subject line.
    srow = ctk.CTkFrame(dialog, fg_color="transparent")
    srow.pack(fill="x", padx=12, pady=(2, 2))
    ctk.CTkLabel(srow, text="Subject:", width=90, anchor="w").pack(side="left")
    subject_entry = ctk.CTkEntry(srow)
    subject_entry.pack(side="left", fill="x", expand=True)
    if subject_default:
        subject_entry.insert(0, subject_default)

    # Optional: fill the body from a saved note template. Only shown when the
    # user has templates; picking one opens a tab-through fill form and drops
    # the rendered text into the body (and presets the note type if the template
    # names one). Normal free-typing is unaffected.
    if note_templates or default_template is not None:
        def _apply_template_fill(tmpl):
            filled = prompt_fill_note_template(dialog, tmpl)
            if filled is None:
                return
            text_box.delete("1.0", "end")
            text_box.insert("1.0", filled)
            text_box.mark_set("insert", "end-1c")
            if tmpl.note_type and tmpl.note_type in type_choices:
                type_var.set(tmpl.note_type)
                _sync_activities()

    if note_templates:
        tmpl_row = ctk.CTkFrame(dialog, fg_color="transparent")
        tmpl_row.pack(fill="x", padx=12, pady=(2, 2))
        ctk.CTkLabel(tmpl_row, text="Template:", width=90, anchor="w").pack(
            side="left")

        def _choose_template(_e=None):
            tmpl = pick_note_template(
                dialog, note_templates, course_entry.get().strip(),
                manage=on_manage_templates)
            if tmpl:
                _apply_template_fill(tmpl)

        ctk.CTkButton(
            tmpl_row, text="Choose template ▾", command=_choose_template,
            width=170, **SECONDARY_BTN_KWARGS).pack(side="left")
        ctk.CTkLabel(
            tmpl_row, text="  fills the note body below",
            text_color=("gray40", "gray65")).pack(side="left")

    ctk.CTkLabel(dialog, text="Note body:", anchor="w").pack(
        fill="x", padx=12, pady=(6, 0))
    text_box = ctk.CTkTextbox(dialog, wrap="word", height=150)
    text_box.pack(fill="both", expand=True, padx=12, pady=(0, 0))
    c = body_prefill or ""
    if c and c[-1] not in (" ", "\n", "\t"):
        c += " "
    text_box.insert("1.0", c)
    text_box.mark_set("insert", "end-1c")
    if enter_submits:
        ctk.CTkLabel(
            dialog, text="Press Shift+Enter for a new line · Enter submits",
            anchor="w", font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
        ).pack(fill="x", padx=12, pady=(1, 4))

    act_hdr = ctk.CTkLabel(dialog, text="Academic activities:", anchor="w")
    act_hdr.pack(fill="x", padx=12, pady=(6, 0))
    act_frame = ctk.CTkFrame(dialog, fg_color=("gray95", "gray18"))
    act_frame.pack(fill="x", padx=12, pady=(0, 4))
    act_vars = {}
    act_boxes = []
    for lbl in ACADEMIC_ACTIVITY_LABELS:
        v = ctk.BooleanVar(value=(lbl in (activities_on or [])))
        act_vars[lbl] = v
        cb = ctk.CTkCheckBox(
            act_frame, text=lbl, variable=v, font=ctk.CTkFont(size=11),
        )
        cb.pack(anchor="w", padx=8, pady=1)
        act_boxes.append(cb)

    def _sync_activities(_v=None) -> None:
        """Enable/disable the academic-activity checkboxes for the selected
        note type — types like 'Email to Student' / 'Admin Note' don't take
        them (matching the live Caseload form), so they're grayed + cleared."""
        disabled = activities_disabled_for(fmt, type_var.get())
        for cb in act_boxes:
            try:
                if disabled:
                    cb.deselect()
                    cb.configure(state="disabled")
                else:
                    cb.configure(state="normal")
            except Exception:
                pass
        act_hdr.configure(
            text=("Academic activities:  (not used for this note type)"
                  if disabled else "Academic activities:"))

    _sync_activities()  # apply the initial state for the action's type

    ea_sel = ctk.StringVar(value="skip")
    ea_close = ctk.BooleanVar(value=False)
    if eas:
        ctk.CTkLabel(
            dialog, text=f"Essential Actions ({len(eas)} open):", anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(fill="x", padx=12, pady=(6, 0))
        eabox = ctk.CTkFrame(dialog, fg_color=("gray95", "gray18"))
        eabox.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkRadioButton(
            eabox, text="Don't attach", variable=ea_sel, value="skip",
        ).pack(anchor="w", padx=8, pady=1)
        for i, ea in enumerate(eas):
            t = ea.get("reason", "")
            if ea.get("course"):
                t += f"   ({ea['course']})"
            ctk.CTkRadioButton(
                eabox, text=t, variable=ea_sel, value=str(i),
            ).pack(anchor="w", padx=8, pady=1)
        ctk.CTkCheckBox(
            dialog, text="Close the Essential Action when the note is saved",
            variable=ea_close, font=ctk.CTkFont(size=11),
        ).pack(anchor="w", padx=12, pady=(0, 4))

    def _cont(_e=None):
        ea_choice = None
        v = ea_sel.get()
        if eas and v != "skip":
            ea = eas[int(v)]
            ea_choice = (ea.get("reason", ""), ea.get("course", ""),
                         bool(ea_close.get()))
        chosen_type = type_var.get().strip()
        # Never send activities for a type that doesn't take them.
        acts = ([] if activities_disabled_for(fmt, chosen_type)
                else [l for l, vv in act_vars.items() if vv.get()])
        res["value"] = {
            "body": text_box.get("1.0", "end-1c"),
            "subject": subject_entry.get().strip(),
            "course": course_entry.get().strip(),
            "type": chosen_type,
            "activities": acts,
            "ea": ea_choice,
        }
        _close()

    def _cancel(_e=None):
        _close()

    def _close():
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(pady=(2, 10))
    ctk.CTkButton(btn_row, text="Continue", command=_cont, width=110).pack(
        side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", command=_cancel, width=90,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)
    dialog.bind("<Escape>", _cancel)
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    # Enter in the body submits, Shift+Enter inserts a newline (default).
    if enter_submits:
        def _newline(_e=None):
            text_box.insert("insert", "\n")
            return "break"
        text_box.bind("<Return>", lambda _e: (_cont(), "break")[1])
        text_box.bind("<Shift-Return>", _newline)
    # Size to fit the whole form (the Academic Activities + Essential Actions
    # blocks and the buttons sit at the bottom and were getting clipped), and
    # place it near the mouse.
    _fit_dialog_to_content(dialog, min_w=480, near_mouse=True)
    # A note with a bound default template pre-opens its fill form so the user
    # fills it straight away (they can Cancel that to free-type instead).
    if default_template is not None:
        dialog.after(60, lambda: _apply_template_fill(default_template))
    parent.wait_window(dialog)
    return res["value"]


def prompt_text_review(
    parent, *, who: str, mobile: str, inbox_label: str, when_str: str,
    body: str, char_limit: int, scheduled: bool,
) -> Optional[str]:
    """In-app review/edit for a single outgoing text — the texting equivalent of
    the email previewer. Shows recipient / inbox / scheduled time and lets the
    user edit the message. Returns the (possibly edited) body to send, or None
    on cancel. The caller then drives Mongoose to completion (no manual clicks
    in Mongoose)."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Review text")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(50, lambda: (dialog.lift(), dialog.focus_force()))
    res = {"value": None}

    header = "To:  " + who + (f"   ·   {mobile}" if mobile else "")
    if inbox_label:
        header += f"\nInbox:  {inbox_label}"
    header += f"\n{'Schedule' if scheduled else 'Send'}:  {when_str}"
    ctk.CTkLabel(
        dialog, text=header, justify="left", anchor="w",
        font=ctk.CTkFont(size=12),
    ).pack(fill="x", padx=12, pady=(12, 6))

    ctk.CTkLabel(dialog, text="Message:", anchor="w").pack(
        fill="x", padx=12, pady=(2, 0))
    box = ctk.CTkTextbox(dialog, wrap="word", height=150, width=460)
    box.pack(fill="both", expand=True, padx=12, pady=(0, 2))
    box.insert("1.0", body or "")
    box.mark_set("insert", "end-1c")

    count = ctk.CTkLabel(
        dialog, text="", anchor="e", font=ctk.CTkFont(size=11),
        text_color=("gray40", "gray70"))
    count.pack(fill="x", padx=12, pady=(0, 4))

    def _update_count(_e=None):
        n = len(box.get("1.0", "end-1c"))
        over = n - char_limit
        count.configure(
            text=f"{n}/{char_limit}" + (f"  ({over} over — will be trimmed)"
                                        if over > 0 else ""),
            text_color=("#d11" if over > 0 else ("gray40", "gray70")),
        )

    box.bind("<KeyRelease>", _update_count)
    _update_count()

    def _close():
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def _send(_e=None):
        res["value"] = box.get("1.0", "end-1c")
        _close()

    def _cancel(_e=None):
        _close()

    btns = ctk.CTkFrame(dialog, fg_color="transparent")
    btns.pack(pady=(2, 10))
    ctk.CTkButton(
        btns, text=("Schedule" if scheduled else "Send"),
        command=_send, width=120,
    ).pack(side="left", padx=4)
    ctk.CTkButton(
        btns, text="Cancel", command=_cancel, width=90, **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)
    dialog.bind("<Escape>", _cancel)
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    parent.wait_window(dialog)
    return res["value"]


def ask_yes_no_topmost(
    parent, title: str, message: str,
    yes_label: str = "Yes", no_label: str = "No",
    at: Optional[tuple] = None,
) -> bool:
    """Topmost Yes/No modal. Use AFTER Outlook (or any other window)
    has stolen focus — tkinter's stock messagebox.askyesno doesn't
    have topmost / focus-force handling, so its dialog can open
    BEHIND Outlook and look like the app hung (the user can't see
    where the question is waiting). This variant uses the same
    pattern as `prompt_additional_text` — CTkToplevel + topmost +
    repeated focus_force calls — so the question always lands in
    front of the user.

    Returns True for Yes, False for No / window-close / Esc."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    result = {"value": False}

    ctk.CTkLabel(
        dialog, text=message, justify="left", wraplength=460,
    ).pack(padx=16, pady=(14, 8), anchor="w")

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=16, pady=(4, 14))

    def _close(value: bool) -> None:
        result["value"] = value
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    yes_btn = ctk.CTkButton(
        btn_row, text=yes_label, width=100,
        command=lambda: _close(True),
    )
    yes_btn.pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text=no_label, width=100,
        command=lambda: _close(False),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    dialog.bind("<Return>", lambda _e: _close(True))
    dialog.bind("<Escape>", lambda _e: _close(False))
    dialog.protocol("WM_DELETE_WINDOW", lambda: _close(False))

    # Optionally pop the dialog right where the action was invoked (e.g.
    # over the caseload row-menu the user just clicked), clamped on-screen.
    if at is not None:
        try:
            dialog.update_idletasks()
            w = dialog.winfo_reqwidth()
            h = dialog.winfo_reqheight()
            sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
            x = max(0, min(int(at[0]) - 20, sw - w - 8))
            y = max(0, min(int(at[1]) - 10, sh - h - 8))
            dialog.geometry(f"+{x}+{y}")
        except Exception:
            pass

    # Outlook may steal focus right after compose_email returns;
    # claw it back aggressively. The two .after() retries handle the
    # case where Outlook fully renders ~100-500ms after Display().
    dialog.lift()
    dialog.focus_force()
    yes_btn.focus_set()
    dialog.after(100, lambda: (dialog.lift(), dialog.focus_force()))
    dialog.after(500, lambda: (dialog.lift(), dialog.focus_force()))

    parent.wait_window(dialog)
    return result["value"]


def prompt_mongoose_stale(parent, age_str: str) -> str:
    """Pre-fire warning that the Mongoose text-ID export is stale. Returns
    'update' (refresh it first), 'continue' (fire with the existing data), or
    'cancel' (abort the fire / window close / Esc)."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Mongoose text IDs are stale")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    result = {"value": "cancel"}

    ctk.CTkLabel(
        dialog,
        text=f"⚠  The Mongoose text-ID export is {age_str}.",
        font=ctk.CTkFont(size=14, weight="bold"),
        justify="left", wraplength=460,
    ).pack(padx=18, pady=(16, 4), anchor="w")
    ctk.CTkLabel(
        dialog,
        text=("Opt-in can change several times a day. If you fire now without "
              "updating:\n"
              "  •  students already in the export text normally;\n"
              "  •  students enrolled since the last update fall back to their "
              "Salesforce opt-in (flagged “unverified” in the review);\n"
              "  •  a student who opted out since the last export could still be "
              "texted.\n\n"
              "Updating re-exports each course segment from Mongoose "
              "(~a few seconds per course)."),
        justify="left", wraplength=460, text_color=("gray25", "gray75"),
    ).pack(padx=18, pady=(0, 12), anchor="w")

    def _close(v: str) -> None:
        result["value"] = v
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=18, pady=(0, 16))
    upd = ctk.CTkButton(btn_row, text="🔄 Update text IDs first", width=180,
                        command=lambda: _close("update"))
    upd.pack(side="left", padx=4)
    ctk.CTkButton(btn_row, text="Continue without updating", width=180,
                  command=lambda: _close("continue"),
                  **SECONDARY_BTN_KWARGS).pack(side="left", padx=4)
    ctk.CTkButton(btn_row, text="Cancel", width=90,
                  command=lambda: _close("cancel"),
                  **SECONDARY_BTN_KWARGS).pack(side="left", padx=4)
    dialog.bind("<Escape>", lambda _e: _close("cancel"))
    dialog.protocol("WM_DELETE_WINDOW", lambda: _close("cancel"))
    dialog.lift()
    dialog.focus_force()
    upd.focus_set()
    dialog.after(120, lambda: (dialog.lift(), dialog.focus_force()))
    parent.wait_window(dialog)
    return result["value"]


def prompt_column_picker(parent, sections, *, near=None):
    """Searchable, grouped column picker. `sections` is an ordered list of
    (header, items) where items is a list of (label, key) pairs. Returns the
    chosen `key` (str), or None if cancelled. A live search box filters across
    every section by label; empty sections hide while filtering."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Choose a column")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.geometry("460x520")
    result = {"key": None}

    search_var = tk.StringVar()
    ctk.CTkEntry(
        dialog, textvariable=search_var, placeholder_text="Search columns…",
    ).pack(fill="x", padx=12, pady=(12, 6))

    listwrap = ctk.CTkScrollableFrame(dialog)
    listwrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
    listwrap.grid_columnconfigure(0, weight=1)

    def _choose(key: str) -> None:
        result["key"] = key
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def _render() -> None:
        for w in listwrap.winfo_children():
            w.destroy()
        q = search_var.get().strip().lower()
        r = 0
        for header, items in sections:
            shown = [(lab, key) for (lab, key) in items
                     if not q or q in lab.lower()]
            if not shown:
                continue
            ctk.CTkLabel(
                listwrap, text=header,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=("gray35", "gray70"), anchor="w",
            ).grid(row=r, column=0, sticky="ew", padx=6, pady=(8, 2))
            r += 1
            for lab, key in shown:
                ctk.CTkButton(
                    listwrap, text=lab, anchor="w", height=28,
                    fg_color="transparent", text_color=("gray10", "gray90"),
                    hover_color=("gray85", "gray28"),
                    command=lambda k=key: _choose(k),
                ).grid(row=r, column=0, sticky="ew", padx=6, pady=1)
                r += 1
        if r == 0:
            ctk.CTkLabel(listwrap, text="No columns match.",
                         text_color=("gray45", "gray60")).grid(
                row=0, column=0, sticky="w", padx=8, pady=8)

    search_var.trace_add("write", lambda *_a: _render())
    _render()
    ctk.CTkButton(dialog, text="Cancel", command=lambda: _choose(None),
                  **SECONDARY_BTN_KWARGS).pack(pady=(0, 10))
    dialog.bind("<Escape>", lambda _e: _choose(None))
    if near is not None:
        try:
            dialog.update_idletasks()
            sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
            w, h = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
            x = max(0, min(int(near[0]) - 20, sw - w - 8))
            y = max(0, min(int(near[1]) - 10, sh - h - 8))
            dialog.geometry(f"+{x}+{y}")
        except Exception:
            pass
    dialog.lift()
    dialog.focus_force()
    parent.wait_window(dialog)
    return result["key"]


def prompt_override_selection(parent, labels, action_name):
    """Modal listing students who DON'T meet an action's filter conditions, each
    with a checkbox to 'fire on anyway'. `labels` is the ordered display list.
    Returns the list of CHECKED INDICES (into `labels`) to fire anyway, or None
    if the user cancelled the whole fire. Empty list = fire only the matches."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Some students don't match the filter")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    result = {"value": None}

    ctk.CTkLabel(
        dialog,
        text=(f"{len(labels)} selected student(s) don't meet {action_name!r}'s "
              "filter conditions.\nStudents who DO match will fire regardless. "
              "Check any below to fire on\nanyway; unchecked ones are skipped."),
        justify="left", wraplength=440,
    ).pack(padx=16, pady=(14, 6), anchor="w")

    vars_list: list = []
    sel_all_var = ctk.BooleanVar(value=False)

    def _toggle_all() -> None:
        for v in vars_list:
            v.set(sel_all_var.get())

    ctk.CTkCheckBox(
        dialog, text="Select all", variable=sel_all_var, command=_toggle_all,
    ).pack(padx=16, pady=(0, 4), anchor="w")

    scroll = ctk.CTkScrollableFrame(
        dialog, width=400, height=min(320, 30 * len(labels) + 12))
    scroll.pack(fill="both", expand=True, padx=12, pady=(0, 8))
    for lbl in labels:
        v = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(scroll, text=lbl, variable=v).pack(anchor="w", pady=1)
        vars_list.append(v)

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=16, pady=(4, 14))

    def _close(cancel: bool) -> None:
        result["value"] = (None if cancel else
                           [i for i, v in enumerate(vars_list) if v.get()])
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    cont_btn = ctk.CTkButton(
        btn_row, text="Continue", width=150, command=lambda: _close(False))
    cont_btn.pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel fire", width=110, command=lambda: _close(True),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    dialog.bind("<Escape>", lambda _e: _close(True))
    dialog.protocol("WM_DELETE_WINDOW", lambda: _close(True))
    dialog.lift()
    dialog.focus_force()
    cont_btn.focus_set()
    dialog.after(100, lambda: (dialog.lift(), dialog.focus_force()))

    parent.wait_window(dialog)
    return result["value"]


def prompt_branch_unmatched(parent, labels, branch_titles, action_name):
    """Branched-fire override: lists students who match NO branch, with a picker
    to route the CHECKED ones into a chosen branch (or skip them all). Returns
    (branch_index | None, [checked indices]) — a None index or no checks means
    skip — or None if the user cancelled the whole fire."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Some students match no branch")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    result = {"value": None}

    ctk.CTkLabel(
        dialog,
        text=(f"{len(labels)} selected student(s) don't match any branch of "
              f"{action_name!r}.\nPick a branch to send the checked ones, or "
              "leave “Skip” to fire only the matched students."),
        justify="left", wraplength=460,
    ).pack(padx=16, pady=(14, 6), anchor="w")

    _SKIP = "— Skip these students —"
    route_var = ctk.StringVar(value=_SKIP)
    ctk.CTkLabel(dialog, text="Route checked students to:").pack(
        padx=16, pady=(2, 0), anchor="w")
    ctk.CTkOptionMenu(
        dialog, values=[_SKIP] + list(branch_titles), variable=route_var,
        width=300).pack(padx=16, pady=(0, 8), anchor="w")

    vars_list: list = []
    sel_all_var = ctk.BooleanVar(value=False)

    def _toggle_all() -> None:
        for v in vars_list:
            v.set(sel_all_var.get())

    ctk.CTkCheckBox(
        dialog, text="Select all", variable=sel_all_var, command=_toggle_all,
    ).pack(padx=16, pady=(0, 4), anchor="w")

    scroll = ctk.CTkScrollableFrame(
        dialog, width=420, height=min(300, 30 * len(labels) + 12))
    scroll.pack(fill="both", expand=True, padx=12, pady=(0, 8))
    for lbl in labels:
        v = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(scroll, text=lbl, variable=v).pack(anchor="w", pady=1)
        vars_list.append(v)

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=16, pady=(4, 14))

    def _close(cancel: bool) -> None:
        if cancel:
            result["value"] = None
        else:
            sel = route_var.get()
            idx = branch_titles.index(sel) if sel in branch_titles else None
            checked = [i for i, v in enumerate(vars_list) if v.get()]
            result["value"] = (idx, checked)
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    cont_btn = ctk.CTkButton(
        btn_row, text="Continue", width=150, command=lambda: _close(False))
    cont_btn.pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel fire", width=110, command=lambda: _close(True),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    dialog.bind("<Escape>", lambda _e: _close(True))
    dialog.protocol("WM_DELETE_WINDOW", lambda: _close(True))
    dialog.lift()
    dialog.focus_force()
    cont_btn.focus_set()
    dialog.after(100, lambda: (dialog.lift(), dialog.focus_force()))

    parent.wait_window(dialog)
    return result["value"]


def prompt_segment_setup(parent, missing: list) -> bool:
    """Modal with step-by-step instructions to create the missing Mongoose
    contacts segment(s) — one per caseload department that has none yet. The
    exact segment name(s) are shown in a read-only box to copy verbatim (the
    auto-export matches the name exactly). Returns True if the user clicked
    'Re-check now' (the caller then re-runs the sync)."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Texting IDs — create Mongoose segment(s)")
    dialog.geometry("640x560")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(150, lambda: (dialog.lift(), dialog.focus_force()))
    result = {"recheck": False}

    ctk.CTkLabel(
        dialog,
        text=f"{len(missing)} department(s) need a contacts segment",
        font=ctk.CTkFont(size=15, weight="bold"), anchor="w",
    ).pack(fill="x", padx=16, pady=(14, 2))
    ctk.CTkLabel(
        dialog, anchor="w", justify="left", wraplength=600,
        text=("Optional but recommended. Texting already works without a "
              "segment — each student is reached by their Salesforce Contact id "
              "(no mobile needed) and gated by the Salesforce opt-in field. But "
              "that field can disagree with who's actually opted in inside "
              "Mongoose. A one-time segment per department gives VERIFIED "
              "opt-in (and covers any student whose Contact id didn't come "
              "through from Salesforce)."),
        font=ctk.CTkFont(size=12),
    ).pack(fill="x", padx=16, pady=(0, 8))

    steps = (
        "In Mongoose, for EACH department below:\n"
        "  1.  Switch to that department (top-left department selector).\n"
        "  2.  Tools → Segments → New Segment.\n"
        "  3.  Add a filter:   Contact ID   →   is not empty\n"
        "        (REQUIRED — this is what makes the list complete: every\n"
        "         student who has a Contact id is included.)\n"
        "  4.  Name the segment EXACTLY as shown below (copy it).\n"
        "  5.  Save.\n"
        "Then click “Re-check now”."
    )
    ctk.CTkLabel(
        dialog, text=steps, anchor="w", justify="left",
        font=ctk.CTkFont(size=12),
    ).pack(fill="x", padx=16, pady=(0, 8))

    ctk.CTkLabel(
        dialog, text="Exact segment name(s) to create:", anchor="w",
        font=ctk.CTkFont(size=12, weight="bold"),
    ).pack(fill="x", padx=16, pady=(0, 2))
    names = "\n".join(m["segment_name"] for m in missing)
    name_box = ctk.CTkTextbox(dialog, height=max(40, 22 * len(missing)),
                              wrap="none", font=ctk.CTkFont(size=13))
    name_box.pack(fill="x", padx=16, pady=(0, 8))
    name_box.insert("1.0", names)
    name_box.configure(state="disabled")

    # Show what each department currently has (helps spot a near-miss name).
    have = []
    for m in missing:
        present = m.get("available") or []
        have.append(f"{m['course']}: " + (", ".join(present) if present
                                          else "(no segments yet)"))
    ctk.CTkLabel(
        dialog, text="Currently in each department:\n" + "\n".join(have),
        anchor="w", justify="left", wraplength=600,
        font=ctk.CTkFont(size=11), text_color=("gray40", "gray70"),
    ).pack(fill="x", padx=16, pady=(0, 8))

    footer = ctk.CTkFrame(dialog, fg_color="transparent")
    footer.pack(fill="x", padx=16, pady=(0, 14))

    def _close():
        try:
            dialog.grab_release()
        except Exception:
            pass
        try:
            dialog.destroy()
        except Exception:
            pass

    def _recheck():
        result["recheck"] = True
        _close()

    ctk.CTkButton(footer, text="Re-check now", command=_recheck,
                  width=140).pack(side="right", padx=(6, 0))
    ctk.CTkButton(footer, text="Close", command=_close, width=90,
                  **SECONDARY_BTN_KWARGS).pack(side="right")
    dialog.bind("<Escape>", lambda _e: _close())
    dialog.protocol("WM_DELETE_WINDOW", _close)
    parent.wait_window(dialog)
    return result["recheck"]


def prompt_batch_text_review(
    parent,
    scenario_name: str,
    groups: list[dict],
    skipped_names: "Optional[list[str]]" = None,
    filter_summary: str = "",
    *,
    scheduled: bool = True,
) -> "Optional[list[list[str]]]":
    """Modal reviewer for batch texts. Each timezone group is a foldable section
    with a per-student checklist (default checked; students with neither a mobile
    nor a Contact id are shown disabled). A read-only "Skipped - not opted in"
    section lists students who won't be texted. The right pane shows the group's
    shared message.

    `groups`: dicts with keys label, course_code, when_str, body, issues,
    inbox_label, schedule, schedule_name, members (list of {name, mobile, term,
    via_id}). `term` is the Mongoose search key (Contact id or mobile); a member
    is textable iff it has one. Returns a list PARALLEL to `groups`, each element
    the selected recipient terms for that group; or None on cancel."""
    skipped_names = skipped_names or []
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Review texts - {scenario_name}")
    dialog.geometry("980x680")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(150, lambda: (dialog.lift(), dialog.focus_force()))
    result: dict = {"value": None}

    # Per-group, per-member selection vars. Members with no term (no mobile and
    # no Contact id) aren't textable -> var stays False and the checkbox is
    # disabled.
    member_vars: list = []
    for g in groups:
        member_vars.append(
            [ctk.BooleanVar(value=bool(m.get("term")))
             for m in g.get("members", [])])

    banner = ctk.CTkFrame(dialog, fg_color=("gray92", "gray18"))
    banner.pack(fill="x", padx=8, pady=(8, 0))
    ctk.CTkLabel(
        banner, text=f"Review batch texts: {scenario_name}",
        font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
    ).pack(fill="x", padx=10, pady=(8, 0))
    if filter_summary:
        ctk.CTkLabel(
            banner, text=f"Matched by: {filter_summary}",
            font=ctk.CTkFont(size=11), text_color=("gray40", "gray70"),
            anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 2))
    sel_label = ctk.CTkLabel(
        banner, text="", font=ctk.CTkFont(size=11),
        text_color=("gray35", "gray70"), anchor="w")
    sel_label.pack(fill="x", padx=10, pady=(0, 8))

    body = ctk.CTkFrame(dialog, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=8, pady=6)
    # Left: native ttk.Treeview — timezone groups as parent rows, students as
    # children, each with a ☐/☑ glyph in the 'sel' column (one Treeview is far
    # lighter than a CTkCheckBox per member, and gives native expand/collapse).
    left_col = ctk.CTkFrame(body, fg_color=("gray95", "gray16"))
    left_col.pack(side="left", fill="y", padx=(0, 6))
    tz_wrap = tk.Frame(left_col, bd=0, highlightthickness=0)
    tz_wrap.pack(side="top", fill="both", expand=True, padx=4, pady=4)
    tz_wrap.grid_rowconfigure(0, weight=1)
    tz_wrap.grid_columnconfigure(0, weight=1)
    tz_tree = ttk.Treeview(
        tz_wrap, columns=("sel",), show="tree headings",
        selectmode="browse", height=10, style="Caseload.Treeview",
    )
    tz_tree.heading("#0", text="Students by timezone")
    tz_tree.column("#0", width=360, anchor="w")
    tz_tree.heading("sel", text="")
    tz_tree.column("sel", width=44, minwidth=44, stretch=False, anchor="center")
    tz_tree.grid(row=0, column=0, sticky="nsew")
    _tsb = ttk.Scrollbar(tz_wrap, command=tz_tree.yview)
    _tsb.grid(row=0, column=1, sticky="ns")
    tz_tree.configure(yscrollcommand=_tsb.set)
    _tdark = ctk.get_appearance_mode() == "Dark"
    tz_tree.tag_configure("issue",
                          foreground=("#ff8a80" if _tdark else "#b00020"))
    tz_tree.tag_configure("muted", foreground="gray55")

    def _glyph(checked, textable=True):
        if not textable:
            return "—"
        return "☑" if checked else "☐"

    node_map: dict = {}  # iid -> ("group", i) | ("member", i, j)

    def _refresh_group_glyph(i):
        master = any(v.get() for v in member_vars[i])
        try:
            tz_tree.set(f"g{i}", "sel", _glyph(master))
        except Exception:
            pass

    def _toggle_iid(iid):
        info = node_map.get(iid)
        if not info:
            return
        if info[0] == "group":
            i = info[1]
            members = groups[i].get("members", [])
            gv = member_vars[i]
            target = not any(v.get() for v in gv)
            for j, (m, v) in enumerate(zip(members, gv)):
                if m.get("term"):
                    v.set(target)
                    tz_tree.set(f"g{i}m{j}", "sel", _glyph(target))
            _refresh_group_glyph(i)
        else:
            _, i, j = info
            m = groups[i].get("members", [])[j]
            if not m.get("term"):
                return  # not textable — ignore
            v = member_vars[i][j]
            v.set(not v.get())
            tz_tree.set(f"g{i}m{j}", "sel", _glyph(v.get()))
            _refresh_group_glyph(i)
        _update_sel_label()

    def _on_tz_click(event):
        iid = tz_tree.identify_row(event.y)
        if not iid:
            return
        if tz_tree.identify_column(event.x) == "#1":  # the 'sel' column
            _toggle_iid(iid)
            return "break"  # toggle only — don't move the preview selection

    def _on_tz_select(event=None):
        info = node_map.get(tz_tree.focus())
        if info:
            _show(info[1])

    tz_tree.bind("<Button-1>", _on_tz_click)
    tz_tree.bind("<<TreeviewSelect>>", _on_tz_select)

    right = ctk.CTkFrame(body, fg_color=("gray95", "gray16"))
    right.pack(side="left", fill="both", expand=True)
    preview_hdr = ctk.CTkLabel(
        right, text="", anchor="w", justify="left", font=ctk.CTkFont(size=12))
    preview_hdr.pack(fill="x", padx=10, pady=(10, 4))
    preview_box = ctk.CTkTextbox(right, wrap="word")
    preview_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    preview_box.configure(state="disabled")

    def _count():
        return sum(1 for gv in member_vars for v in gv if v.get())

    def _update_sel_label():
        sel_label.configure(
            text=f"{_count()} recipient(s) selected across "
                 f"{len(groups)} timezone group(s)")

    def _show(i):
        g = groups[i]
        names = ", ".join(m["name"] for m in g.get("members", []))
        hdr = (f"{g['label']}\n{'Schedule' if scheduled else 'Send'}:  "
               f"{g['when_str']}\nRecipients:  {names}")
        if g.get("issues"):
            hdr += f"\n⚠ {', '.join(g['issues'])}"
        preview_hdr.configure(text=hdr)
        preview_box.configure(state="normal")
        preview_box.delete("1.0", "end")
        preview_box.insert("1.0", g.get("body", ""))
        preview_box.configure(state="disabled")

    if skipped_names:
        skip_frame = ctk.CTkFrame(left_col, fg_color=("gray90", "gray22"))
        skip_frame.pack(side="bottom", fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            skip_frame,
            text=f"Skipped - not opted in ({len(skipped_names)})",
            anchor="w", font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray45", "gray65"),
        ).pack(fill="x", padx=8, pady=(4, 0))
        ctk.CTkLabel(
            skip_frame, text=", ".join(skipped_names), anchor="w",
            justify="left", wraplength=360, font=ctk.CTkFont(size=10),
            text_color=("gray45", "gray60"),
        ).pack(fill="x", padx=8, pady=(0, 4))

    # Populate the tree: a parent row per timezone group, a child per member.
    for i, g in enumerate(groups):
        gv = member_vars[i]
        members = g.get("members", [])
        ntext = sum(1 for m in members if m.get("term"))
        master = any(v.get() for v in gv)
        warn = "⚠ " if g.get("issues") else ""
        gid = f"g{i}"
        tz_tree.insert(
            "", "end", iid=gid, open=True,
            text=f"{warn}{g['label']}  ·  {g['when_str']}  ·  {ntext} textable",
            values=(_glyph(master),),
            tags=(("issue",) if g.get("issues") else ()),
        )
        node_map[gid] = ("group", i)
        for j, (m, v) in enumerate(zip(members, gv)):
            has = bool(m.get("term"))
            if not has:
                suffix = "   (no mobile / Contact id)"
            elif m.get("via_id"):
                suffix = "   (via Contact id)"
            else:
                suffix = ""
            mid = f"g{i}m{j}"
            tz_tree.insert(
                gid, "end", iid=mid, text="   " + m["name"] + suffix,
                values=(_glyph(v.get(), has),),
                tags=(() if has else ("muted",)),
            )
            node_map[mid] = ("member", i, j)

    _update_sel_label()
    if groups:
        _show(0)
        try:
            tz_tree.selection_set("g0")
            tz_tree.focus("g0")
        except Exception:
            pass

    footer = ctk.CTkFrame(dialog, fg_color="transparent")
    # Reserve the footer at the bottom BEFORE the expanding body, so the action
    # buttons are never pushed off-screen by a tall tree (the tree scrolls to
    # fit the space that's left).
    footer.pack(side="bottom", fill="x", padx=8, pady=(0, 8), before=body)

    def _toggle_all():
        target = _count() == 0
        for i, (g, gv) in enumerate(zip(groups, member_vars)):
            for j, (m, v) in enumerate(zip(g.get("members", []), gv)):
                if m.get("term"):
                    v.set(target)
                    try:
                        tz_tree.set(f"g{i}m{j}", "sel", _glyph(target))
                    except Exception:
                        pass
            _refresh_group_glyph(i)
        _update_sel_label()

    def _close():
        try:
            dialog.grab_release()
        except Exception:
            pass
        try:
            dialog.destroy()
        except Exception:
            pass

    def _send():
        out = []
        for g, gv in zip(groups, member_vars):
            out.append([m["term"] for m, v in zip(g.get("members", []), gv)
                        if v.get() and m.get("term")])
        result["value"] = out
        _close()

    ctk.CTkButton(
        footer, text="Select all / none", command=_toggle_all, width=140,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left")
    ctk.CTkButton(
        footer, text=("Schedule selected" if scheduled else "Send selected"),
        command=_send, width=150,
    ).pack(side="right", padx=(6, 0))
    ctk.CTkButton(
        footer, text="Cancel", command=_close, width=90, **SECONDARY_BTN_KWARGS,
    ).pack(side="right")
    dialog.bind("<Escape>", lambda _e: _close())
    dialog.protocol("WM_DELETE_WINDOW", _close)
    parent.wait_window(dialog)
    return result["value"]


def prompt_email_deselect_choice(parent, n_unchecked, n_total, other_desc):
    """3-way modal shown when the user leaves some students UNCHECKED in the
    email review AND the action also does something else (a note/text) for them.
    Returns:
      'email_only' — skip only the email for the unchecked; still do the rest.
      'entirely'   — drop the unchecked from the action entirely.
      'back'       — return to the email review.
    Defaults to 'back' (Esc / window close) so no student is silently dropped."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Some students unchecked for email")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(50, lambda: (dialog.lift(), dialog.focus_force()))
    res = {"value": "back"}

    ctk.CTkLabel(
        dialog, justify="left", anchor="w", wraplength=430,
        text=(f"{n_unchecked} of {n_total} student(s) are unchecked for "
              f"email.\nThis action also does {other_desc} for them.\n\n"
              f"For the unchecked student(s):"),
    ).pack(fill="x", padx=16, pady=(16, 10))

    def choose(v):
        res["value"] = v
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass
        try:
            if parent is not None:
                parent.grab_set()
        except Exception: pass

    btns = ctk.CTkFrame(dialog, fg_color="transparent")
    btns.pack(fill="x", padx=16, pady=(0, 14))
    ctk.CTkButton(
        btns, text="Skip only the email\n(do the rest for them)",
        command=lambda: choose("email_only"), width=210, height=46,
    ).pack(side="left", padx=(0, 6))
    ctk.CTkButton(
        btns, text="Skip them\nentirely", command=lambda: choose("entirely"),
        width=120, height=46, **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=6)
    ctk.CTkButton(
        btns, text="◀ Back", command=lambda: choose("back"), width=80,
        height=46, **SECONDARY_BTN_KWARGS,
    ).pack(side="right")
    dialog.bind("<Escape>", lambda _e: choose("back"))
    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("back"))
    _fit_dialog_to_content(dialog, min_w=470, near_mouse=True)
    parent.wait_window(dialog)
    return res["value"]


def prompt_batch_review(
    parent,
    scenario_name: str,
    rows: list[dict],
    display_columns: list[str],
) -> Optional[list[dict]]:
    """Show matched students before a batch fires. Returns the subset
    the user kept checked + confirmed, or None on cancel.

    `display_columns` is the in-order list of fields shown per row;
    the first column is usually 'Name' so the student is easy to
    identify, followed by whatever fields the scenario filtered on."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Batch: {scenario_name}")
    _restore_dialog_geometry(dialog, "batch_review")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    result: dict = {"value": None}

    header_label = ctk.CTkLabel(
        dialog,
        text=(
            f"{len(rows)} students matched. Uncheck anyone to skip, "
            "then click Confirm to start."
        ),
        anchor="w", justify="left",
    )
    header_label.pack(fill="x", padx=12, pady=(12, 4))

    cols_label = ctk.CTkLabel(
        dialog,
        text=" · ".join(display_columns),
        anchor="w", justify="left",
        font=ctk.CTkFont(size=12, weight="bold"),
    )
    cols_label.pack(fill="x", padx=12, pady=(0, 4))

    # Master select-all box: the SAME little checkbox the caseload viewer
    # uses in its header, sitting to the LEFT of a gray "Student (N)" tag.
    # Click toggles all rows — or clears them if they're already all
    # selected — mirroring the viewer's _toggle_select_all. The image refs
    # are kept on the dialog so Tk doesn't GC them (→ blank).
    _chk_un, _chk_ch = _build_checkbox_images()
    dialog._batch_chk_imgs = (_chk_un, _chk_ch)

    sel_row = ctk.CTkFrame(dialog, fg_color="transparent")
    sel_row.pack(fill="x", padx=12, pady=(0, 4))
    sel_all_box = ctk.CTkLabel(sel_row, text="", image=_chk_ch, cursor="hand2")
    sel_all_box.pack(side="left", padx=(4, 8))
    ctk.CTkLabel(
        sel_row, text=f"Student ({len(rows)})",
        fg_color=("gray85", "gray25"), corner_radius=6,
    ).pack(side="left", ipadx=8, ipady=2)

    scroll = ctk.CTkScrollableFrame(dialog)
    scroll.pack(fill="both", expand=True, padx=12, pady=4)

    checked_vars: list[ctk.BooleanVar] = []

    def update_count_label() -> None:
        n = sum(1 for v in checked_vars if v.get())
        confirm_btn.configure(text=f"Confirm {n}")
        # Master box reflects the rows: filled only when ALL are checked.
        all_on = bool(checked_vars) and n == len(checked_vars)
        try:
            sel_all_box.configure(image=_chk_ch if all_on else _chk_un)
        except Exception:
            pass

    def toggle_all(_event=None) -> None:
        # Select all, or clear if already all selected (mirrors the viewer).
        new = not (bool(checked_vars) and all(v.get() for v in checked_vars))
        for v in checked_vars:
            v.set(new)
        update_count_label()

    sel_all_box.bind("<Button-1>", toggle_all)

    for row in rows:
        v = ctk.BooleanVar(value=True)
        checked_vars.append(v)
        text = " · ".join(
            (row.get(c, "") or "")[:60] for c in display_columns
        )
        cb = ctk.CTkCheckBox(
            scroll, text=text, variable=v, command=update_count_label,
        )
        cb.pack(fill="x", padx=4, pady=1, anchor="w")

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(pady=(4, 12))

    def confirm(_event=None) -> None:
        selected = [rows[i] for i, v in enumerate(checked_vars) if v.get()]
        result["value"] = selected
        _save_dialog_geometry(dialog, "batch_review")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def cancel(_event=None) -> None:
        _save_dialog_geometry(dialog, "batch_review")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    confirm_btn = ctk.CTkButton(
        btn_row, text=f"Confirm {len(rows)}", command=confirm, width=140,
    )
    confirm_btn.pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", command=cancel, width=90,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    dialog.bind("<Escape>", cancel)
    dialog.protocol("WM_DELETE_WINDOW", cancel)

    parent.wait_window(dialog)
    return result["value"]
