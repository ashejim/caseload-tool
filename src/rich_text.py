"""Rich-text editing + HTML preview widgets for the email/template editors.

- ``RichTextEditor`` — a CTk-backed WYSIWYG box that reads/writes a restricted
  HTML subset (bold/italic/underline, links, lists, images), with rich-paste
  from the Windows clipboard's CF_HTML.
- ``_EditorHTMLParser`` — parses that HTML subset back into the editor's tags.
- ``_HTMLToTkRenderer`` — renders HTML into a read-only tk.Text for the email
  review preview.
- clipboard-HTML helpers — pull the HTML fragment out of a CF_HTML paste.

Extracted from ``scripts/launcher.py`` so the widget layer is its own module.
The two dialogs that use these (the HTML template editor + batch email review)
live in ``src.dialogs``.
"""
import html
import re
from html.parser import HTMLParser
from typing import Optional

import customtkinter as ctk
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from src.config import email_link_color
from src.ui_common import SECONDARY_BTN_KWARGS


def _extract_cf_html_fragment(raw: bytes) -> Optional[str]:
    """Pull the copied HTML fragment out of a Windows 'HTML Format' clipboard
    payload. The payload is a header (Version/StartHTML/StartFragment/… BYTE
    offsets) followed by HTML with <!--StartFragment-->…<!--EndFragment-->
    markers around the actual selection. Prefer the byte offsets (exact), fall
    back to the comment markers, then the whole thing."""
    try:
        header = raw[:256].decode("ascii", "replace")
        sf = re.search(r"StartFragment:(\d+)", header)
        ef = re.search(r"EndFragment:(\d+)", header)
        if sf and ef:
            frag = raw[int(sf.group(1)):int(ef.group(1))]
            return frag.decode("utf-8", "replace")
    except Exception:
        pass
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return None
    s = text.find("<!--StartFragment-->")
    e = text.find("<!--EndFragment-->")
    if s != -1 and e != -1:
        return text[s + len("<!--StartFragment-->"):e]
    return text or None


def _clipboard_html_fragment() -> Optional[str]:
    """The clipboard's rich-HTML form (with links/formatting), or None when
    the clipboard has no HTML (so callers fall back to plain-text paste).
    Windows-only via pywin32's win32clipboard (already a dependency)."""
    try:
        import win32clipboard as wc
    except Exception:
        return None
    try:
        wc.OpenClipboard()
    except Exception:
        return None
    try:
        cf = wc.RegisterClipboardFormat("HTML Format")
        if not wc.IsClipboardFormatAvailable(cf):
            return None
        raw = wc.GetClipboardData(cf)
    except Exception:
        return None
    finally:
        try:
            wc.CloseClipboard()
        except Exception:
            pass
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not isinstance(raw, (bytes, bytearray)):
        return None
    return _extract_cf_html_fragment(bytes(raw))


class _EditorHTMLParser(HTMLParser):
    """Parse simple HTML into a RichTextEditor's Tk Text with editable
    tags. Companion to RichTextEditor.to_html — handles paragraphs,
    headings, b/i/u, links, alignment, images, and (for round-tripping
    older templates) bullet/numbered lists rendered as plain lines."""

    def __init__(self, editor: "RichTextEditor", anchor: str = "end"):
        super().__init__()
        self.ed = editor
        self.t = editor.text
        # Where parsed content lands: "end" when building the whole document
        # (set_html), or "insert" to splice at the cursor (rich paste). `_here`
        # is the matching "current position" index — "end-1c" sits just before
        # the Text widget's permanent trailing newline; the insert mark needs
        # no such offset.
        self.anchor = anchor
        self._here = "end-1c" if anchor == "end" else "insert"
        self._inline: list[str] = []        # active bold/italic/underline
        self._link: Optional[str] = None    # active link tag name
        self._block_start: Optional[str] = None
        self._block: str = "p"              # p | h2
        self._align: str = "left"
        self._list: list[str] = []          # ul/ol nesting (round-trip only)
        self._ol_n: list[int] = []
        self._skip = 0
        self._pending_nl = False            # emit a newline before next block

    _SKIP = {"style", "script", "head", "title", "meta", "link"}

    def _begin_block(self, block: str, align: str) -> None:
        if self._pending_nl:
            self.t.insert(self.anchor, "\n")
        self._pending_nl = False
        self._block_start = self.t.index(self._here)
        self._block = block
        self._align = align

    def _end_block(self) -> None:
        if self._block_start is None:
            return
        end = self.t.index(self._here)
        if self._block == "h2":
            self.t.tag_add("h2", self._block_start, end)
        elif self._block in ("ul", "ol"):
            self.t.tag_add(self._block, self._block_start, end)
        if self._align == "center":
            self.t.tag_add("align_center", self._block_start, end)
        elif self._align == "right":
            self.t.tag_add("align_right", self._block_start, end)
        self._block_start = None
        self._pending_nl = True

    @staticmethod
    def _align_of(attrs: dict) -> str:
        style = (attrs.get("style", "") or "").lower()
        if "text-align:center" in style.replace(" ", ""):
            return "center"
        if "text-align:right" in style.replace(" ", ""):
            return "right"
        return "left"

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        d = dict(attrs)
        if tag in ("p", "div"):
            # div is treated as a paragraph break — web/Outlook content uses
            # it per line; the pending-newline logic keeps siblings on
            # separate lines without breaking on benign nesting.
            self._begin_block("p", self._align_of(d))
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._begin_block("h2", self._align_of(d))
        elif tag == "br":
            self.t.insert(self.anchor, "\n")
        elif tag in ("b", "strong"):
            self._inline.append("bold")
        elif tag in ("i", "em"):
            self._inline.append("italic")
        elif tag == "u":
            self._inline.append("underline")
        elif tag == "a":
            self._link = self.ed._new_link(d.get("href", ""))
        elif tag in ("ul", "ol"):
            self._list.append(tag)
            self._ol_n.append(0)
        elif tag == "li":
            if self._pending_nl:
                self.t.insert(self.anchor, "\n")
            self._pending_nl = False
            self._block_start = self.t.index(self._here)
            kind = "ol" if (self._list and self._list[-1] == "ol") else "ul"
            self._block, self._align = kind, "left"
            mstart = self.t.index(self._here)
            if kind == "ol":
                self._ol_n[-1] += 1
                self.t.insert(self.anchor, "%d. " % self._ol_n[-1])
            else:
                self.t.insert(self.anchor, "• ")
            self.t.tag_add("listmarker", mstart, self._here)
        elif tag == "img":
            self.ed._insert_image_token(
                d.get("src", ""), pending_nl=self._pending_nl,
                anchor=self.anchor)
            self._pending_nl = True

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
            self._end_block()
        elif tag in ("b", "strong"):
            self._pop("bold")
        elif tag in ("i", "em"):
            self._pop("italic")
        elif tag == "u":
            self._pop("underline")
        elif tag == "a":
            self._link = None
        elif tag in ("ul", "ol"):
            if self._list:
                self._list.pop()
                self._ol_n.pop()

    def _pop(self, name: str) -> None:
        for i in range(len(self._inline) - 1, -1, -1):
            if self._inline[i] == name:
                del self._inline[i]
                return

    def _insert(self, text: str) -> None:
        tags = tuple(self._inline) + ((self._link,) if self._link else ())
        self.t.insert(self.anchor, text, tags)

    def handle_data(self, data):
        if self._skip:
            return
        collapsed = re.sub(r"\s+", " ", data)
        if not collapsed.strip() and "\n" in data:
            return
        if collapsed:
            self._insert(collapsed)

    def handle_entityref(self, name):
        if not self._skip:
            self._insert(html.unescape(f"&{name};"))

    def handle_charref(self, name):
        if not self._skip:
            self._insert(html.unescape(f"&#{name};"))


class RichTextEditor:
    """Lightweight rich-text editor over tk.Text that round-trips to
    simple, email-friendly HTML. Block model is LINE-BASED: each logical
    line is one block — a paragraph by default, or a heading; alignment is
    a per-line attribute. Inline runs carry bold/italic/underline/link
    tags. {{vars}} are literal text; images are placeholder tokens that
    serialize back to `<img src="cid:…">`. (Lists are a later phase.)"""

    _INLINE = ("bold", "italic", "underline")

    def __init__(self, parent, base_size: int = 11,
                 on_add_image: Optional[Callable] = None,
                 on_insert_var=None):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.grid_rowconfigure(1, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)
        self._base_size = base_size
        self._link_seq = 0
        self._links: dict[str, str] = {}
        self._img_seq = 0
        self._imgs: dict[str, str] = {}
        self._on_add_image = on_add_image

        self._build_toolbar()

        wrap = ctk.CTkFrame(self.frame)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        dark = ctk.get_appearance_mode() == "Dark"
        bg, fg = ("#2b2b2b", "#dce4ee") if dark else ("#ffffff", "#1a1a1a")
        self.text = tk.Text(
            wrap, wrap="word", undo=True, borderwidth=0,
            font=("Segoe UI", base_size), padx=10, pady=8,
            background=bg, foreground=fg, insertbackground=fg,
            spacing3=4,
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(wrap, command=self.text.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=vsb.set)
        self._configure_tags()
        # Enter continues/exits a list; Space drives Markdown shortcuts.
        self.text.bind("<Return>", self._on_return)
        self.text.bind("<KeyPress-space>", self._on_space)
        # Rich paste: keep links/bold/etc. when the clipboard carries HTML.
        self.text.bind("<<Paste>>", self._on_paste)

    # ----- setup -----

    def _configure_tags(self) -> None:
        base = tkfont.Font(family="Segoe UI", size=self._base_size)
        self.text.tag_configure(
            "bold", font=tkfont.Font(
                family="Segoe UI", size=self._base_size, weight="bold"))
        self.text.tag_configure(
            "italic", font=tkfont.Font(
                family="Segoe UI", size=self._base_size, slant="italic"))
        self.text.tag_configure(
            "underline", font=tkfont.Font(
                family="Segoe UI", size=self._base_size, underline=True))
        self.text.tag_configure(
            "h2", font=tkfont.Font(
                family="Segoe UI", size=self._base_size + 6, weight="bold"),
            spacing1=8, spacing3=4)
        self.text.tag_configure("align_center", justify="center")
        self.text.tag_configure("align_right", justify="right")
        self.text.tag_configure("ul", lmargin1=22, lmargin2=38)
        self.text.tag_configure("ol", lmargin1=22, lmargin2=38)
        self.text.tag_configure("listmarker")  # marks bullet/number prefix
        self.text.tag_configure(
            "image", foreground=("#1f6aa5" if ctk.get_appearance_mode() ==
                                 "Dark" else "#3a7ebf"))
        del base

    def _build_toolbar(self) -> None:
        bar = ctk.CTkFrame(self.frame, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        def btn(text, cmd, w=34):
            return ctk.CTkButton(
                bar, text=text, width=w, height=26, command=cmd,
                font=ctk.CTkFont(size=12), **SECONDARY_BTN_KWARGS)

        btn("B", lambda: self._toggle_inline("bold"), 30).pack(
            side="left", padx=1)
        btn("I", lambda: self._toggle_inline("italic"), 30).pack(
            side="left", padx=1)
        btn("U", lambda: self._toggle_inline("underline"), 30).pack(
            side="left", padx=1)
        btn("Heading", self._toggle_heading, 64).pack(side="left", padx=(8, 1))
        btn("• List", lambda: self._toggle_list("ul"), 52).pack(
            side="left", padx=(6, 1))
        btn("1. List", lambda: self._toggle_list("ol"), 56).pack(
            side="left", padx=1)
        ctk.CTkLabel(
            bar, text="Align:", font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 2))
        btn("Left", lambda: self._set_align("left"), 48).pack(
            side="left", padx=1)
        btn("Center", lambda: self._set_align("center"), 58).pack(
            side="left", padx=1)
        btn("Right", lambda: self._set_align("right"), 52).pack(
            side="left", padx=1)
        btn("🔗 Link", self._add_link, 64).pack(side="left", padx=(8, 1))
        if self._on_add_image:
            btn("🖼 Image", lambda: self._on_add_image(), 70).pack(
                side="left", padx=1)

    # ----- formatting actions -----

    def _sel_range(self):
        try:
            return self.text.index("sel.first"), self.text.index("sel.last")
        except tk.TclError:
            return None

    def _toggle_inline(self, tag: str) -> None:
        rng = self._sel_range()
        if not rng:
            return
        a, b = rng
        # If every char already has the tag, remove it; else add it.
        on = all(tag in self.text.tag_names(f"{a}+{i}c")
                 for i in range(self._span(a, b)))
        if on:
            self.text.tag_remove(tag, a, b)
        else:
            self.text.tag_add(tag, a, b)
        self.text.focus_set()

    def _span(self, a: str, b: str) -> int:
        return max(0, len(self.text.get(a, b)))

    def _line_range(self):
        sel = self._sel_range()
        if sel:
            first = int(sel[0].split(".")[0])
            last = int(sel[1].split(".")[0])
        else:
            first = last = int(self.text.index("insert").split(".")[0])
        return first, last

    def _line_block_kind(self, ln: int) -> Optional[str]:
        names = self.text.tag_names(f"{ln}.0")
        for k in ("h2", "ul", "ol"):
            if k in names:
                return k
        return None

    def _strip_marker(self, ln: int) -> None:
        """Remove a leading bullet/number marker from a list line."""
        a = f"{ln}.0"
        rng = self.text.tag_nextrange("listmarker", a, f"{ln}.end")
        if rng and self.text.compare(rng[0], "==", a):
            self.text.delete(rng[0], rng[1])

    def _set_line_block(self, ln: int, block: Optional[str]) -> None:
        """Set a line's block type: None (paragraph), 'h2', 'ul', or 'ol'.
        Markers for list lines are (re)generated by _renumber_lists."""
        a, b = f"{ln}.0", f"{ln + 1}.0"
        self._strip_marker(ln)
        for t in ("h2", "ul", "ol"):
            self.text.tag_remove(t, a, b)
        if block in ("h2", "ul", "ol"):
            self.text.tag_add(block, a, b)

    def _renumber_lists(self) -> None:
        """Rewrite every list line's marker: '• ' for bullets, sequential
        '1. 2. …' for numbered runs (restarting after any break)."""
        last = int(self.text.index("end-1c").split(".")[0])
        prev = None
        n = 0
        for ln in range(1, last + 1):
            kind = self._line_block_kind(ln)
            kind = kind if kind in ("ul", "ol") else None
            if kind == "ol":
                n = (n + 1) if prev == "ol" else 1
            prev = kind
            if kind:
                self._strip_marker(ln)
                marker = (f"{n}. " if kind == "ol" else "• ")
                self.text.insert(f"{ln}.0", marker)
                self.text.tag_add(
                    "listmarker", f"{ln}.0", f"{ln}.0 + {len(marker)}c")
                self.text.tag_add(kind, f"{ln}.0", f"{ln + 1}.0")

    def _toggle_heading(self) -> None:
        first, last = self._line_range()
        all_h2 = all(self._line_block_kind(ln) == "h2"
                     for ln in range(first, last + 1))
        for ln in range(first, last + 1):
            self._set_line_block(ln, None if all_h2 else "h2")
        self.text.focus_set()

    def _toggle_list(self, kind: str) -> None:
        first, last = self._line_range()
        all_kind = all(self._line_block_kind(ln) == kind
                       for ln in range(first, last + 1))
        for ln in range(first, last + 1):
            self._set_line_block(ln, None if all_kind else kind)
        self._renumber_lists()
        self.text.focus_set()

    def _on_return(self, event=None):
        """Inside a list: Enter starts a new item; Enter on an empty item
        leaves the list. Elsewhere: default newline (new paragraph)."""
        ln = int(self.text.index("insert").split(".")[0])
        kind = self._line_block_kind(ln)
        if kind not in ("ul", "ol"):
            return  # default
        line = self.text.get(f"{ln}.0", f"{ln}.end")
        content = re.sub(r"^(?:•\s*|\d+\.\s*)", "", line)
        if not content.strip():
            self._set_line_block(ln, None)
            self._renumber_lists()
            return "break"
        self.text.insert("insert", "\n")
        newln = int(self.text.index("insert").split(".")[0])
        self.text.tag_add(kind, f"{newln}.0", f"{newln + 1}.0")
        self._renumber_lists()
        self.text.see("insert")
        return "break"

    def _on_space(self, event=None):
        """Markdown shortcuts on the space key. Line-start: '# '→heading,
        '- '/'* '/'+ '→bullets, '1. '→numbered. Inline (token then space):
        **bold**, *italic* / _italic_, [text](url)."""
        ln, col = map(int, self.text.index("insert").split("."))
        before = self.text.get(f"{ln}.0", "insert")
        if before == "#":
            self.text.delete(f"{ln}.0", "insert")
            self._set_line_block(ln, "h2")
            return "break"
        if before in ("-", "*", "+"):
            self.text.delete(f"{ln}.0", "insert")
            self._set_line_block(ln, "ul")
            self._renumber_lists()
            return "break"
        if re.fullmatch(r"\d+\.", before):
            self.text.delete(f"{ln}.0", "insert")
            self._set_line_block(ln, "ol")
            self._renumber_lists()
            return "break"
        # Inline tokens ending right at the cursor.
        for pat, tag in (
            (r"\*\*([^*]+)\*\*$", "bold"),
            (r"__([^_]+)__$", "bold"),
            (r"(?<!\*)\*([^*\s][^*]*)\*$", "italic"),
            (r"(?<!_)_([^_\s][^_]*)_$", "italic"),
        ):
            m = re.search(pat, before)
            if m:
                a = f"{ln}.{col - len(m.group(0))}"
                self.text.delete(a, "insert")
                self.text.insert(a, m.group(1), (tag,))
                return  # let the space type normally after the run
        m = re.search(r"\[([^\]]+)\]\(([^)]+)\)$", before)
        if m:
            a = f"{ln}.{col - len(m.group(0))}"
            link_tag = self._new_link(m.group(2))
            self.text.delete(a, "insert")
            self.text.insert(a, m.group(1), (link_tag,))
            return
        return

    def _set_align(self, how: str) -> None:
        first, last = self._line_range()
        for ln in range(first, last + 1):
            # Tk's `justify` only takes effect when the tag spans the full
            # line INCLUDING its newline — hence {ln}.0 .. {ln+1}.0.
            a, b = f"{ln}.0", f"{ln + 1}.0"
            self.text.tag_remove("align_center", a, b)
            self.text.tag_remove("align_right", a, b)
            if how == "center":
                self.text.tag_add("align_center", a, b)
            elif how == "right":
                self.text.tag_add("align_right", a, b)
        self.text.focus_set()

    def _add_link(self) -> None:
        rng = self._sel_range()
        if not rng:
            from tkinter import messagebox
            messagebox.showinfo(
                "Add link", "Select the text to turn into a link first.")
            return
        url = ctk.CTkInputDialog(
            text="Link URL (https://… or mailto:…):", title="Add link").get_input()
        if not url:
            return
        tag = self._new_link(url.strip())
        self.text.tag_add(tag, rng[0], rng[1])
        self.text.focus_set()

    def _new_link(self, href: str) -> str:
        self._link_seq += 1
        tag = f"link#{self._link_seq}"
        self._links[tag] = href
        # Preview link color matches the configured email link color in light
        # mode (WYSIWYG); dark mode keeps a lighter blue for readability on the
        # dark editor background.
        dark = ctk.get_appearance_mode() == "Dark"
        self.text.tag_configure(
            tag, foreground=("#79b8ff" if dark else email_link_color()),
            underline=True)
        return tag

    def insert_text(self, text: str) -> None:
        self.text.insert("insert", text)
        self.text.focus_set()

    def _insert_image_token(self, src: str, pending_nl: bool = False,
                            anchor: str = "end") -> None:
        here = "end-1c" if anchor == "end" else "insert"
        if pending_nl:
            self.text.insert(anchor, "\n")
        stem = src[4:] if src.startswith("cid:") else src.rsplit("/", 1)[-1]
        self._img_seq += 1
        tag = f"img#{self._img_seq}"
        self._imgs[tag] = src
        start = self.text.index(here)
        self.text.insert(anchor, f"🖼 {stem}")
        self.text.tag_add("image", start, here)
        self.text.tag_add(tag, start, here)

    def insert_image(self, src: str) -> None:
        """Insert an image placeholder at a fresh line near the cursor."""
        self.text.insert("insert", "\n")
        stem = src[4:] if src.startswith("cid:") else src.rsplit("/", 1)[-1]
        self._img_seq += 1
        tag = f"img#{self._img_seq}"
        self._imgs[tag] = src if src.startswith("cid:") else f"cid:{stem}"
        start = self.text.index("insert")
        self.text.insert("insert", f"🖼 {stem}")
        self.text.tag_add("image", start, "insert")
        self.text.tag_add(tag, start, "insert")
        self.text.insert("insert", "\n")
        self.text.focus_set()

    # ----- HTML <-> editor -----

    def set_html(self, html_text: str) -> None:
        self.text.delete("1.0", "end")
        for t in list(self._links):
            self._links.pop(t, None)
        self._imgs.clear()
        self._link_seq = self._img_seq = 0
        parser = _EditorHTMLParser(self)
        try:
            parser.feed(html_text or "")
            parser.close()
        except Exception:
            # Fall back to dropping the raw text in unstyled.
            self.text.insert("1.0", html_text or "")
        # Trim a leading blank line the block logic may have produced.
        if self.text.get("1.0", "1.end").strip() == "" and \
                int(self.text.index("end-1c").split(".")[0]) > 1:
            self.text.delete("1.0", "2.0")
        self._renumber_lists()  # normalize bullet/number markers
        self.text.edit_reset()

    def _on_paste(self, event=None):
        """Paste rich content (links, bold, etc.) when the clipboard carries
        HTML, by parsing it in at the cursor. With no HTML on the clipboard,
        returns None so Tk's default plain-text paste runs unchanged."""
        frag = _clipboard_html_fragment()
        if not frag or not frag.strip():
            return None  # plain text only -> let the default <<Paste>> proceed
        try:
            if self.text.tag_ranges("sel"):
                self.text.delete("sel.first", "sel.last")
        except Exception:
            pass
        parser = _EditorHTMLParser(self, anchor="insert")
        try:
            parser.feed(frag)
            parser.close()
        except Exception:
            pass  # best-effort: whatever parsed before the error stays
        self._renumber_lists()
        try:
            self.text.see("insert")
        except Exception:
            pass
        self.text.focus_set()
        return "break"  # consumed — skip the default plain-text paste

    def _inline_key_at(self, idx: str):
        names = self.text.tag_names(idx)
        inline = tuple(n for n in self._INLINE if n in names)
        link = next((n for n in names if n.startswith("link#")), None)
        return inline, link

    def _img_tag_on_line(self, ln: int) -> Optional[str]:
        for n in self.text.tag_names(f"{ln}.0"):
            if n.startswith("img#"):
                return n
        return None

    def _serialize_line(self, ln: int) -> str:
        line = self.text.get(f"{ln}.0", f"{ln}.end")
        runs = []  # (text, (inline_tuple, link))
        for col, ch in enumerate(line):
            key = self._inline_key_at(f"{ln}.{col}")
            if runs and runs[-1][1] == key:
                runs[-1][0].append(ch)
            else:
                runs.append(([ch], key))
        out = []
        color = email_link_color()
        for chars, (inline, link) in runs:
            text = html.escape("".join(chars))
            opens, closes = "", ""
            if link:
                href = html.escape(self._links.get(link, ""), quote=True)
                # Color the link, and wrap its text in a <span> carrying the
                # SAME color: Outlook's Word engine routinely strips color off
                # the <a> itself but honors it on a child element, so the span
                # is what actually makes the color stick. Color comes from
                # config (Settings → Email link color; default blue).
                opens += (f'<a href="{href}" '
                          f'style="color:{color};text-decoration:underline;">'
                          f'<span style="color:{color};">')
                closes = "</span></a>" + closes
            for t, (o, c) in (("bold", ("<b>", "</b>")),
                              ("italic", ("<i>", "</i>")),
                              ("underline", ("<u>", "</u>"))):
                if t in inline:
                    opens += o
                    closes = c + closes
            out.append(opens + text + closes)
        return "".join(out)

    def to_html(self) -> str:
        blocks = []
        last = int(self.text.index("end-1c").split(".")[0])
        ln = 1
        while ln <= last:
            img_tag = self._img_tag_on_line(ln)
            if img_tag:
                src = self._imgs.get(img_tag, "")
                if src:
                    blocks.append(f'<img src="{html.escape(src, quote=True)}">')
                ln += 1
                continue
            names0 = self.text.tag_names(f"{ln}.0")
            if "ul" in names0 or "ol" in names0:
                kind = "ul" if "ul" in names0 else "ol"
                items = []
                while ln <= last and kind in self.text.tag_names(f"{ln}.0"):
                    raw = self.text.get(f"{ln}.0", f"{ln}.end")
                    if raw.strip():
                        inner = re.sub(r"^(?:•\s*|\d+\.\s*)", "",
                                       self._serialize_line(ln))
                        items.append(f"<li>{inner}</li>")
                    ln += 1
                if items:
                    blocks.append(f"<{kind}>" + "".join(items) + f"</{kind}>")
                continue
            raw = self.text.get(f"{ln}.0", f"{ln}.end")
            if not raw.strip():
                ln += 1
                continue
            inner = self._serialize_line(ln)
            align = ("center" if "align_center" in names0
                     else "right" if "align_right" in names0 else "")
            style = f' style="text-align:{align}"' if align else ""
            if "h2" in names0:
                blocks.append(f"<h2{style}>{inner}</h2>")
            else:
                blocks.append(f"<p{style}>{inner}</p>")
            ln += 1
        return "\n".join(blocks) + ("\n" if blocks else "")


class _HTMLToTkRenderer(HTMLParser):
    """Render simplified HTML into a Tk Text widget using tag-based
    formatting. Goal: legible to non-technical reviewers (FERPA), not
    pixel-perfect. Output handles paragraphs, links, basic
    formatting, lists, headings, and images-as-placeholders.

    Highlights any `{{var}}` placeholder that survived rendering in
    red — those are the FERPA risk (template variable referenced
    that didn't get a value)."""

    _SKIP_CONTENT = {"style", "script", "head", "title", "meta", "link"}

    def __init__(self, text_widget, unresolved_var_set: Optional[set] = None):
        super().__init__()
        self.text = text_widget
        # `unresolved_vars` is populated as we find any leftover
        # `{{name}}` in the rendered HTML — caller uses it to
        # populate the "issues" badge on the row.
        self.unresolved_vars: set[str] = (
            unresolved_var_set if unresolved_var_set is not None else set()
        )
        self._format_stack: list[str] = []
        self._link_href = ""
        self._list_stack: list[dict] = []
        self._skip_depth = 0
        self._first_block = True

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_CONTENT:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        d = dict(attrs)

        if tag == "p":
            self._paragraph_break()
        elif tag == "br":
            self.text.insert("end", "\n")
        elif tag in ("strong", "b"):
            self._format_stack.append("bold")
        elif tag in ("em", "i"):
            self._format_stack.append("italic")
        elif tag == "u":
            self._format_stack.append("underline")
        elif tag == "a":
            self._format_stack.append("link")
            self._link_href = d.get("href", "")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._paragraph_break()
            self._format_stack.append("heading")
        elif tag == "ol":
            self._list_stack.append({"type": "ol", "n": 0})
            self._paragraph_break()
        elif tag == "ul":
            self._list_stack.append({"type": "ul", "n": 0})
            self._paragraph_break()
        elif tag == "li":
            self.text.insert("end", "\n")
            depth = max(0, len(self._list_stack) - 1)
            self.text.insert("end", "    " * depth)
            if self._list_stack and self._list_stack[-1]["type"] == "ol":
                self._list_stack[-1]["n"] += 1
                self.text.insert("end", f"{self._list_stack[-1]['n']}. ")
            else:
                self.text.insert("end", "• ")
        elif tag == "img":
            src = d.get("src", "")
            alt = (d.get("alt", "") or "").strip()
            # cid:STEM is what the live email uses; show the stem
            # so the reviewer can see the file being referenced.
            if src.startswith("cid:"):
                label = src[4:]
            else:
                label = src.rsplit("/", 1)[-1] or src
            marker = f"[Image: {label}"
            if alt:
                marker += f"  ⇨  {alt}"
            marker += "]"
            self._paragraph_break()
            self._insert_tagged(marker, "image")
            self.text.insert("end", "\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_CONTENT:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return

        if tag in ("strong", "b"):
            self._pop_format("bold")
        elif tag in ("em", "i"):
            self._pop_format("italic")
        elif tag == "u":
            self._pop_format("underline")
        elif tag == "a":
            self._pop_format("link")
            href = self._link_href
            self._link_href = ""
            # Show the URL in dim text after the link so reviewers
            # can verify what the click goes to. Trim mailto: prefix
            # to keep it tidy.
            if href:
                disp = href[7:] if href.startswith("mailto:") else href
                self._insert_tagged(f"  ({disp})", "url_hint")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._pop_format("heading")
            self.text.insert("end", "\n")
        elif tag in ("ol", "ul"):
            if self._list_stack:
                self._list_stack.pop()
            self.text.insert("end", "\n")

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        # Collapse runs of whitespace (HTML semantics) but preserve
        # one separator between words.
        collapsed = re.sub(r"\s+", " ", data)
        if not collapsed:
            return
        # Detect any leftover {{var}} placeholders — these mean the
        # template referenced a variable that didn't get a value,
        # which is the kind of leak FERPA review needs to catch.
        var_re = re.compile(r"\{\{\s*(\w+)\s*\}\}")
        idx = 0
        for m in var_re.finditer(collapsed):
            if m.start() > idx:
                self._insert_tagged(
                    collapsed[idx:m.start()], *self._format_stack,
                )
            self._insert_tagged(m.group(0), "unresolved_var")
            self.unresolved_vars.add(m.group(1))
            idx = m.end()
        if idx < len(collapsed):
            self._insert_tagged(
                collapsed[idx:], *self._format_stack,
            )

    def handle_entityref(self, name):
        if self._skip_depth > 0:
            return
        ch = html.unescape(f"&{name};")
        self._insert_tagged(ch, *self._format_stack)

    def handle_charref(self, name):
        if self._skip_depth > 0:
            return
        ch = html.unescape(f"&#{name};")
        self._insert_tagged(ch, *self._format_stack)

    def _pop_format(self, name):
        # Pop the rightmost matching entry (handles nested tags).
        for i in range(len(self._format_stack) - 1, -1, -1):
            if self._format_stack[i] == name:
                del self._format_stack[i]
                return

    def _paragraph_break(self):
        if self._first_block:
            self._first_block = False
            return
        # Avoid stacking multiple blank lines if the previous block
        # already ended with one.
        tail = self.text.get("end-3c", "end-1c")
        if tail.endswith("\n\n"):
            return
        if tail.endswith("\n"):
            self.text.insert("end", "\n")
            return
        self.text.insert("end", "\n\n")

    def _insert_tagged(self, text, *tags):
        if not text:
            return
        start = self.text.index("end-1c")
        self.text.insert("end", text)
        end = self.text.index("end-1c")
        for tag in tags:
            self.text.tag_add(tag, start, end)
