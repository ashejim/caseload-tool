"""Tests for src/colors.py — pure colour-math helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import colors  # noqa: E402


def test_text_color_for_bg():
    assert colors.text_color_for_bg("#ffffff") == "#000000"   # light bg -> black
    assert colors.text_color_for_bg("#000000") == "#ffffff"   # dark bg -> white
    assert colors.text_color_for_bg("#fff") == "#000000"      # 3-digit form
    # unparseable -> safe default (black)
    assert colors.text_color_for_bg("") == "#000000"
    assert colors.text_color_for_bg("nothex") == "#000000"


def test_tint_hex_endpoints_and_midpoint():
    assert colors.tint_hex("#000000", "#ffffff", 0.0) == "#000000"
    assert colors.tint_hex("#000000", "#ffffff", 1.0) == "#ffffff"
    assert colors.tint_hex("#000000", "#ffffff", 0.5) == "#808080"
    # unparseable input passes through unchanged ("nothex" isn't valid hex;
    # note "bad" WOULD parse as the 3-digit colour #bbaadd)
    assert colors.tint_hex("nothex", "#ffffff", 0.5) == "nothex"


def test_hover_color_for_darkens():
    # 0.82 factor: int(0xff * 0.82) == 209 == 0xd1
    assert colors.hover_color_for("#ffffff") == "#d1d1d1"
    assert colors.hover_color_for("badcolor") == "badcolor"   # passthrough


def test_scope_banner_theme():
    # ungrouped / unparseable -> the default palette
    assert colors.scope_banner_theme("") is colors.SCOPE_BANNER_DEFAULT
    assert colors.scope_banner_theme(None) is colors.SCOPE_BANNER_DEFAULT
    # a real colour yields ((light,dark) fg, (light,dark) text) hex tuples
    fg, text = colors.scope_banner_theme("#2f75c9")
    assert len(fg) == 2 and len(text) == 2
    assert all(c.startswith("#") and len(c) == 7 for c in (*fg, *text))


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
