"""Note-body text conversions — plain text <-> the HTML Salesforce stores.

Pure helpers (no GUI): build the paragraph HTML the note-save API wants,
flatten stored note HTML back to readable text, and format a note timestamp.
The ``**bold**`` markdown handling lives in ``src/note_form.py`` (shared with
the DOM form path) and is reused here.
"""
import html
import re
from datetime import datetime


def note_body_to_html(text: str) -> str:
    """Convert a plain-text note body into the simple paragraph HTML the
    Salesforce note-save endpoint stores (each line a ``<p>``; blank lines a
    ``<p><br></p>``) so an API-filed note reads the same as a form-typed one.
    HTML-special characters are escaped; ``**bold**`` markers become
    ``<b>…</b>`` (applied AFTER escaping so the tags survive) — mirrored by
    ``strip_md_bold`` on the DOM form path."""
    from src.note_form import md_bold_to_html
    lines = (text or "").split("\n")
    parts = [
        ("<p>" + md_bold_to_html(html.escape(ln)) + "</p>")
        if ln.strip() else "<p><br></p>"
        for ln in lines
    ]
    return "".join(parts) or "<p><br></p>"


def note_html_to_text(s: str) -> str:
    """Flatten a note body (Salesforce stores some as HTML in the
    data-cell-value attr) to readable plain text."""
    s = s or ""
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*(p|div|li)\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)          # strip remaining tags
    s = html.unescape(s)                    # &nbsp; &amp; etc.
    s = s.replace("\xa0", " ")
    # Collapse runs of blank lines / trailing space.
    lines = [ln.strip() for ln in s.splitlines()]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank and out:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def fmt_note_date(iso: str) -> str:
    """'2026-06-02T17:11:20.000Z' -> '6/02 5:11 PM'. Passes through anything it
    can't parse."""
    s = (iso or "").strip()
    if not s:
        return ""
    try:
        d = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return d.strftime("%m/%d %I:%M %p").lstrip("0")
    except Exception:
        return s[:16].replace("T", " ")
