"""Shared UI primitives for the Tkinter / CustomTkinter front end.

Small, framework-level building blocks that dialogs and panels both reuse:
secondary-button styling, per-session dialog geometry memory, a content-fit
sizer, a lightweight hover tooltip, checkbox images, email-preview tag styles,
and a listbox drag-reorder binding. Nothing here knows about the app
controller — these take plain widgets/values and return widgets/values.
"""
import re

import customtkinter as ctk
import tkinter as tk


# Styling kwargs for "secondary" (non-primary) CTk buttons — a muted, themed
# look distinct from the accent-colored primary action.
SECONDARY_BTN_KWARGS = dict(
    fg_color=("gray82", "gray28"),
    text_color=("gray10", "gray95"),
    hover_color=("gray72", "gray38"),
    border_width=1,
    border_color=("gray60", "gray45"),
)

# Vivid blue for the "+ Add …" affordance buttons in the editor, so the
# add-a-thing actions stand out from other controls. (light, dark) tuples.
_ADD_BTN_BLUE = ("#2f6fed", "#2f6fed")
_ADD_BTN_BLUE_HOVER = ("#2558c8", "#2558c8")


# Per-session memory of each named dialog's last size/position, so reopening a
# resized popup restores it. Defaults seed a sensible first-open size.
_DIALOG_GEOMETRY: dict[str, str] = {}
_DIALOG_DEFAULTS: dict[str, str] = {
    "find_and_pick": "480x440",
    "additional_text": "640x420",
    "batch_review": "720x560",
    "html_template_editor": "900x640",
}


def _restore_dialog_geometry(dialog, key: str) -> None:
    geom = _DIALOG_GEOMETRY.get(key, _DIALOG_DEFAULTS.get(key, ""))
    if geom:
        try:
            dialog.geometry(geom)
        except Exception:
            dialog.geometry(_DIALOG_DEFAULTS.get(key, "400x300"))


def _save_dialog_geometry(dialog, key: str) -> None:
    try:
        _DIALOG_GEOMETRY[key] = dialog.geometry()
    except Exception:
        pass


def _fit_dialog_to_content(dialog, min_w: int = 0, min_h: int = 0,
                           near_mouse: bool = False) -> None:
    """Grow a popup so all of its packed content is visible, then pin that as
    the minimum size. Needed for dialogs whose height varies with optional
    sections (e.g. the note editor's Essential Actions block, which sits at
    the bottom and was getting clipped when a smaller geometry was restored
    from an earlier no-EA session). Never shrinks an already-larger window,
    and clamps to the screen so it can't open off-edge.

    `near_mouse` places the (sized) window just up-left of the pointer so the
    cursor barely travels, instead of keeping any remembered position."""
    try:
        dialog.update_idletasks()
        req_w = max(int(min_w), dialog.winfo_reqwidth())
        req_h = max(int(min_h), dialog.winfo_reqheight())
        sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
        # Don't exceed the screen (leave a margin for the taskbar/title bar).
        max_w = max(req_w, sw - 40)
        max_h = max(req_h, sh - 80)
        req_w, req_h = min(req_w, max_w), min(req_h, max_h)
        geo = dialog.geometry()  # "WxH+X+Y" (W/H may be the 1x1 placeholder)
        m = re.match(r"(\d+)x(\d+)", geo)
        cur_w = int(m.group(1)) if m else 0
        cur_h = int(m.group(2)) if m else 0
        new_w = min(max(cur_w, req_w), max_w)
        new_h = min(max(cur_h, req_h), max_h)
        dialog.minsize(req_w, req_h)
        if near_mouse:
            try:
                px, py = dialog.winfo_pointerxy()
            except Exception:
                px, py = 0, 0
            x = min(max(px - 30, 0), max(sw - new_w, 0))
            y = min(max(py - 30, 0), max(sh - new_h, 0))
            dialog.geometry(f"{new_w}x{new_h}+{x}+{y}")
        else:
            # Keep a remembered position; for a fresh (1x1 placeholder)
            # dialog, set size only and let the window manager place it.
            has_pos = cur_w > 1 and cur_h > 1 and "+" in geo
            pos = geo[geo.index("+"):] if has_pos else ""
            dialog.geometry(f"{new_w}x{new_h}{pos}")
    except Exception:
        pass


def _attach_tooltip(widget, text: str) -> None:
    """Lightweight hover tooltip for a widget (no dependency on any UI
    framework — a borderless Toplevel shown on enter, hidden on leave).

    Idempotent: calling it again with new text UPDATES the tooltip in place
    rather than stacking another <Enter>/<Leave> binding. A widget repainted
    with fresh text — e.g. a task badge whose 'submitted (loading…)' label
    becomes 'passed' after the live status fetch — would otherwise keep the
    first binding alive and show its STALE text on hover (green badge but a
    'loading…' tooltip)."""
    holder = getattr(widget, "_tooltip_state", None)
    if holder is not None:
        holder["text"] = text  # binding already exists — just update the text
        return
    holder = {"text": text, "tip": None}
    try:
        widget._tooltip_state = holder
    except Exception:
        pass

    def show(_e=None):
        if holder["tip"] is not None or not holder["text"]:
            return
        try:
            x = widget.winfo_rootx() + 10
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(
                tip, text=holder["text"], justify="left",
                background="#2b2b2b", foreground="#f0f0f0",
                relief="solid", borderwidth=1, padx=6, pady=3,
                font=("", 9),
            ).pack()
            holder["tip"] = tip
        except Exception:
            holder["tip"] = None

    def hide(_e=None):
        if holder["tip"] is not None:
            try:
                holder["tip"].destroy()
            except Exception:
                pass
            holder["tip"] = None

    widget.bind("<Enter>", show, add="+")
    widget.bind("<Leave>", hide, add="+")


def _build_checkbox_images():
    """Build (unchecked, checked) 16px checkbox PhotoImages in the CTk
    style — an outlined box, and a filled blue box with a white tick. The
    CALLER must keep a reference (else Tk GCs them and they render blank).
    Needs a live Tk root. Shared by the caseload viewer's select-all header
    and the batch-review popup so both look identical."""
    from PIL import Image, ImageDraw, ImageTk
    dark = ctk.get_appearance_mode() == "Dark"
    blue = "#1f6aa5" if dark else "#3a7ebf"
    border = "#6b6e70" if dark else "#979da2"
    size, scale = 16, 4  # supersample then downscale for smooth edges
    S = size * scale
    pad, rad, bw = 1 * scale, 4 * scale, 2 * scale
    un = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(un).rounded_rectangle(
        [pad, pad, S - pad, S - pad], radius=rad, outline=border, width=bw)
    ch = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(ch)
    d.rounded_rectangle(
        [pad, pad, S - pad, S - pad], radius=rad, fill=blue, outline=blue)
    d.line(
        [(S * 0.27, S * 0.52), (S * 0.44, S * 0.69), (S * 0.74, S * 0.32)],
        fill="white", width=bw, joint="curve")
    return (ImageTk.PhotoImage(un.resize((size, size), Image.LANCZOS)),
            ImageTk.PhotoImage(ch.resize((size, size), Image.LANCZOS)))


def _configure_email_preview_tags(text_widget) -> None:
    """Set up the tag styles used by the HTML→Tk email preview renderer.
    Colors adapt to the current ctk appearance mode so the preview is
    readable on both light and dark themes."""
    mode = ctk.get_appearance_mode()
    is_dark = mode == "Dark"
    text_widget.tag_configure(
        "bold", font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
    )
    text_widget.tag_configure(
        "italic", font=ctk.CTkFont(family="Segoe UI", size=12, slant="italic"),
    )
    text_widget.tag_configure("underline", underline=True)
    text_widget.tag_configure(
        "link",
        foreground="#79b8ff" if is_dark else "#1a73e8",
        underline=True,
    )
    text_widget.tag_configure(
        "url_hint",
        foreground="#888888" if is_dark else "#666666",
        font=ctk.CTkFont(family="Segoe UI", size=10),
    )
    text_widget.tag_configure(
        "heading", font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
    )
    text_widget.tag_configure(
        "image",
        foreground="#5a4500" if not is_dark else "#ffd966",
        background="#fff3c4" if not is_dark else "#3a3520",
        font=ctk.CTkFont(family="Segoe UI", size=11, slant="italic"),
    )
    text_widget.tag_configure(
        "unresolved_var",
        foreground="#ffffff",
        background="#cc0000",
        font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
    )


def attach_listbox_drag_reorder(listbox, items, refresh, on_change=None):
    """Make a native ``tk.Listbox`` drag-reorderable, backed by the Python
    list ``items``. During a drag a thin accent line marks the drop gap
    between two rows; the move commits on release. ``refresh(sel=None)``
    re-renders the listbox from ``items`` (selecting ``sel`` when given);
    ``on_change()`` (optional) runs after a committed reorder. Shared by the
    Choose-columns and caseload-panel-actions choosers."""
    dark = ctk.get_appearance_mode() == "Dark"
    accent = "#4aa3df" if dark else "#1f6aa5"
    line = tk.Frame(listbox, height=2, bg=accent, bd=0, highlightthickness=0)
    state = {"src": None}

    def gap_index(y):
        n = listbox.size()
        if n == 0:
            return 0
        j = listbox.nearest(y)
        bbox = listbox.bbox(j)
        if bbox:
            _, by, _, bh = bbox
            if y > by + bh / 2:
                j += 1
        return max(0, min(j, n))

    def show_line(gap):
        n = listbox.size()
        if n == 0:
            line.place_forget()
            return
        if gap >= n:
            bbox = listbox.bbox(n - 1)
            y = (bbox[1] + bbox[3]) if bbox else 0
        else:
            bbox = listbox.bbox(gap)
            y = bbox[1] if bbox else 0
        line.place(x=2, y=max(0, y - 1), relwidth=1.0)
        line.lift()

    def on_press(e):
        state["src"] = listbox.nearest(e.y)

    def on_motion(e):
        if state["src"] is None:
            return
        show_line(gap_index(e.y))
        return "break"

    def on_release(e):
        src = state["src"]
        state["src"] = None
        line.place_forget()
        if src is None:
            return
        dst = gap_index(e.y)
        if dst > src:
            dst -= 1
        if 0 <= src < len(items) and dst != src:
            items.insert(dst, items.pop(src))
            refresh(sel=dst)
            if on_change:
                on_change()

    listbox.bind("<ButtonPress-1>", on_press, add="+")
    listbox.bind("<B1-Motion>", on_motion, add="+")
    listbox.bind("<ButtonRelease-1>", on_release, add="+")


# ---- Adjustable text sizes (per "channel") -------------------------------
# Named font channels so each reading/editing surface can carry its own
# user-adjustable text size: 'activity' (log), 'viewer' (caseload table),
# 'email' (FERPA reviewer), 'editor' (note bodies + template editor).
# CTkTextbox surfaces register via register_font_box(); non-CTk surfaces
# (the ttk caseload Treeview) register an apply callback via
# register_font_apply(). Ctrl +/- and Ctrl+MouseWheel on a registered
# widget adjust that channel live. The App wires _FONT_PERSIST to save.
UI_FONT_CHANNELS = ("activity", "viewer", "email", "editor", "notes")
UI_FONT_DEFAULTS = {"activity": 13, "viewer": 11, "email": 12, "editor": 12,
                    "notes": 12}
UI_FONT_MIN, UI_FONT_MAX = 8, 40
_font_sizes: dict = dict(UI_FONT_DEFAULTS)
_font_boxes: dict = {c: [] for c in UI_FONT_CHANNELS}
_font_applies: dict = {c: [] for c in UI_FONT_CHANNELS}
_FONT_PERSIST: list = [None]  # holder for persist callback(channel, size)


def font_size(channel: str) -> int:
    return _font_sizes.get(channel, 13)


def _set_box_font(box, n: int) -> None:
    """Apply font size `n` to a text box. CTk text boxes need a CTkFont (DPI
    scaling); a native tk.Text (the activity log) takes a (family, size)
    tuple."""
    if hasattr(box, "_textbox"):          # CTkTextbox
        box.configure(font=ctk.CTkFont(size=n))
    else:                                  # native tk.Text
        box.configure(font=("Segoe UI", n))


def set_font_size(channel: str, n: int, persist: bool = True) -> None:
    """Set a channel's text size (clamped) and apply to all its widgets."""
    n = max(UI_FONT_MIN, min(UI_FONT_MAX, int(n)))
    _font_sizes[channel] = n
    for b in list(_font_boxes.get(channel, [])):
        try:
            _set_box_font(b, n)
        except Exception:
            try:
                _font_boxes[channel].remove(b)
            except ValueError:
                pass
    for cb in list(_font_applies.get(channel, [])):
        try:
            cb(n)
        except Exception:
            pass
    if persist and _FONT_PERSIST[0]:
        try:
            _FONT_PERSIST[0](channel, n)
        except Exception:
            pass


def bind_font_hotkeys(channel: str, widget) -> None:
    """Bind Ctrl +/- and Ctrl+MouseWheel on `widget` to adjust `channel`."""
    def bump(d):
        set_font_size(channel, _font_sizes[channel] + d)
        return "break"
    widget.bind("<Control-MouseWheel>",
                lambda e: bump(1 if e.delta > 0 else -1))
    widget.bind("<Control-plus>", lambda e: bump(1))
    widget.bind("<Control-equal>", lambda e: bump(1))  # Ctrl+= (no shift)
    widget.bind("<Control-minus>", lambda e: bump(-1))


def register_font_box(channel: str, box, hotkeys: bool = True) -> None:
    """Register a CTkTextbox to a font channel: apply the current size and
    (optionally) bind the zoom hotkeys."""
    _font_boxes.setdefault(channel, []).append(box)
    try:
        _set_box_font(box, _font_sizes[channel])
    except Exception:
        pass
    if hotkeys:
        bind_font_hotkeys(channel, box)


def register_font_apply(channel: str, cb) -> None:
    """Register an apply callback cb(size) for a non-CTkTextbox surface
    (e.g. the caseload Treeview style). Called now + on every change."""
    _font_applies.setdefault(channel, []).append(cb)
    try:
        cb(_font_sizes[channel])
    except Exception:
        pass
