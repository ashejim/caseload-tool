"""Person-name formatting + loose matching.

Pure text helpers for normalizing a name's capitalization (for template
variables) and tolerantly comparing two names across the shapes the same person
takes in Salesforce vs Outlook.

The active capitalization mode is a small module-level setting the app pushes in
once via ``set_cap_mode`` (at startup and when the preference changes), so both
the App-side and the browser-worker-side variable builders share it without
threading the preference through every call. Encapsulating it behind a setter —
rather than a bare mutable global other modules poke — keeps the shared state in
one place.
"""
import re

# 'off' | 'lower' | 'standard' — see capitalize_name. Set via set_cap_mode.
_cap_mode = "standard"


def set_cap_mode(mode: str) -> None:
    """Set the default capitalization mode used by ``capitalize_name`` when no
    explicit mode is passed. Falls back to 'standard' for a blank/None value."""
    global _cap_mode
    _cap_mode = mode or "standard"


def capitalize_name(name: str, mode=None) -> str:
    """Normalize a name's capitalization for use in template variables.
    ``mode`` (defaults to the current mode set via ``set_cap_mode``):
      'off'      — return the name exactly as stored;
      'lower'    — only fix LOWERCASE entry errors ('john' -> 'John'); leave
                   ALL-CAPS and mixed case alone;
      'standard' — also normalize ALL-CAPS to Title case ('JANE' -> 'Jane'),
                   while PRESERVING intentional mixed case (McDonald, O'Brien,
                   Mary-Jane stay as-is).
    Works per letter-run so hyphen/apostrophe parts are handled
    (mary-jane -> Mary-Jane)."""
    if not name:
        return name
    m = mode or _cap_mode
    if m == "off":
        return name
    if m == "lower":
        return re.sub(r"\b[a-z]", lambda mo: mo.group(0).upper(), name)

    # 'standard': title-case any run that's entirely lower OR entirely upper;
    # leave mixed-case runs untouched so deliberate caps survive.
    def _fix(mo):
        w = mo.group(0)
        if w.islower() or w.isupper():
            return w[:1].upper() + w[1:].lower()
        return w
    return re.sub(r"[A-Za-z]+", _fix, name)


_NAME_TITLES = frozenset({
    "dr", "mr", "mrs", "ms", "prof", "rev", "sir", "madam", "mx",
})


def names_loosely_match(a: str, b: str) -> bool:
    """Tolerant first/last-name comparison. Strips common titles ('Dr.',
    'Prof.', etc.), splits on whitespace + commas, lower-cases, and checks for
    >=2-token overlap.

    Catches the realistic shapes the same person's name takes across Salesforce
    vs Outlook: 'Jim Ashe' vs 'Ashe, Jim', 'Dr. Jim Ashe' vs 'Jim Ashe',
    'Jim Albert Ashe' vs 'Jim Ashe' all match. 'Jim Smith' vs 'Bob Smith' does
    NOT (one-token overlap)."""
    def _tokens(s: str) -> set:
        out: set = set()
        for raw in (s or "").replace(",", " ").split():
            t = raw.strip(".,()[]<>'\"").lower()
            if t and t not in _NAME_TITLES:
                out.add(t)
        return out
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) >= 2
