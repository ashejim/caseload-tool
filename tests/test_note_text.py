"""Tests for src/note_text.py — note-body text/HTML conversions."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import note_text  # noqa: E402


def test_note_body_to_html_paragraphs_and_escaping():
    out = note_text.note_body_to_html("Line one\n\nLine <b> two & three")
    assert out == (
        "<p>Line one</p><p><br></p><p>Line &lt;b&gt; two &amp; three</p>")
    # empty body still yields a valid single empty paragraph
    assert note_text.note_body_to_html("") == "<p><br></p>"


def test_note_body_to_html_bold_after_escaping():
    # **bold** becomes real <b>, and the inner text stays escaped (no injection)
    out = note_text.note_body_to_html("**Purpose**: <x>")
    assert out == "<p><b>Purpose</b>: &lt;x&gt;</p>"


def test_note_html_to_text_flattens():
    # NB: only CLOSING block tags add a newline (a quirk of the original) — the
    # opening <p> doesn't, so "Second<p>Third" flattens to "SecondThird".
    html_in = "First line<br>Second<p>Third</p>&amp; done"
    assert note_text.note_html_to_text(html_in) == "First line\nSecondThird\n& done"
    assert note_text.note_html_to_text("") == ""
    # collapses runs of blank lines
    assert note_text.note_html_to_text("a<br><br><br>b") == "a\n\nb"


def test_fmt_note_date():
    # lstrip("0") only trims the leading zero of the month, so the minute keeps
    # its zero ("05:11"). (The docstring's "5:11" is inaccurate — behavior kept.)
    assert note_text.fmt_note_date("2026-06-02T17:11:20.000Z") == "6/02 05:11 PM"
    assert note_text.fmt_note_date("") == ""
    # unparseable -> best-effort passthrough of the leading 16 chars
    assert note_text.fmt_note_date("garbage-value-here") == "garbage-value-he"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
