"""Hotkey-spec conversions — pure string helpers, no GUI.

The app stores a hotkey as a human spec like ``"Ctrl+Shift+1"`` or ``"F1"``.
These helpers translate that spec into the forms the runtime needs:

- ``to_pynput_hotkey_string`` — pynput's ``HotKey.parse`` syntax, for the
  global listener.
- ``standalone_fkey_vk`` — the Windows virtual-key code for a bare function
  key, used to register a low-level standalone F-key hook.
- ``keysym_to_hotkey_part`` / ``HOTKEY_MOD_ORDER`` — used when *building* a
  spec back up from a live Tk key event (the capture dialog).
"""
from typing import Optional

# Modifier ordering used when composing a spec string (Ctrl+Shift+Alt+Key).
HOTKEY_MOD_ORDER = ("Ctrl", "Shift", "Alt")


def to_pynput_hotkey_string(spec: str) -> str:
    """Convert 'F1' or 'Ctrl+Shift+1' to pynput HotKey.parse syntax."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey spec")
    out = []
    for p in parts:
        if p in ("ctrl", "control"):
            out.append("<ctrl>")
        elif p == "shift":
            out.append("<shift>")
        elif p == "alt":
            out.append("<alt>")
        elif p in ("cmd", "win", "super"):
            out.append("<cmd>")
        elif p.startswith("f") and p[1:].isdigit():
            out.append(f"<{p}>")
        elif len(p) == 1:
            out.append(p)
        else:
            out.append(f"<{p}>")
    return "+".join(out)


def standalone_fkey_vk(spec: str) -> Optional[int]:
    """Windows virtual-key code for a bare function key (F1-F24), or None
    if `spec` isn't a single unmodified function key."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if len(parts) != 1:
        return None
    p = parts[0]
    if p.startswith("f") and p[1:].isdigit():
        n = int(p[1:])
        if 1 <= n <= 24:
            return 0x70 + (n - 1)
    return None


def keysym_to_hotkey_part(ks: str) -> str:
    """Translate a Tk keysym to our hotkey notation."""
    if ks.startswith("F") and ks[1:].isdigit():
        return ks
    if len(ks) == 1:
        return ks.upper()
    return ks
