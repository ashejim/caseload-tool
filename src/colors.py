"""Colour math for UI theming — pure hex helpers, no widget dependency.

Contrast-text choice, blending/tinting, hover shades, and the batch-scope banner
theme (tinted from a group's colour). Kept out of the GUI file so it's testable
and reusable. All functions accept ``#rgb`` or ``#rrggbb`` and fail soft
(returning the input or a safe default) on anything unparseable.
"""
from typing import Optional


def text_color_for_bg(hex_color: str) -> str:
    """Return '#000000' or '#ffffff' — whichever contrasts better against
    ``hex_color``. Uses the YIQ luminance formula which weights green most
    heavily (matches human perception of brightness)."""
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c + c for c in h)
    if len(h) != 6:
        return "#000000"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return "#000000"
    yiq = (r * 299 + g * 587 + b * 114) / 1000
    return "#000000" if yiq >= 128 else "#ffffff"


def tint_hex(hex_color: str, toward: str, t: float) -> str:
    """Blend ``hex_color`` toward ``toward`` by fraction t (0 -> hex_color,
    1 -> toward). Returns the input unchanged if it can't be parsed."""
    try:
        a = (hex_color or "").lstrip("#")
        b = (toward or "").lstrip("#")
        if len(a) == 3:
            a = "".join(c + c for c in a)
        if len(b) == 3:
            b = "".join(c + c for c in b)
        ch = [round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t)
              for i in (0, 2, 4)]
        return "#%02x%02x%02x" % tuple(ch)
    except Exception:
        return hex_color


# Default (ungrouped) batch-banner palette — the original light blue.
SCOPE_BANNER_DEFAULT = (("#dbe8ff", "#22304a"), ("#1f4e8f", "#cfe0ff"))


def scope_banner_theme(base_hex: Optional[str]):
    """(fg_color, text_color) CTk (light, dark) tuples for the batch banner,
    tinted from a group's base colour: a pale wash in light mode / a muted dark
    wash in dark mode, with a legible same-hue label. Falls back to the default
    light-blue when the action is ungrouped (base_hex falsy / unparseable)."""
    base = (base_hex or "").strip()
    if len(base.lstrip("#")) not in (3, 6):
        return SCOPE_BANNER_DEFAULT
    fg = (tint_hex(base, "#ffffff", 0.82), tint_hex(base, "#1b1b1b", 0.72))
    text = (tint_hex(base, "#000000", 0.45), tint_hex(base, "#ffffff", 0.60))
    return (fg, text)


def hover_color_for(hex_color: str) -> str:
    """Slightly darker version of ``hex_color`` for a hover state. Returns the
    input unchanged if it can't be parsed."""
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c + c for c in h)
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    f = 0.82
    return f"#{int(r * f):02x}{int(g * f):02x}{int(b * f):02x}"
