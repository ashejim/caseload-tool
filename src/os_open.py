"""Open URIs and files in external applications.

Small OS-integration helpers used by the editor/preview dialogs: launch a URL
in Edge, hand a file to its default app, or open a template in Word. Kept out
of the GUI modules so they're reusable and testable in isolation. Windows-only
behaviour; the win32/Word path is lazy-imported so the module loads anywhere.
"""
from pathlib import Path


def _open_in_edge(uri: str) -> bool:
    """Launch Microsoft Edge with `uri`. If Edge is already running,
    the URL opens as a new tab (standard Edge behavior on Windows);
    otherwise a fresh Edge process opens. Returns True on success.

    We try in order: explicit msedge.exe path → shell `start msedge`
    → fall through to the user's default browser. The standard-
    install paths cover the vast majority of Windows machines; the
    `start` fallback handles Edge installed somewhere unusual but
    still registered with the shell."""
    import subprocess
    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for exe in edge_paths:
        if Path(exe).exists():
            try:
                subprocess.Popen([exe, uri])
                return True
            except Exception:
                continue
    try:
        # `start` lets the shell resolve msedge from registered apps;
        # works for portable installs and non-default locations.
        subprocess.Popen(["cmd", "/c", "start", "", "msedge", uri], shell=False)
        return True
    except Exception:
        pass
    return False


def _open_externally(path: Path) -> tuple[bool, str]:
    """Open `path` in whatever app the OS has associated with the
    file type (`os.startfile` on Windows). Lets users pick their
    own HTML editor — set VS Code / Notepad++ / Sublime / etc. as
    the default for .html in Windows Settings and clicks here will
    route there. Returns (success, message)."""
    try:
        import os
        os.startfile(str(path))
        return True, "Opened in default app."
    except Exception as e:
        return False, f"Couldn't open file: {e}"


def _open_template_in_word(path: Path) -> tuple[bool, str]:
    """Launch MS Word (via COM) opened to `path`. Returns
    (success, message). Falls back gracefully when Word isn't
    installed — caller can fall back to os.startfile or just
    show the message.

    NOTE: kept for backward-compat and as an escape-hatch path,
    but the main editor flow now uses `_open_externally` which is
    more reliable and lets users pick any editor via the .html
    file association."""
    try:
        import win32com.client
        word = win32com.client.Dispatch("Word.Application")
        word.Documents.Open(str(path))
        word.Visible = True
        return True, "Opened in Word."
    except Exception as e:
        return False, f"Word not available: {e}"
