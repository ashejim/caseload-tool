"""The startup splash — a borderless animated-GIF window shown while the app
boots (browser launch + caseload load). Fully best-effort: if the GIF or Pillow
is unavailable it does nothing, so startup never breaks.
"""
import tkinter as tk
from pathlib import Path

from src.version import (
    __version__,
    GITHUB_URL as _GITHUB_URL,
    AUTHOR_NAME as _AUTHOR_NAME,
    AUTHOR_EMAIL as _AUTHOR_EMAIL,
)


class SplashScreen:
    """A borderless, centered, top-most window that plays an animated GIF while
    the app boots (browser launch + caseload load). Fully best-effort: if the
    GIF or Pillow is unavailable it simply does nothing, so startup never breaks.
    Close it with .close() (idempotent)."""

    def __init__(self, master, gif_path, max_size=460, max_frames=32,
                 loop_ms=3000):
        self.win = None
        self._job = None
        self._frames = []
        self._delays = []
        self._status_lbl = None
        self._progress = None
        try:
            from PIL import Image, ImageTk, ImageSequence
            if not Path(gif_path).exists():
                return
            img = Image.open(str(gif_path))
            total = getattr(img, "n_frames", 1) or 1
            # TRIM: keep ~max_frames evenly sampled (a 100+-frame GIF is slow to
            # load + heavy in memory for a transient splash). Only the kept
            # frames are converted, so the load cost drops with the count.
            step = max(1, total // max_frames)
            for i, frame in enumerate(ImageSequence.Iterator(img)):
                if i % step:
                    continue
                fr = frame.convert("RGBA")
                if max(fr.size) > max_size:      # cap display size
                    s = max_size / max(fr.size)
                    fr = fr.resize((round(fr.width * s), round(fr.height * s)),
                                   Image.BILINEAR)
                self._frames.append(ImageTk.PhotoImage(fr))
            # Even out timing so one full loop runs in ~loop_ms (≈2.5s),
            # regardless of the source GIF's per-frame durations.
            per = max(20, loop_ms // max(1, len(self._frames)))
            self._delays = [per] * len(self._frames)
        except Exception:
            self._frames = []
        if not self._frames:
            return
        try:
            import webbrowser as _wb
            BLUE = "#003057"       # brand navy — the splash border + accents
            WHITE = "#ffffff"
            LINK = "#1a56c4"
            self.win = tk.Toplevel(master)
            self.win.overrideredirect(True)
            try:
                self.win.attributes("-topmost", True)
            except Exception:
                pass
            # Blue border = a colored outer frame with the white content inset.
            outer = tk.Frame(self.win, bg=BLUE)
            outer.pack(fill="both", expand=True)
            inner = tk.Frame(outer, bg=WHITE)
            inner.pack(fill="both", expand=True, padx=4, pady=4)

            def _dismiss(w):
                w.bind("<Button-1>", lambda _e: self.close())

            def _openlink(url, close=True):
                def _h(_e):
                    try:
                        _wb.open(url)
                    except Exception:
                        pass
                    if close:
                        self.close()
                return _h

            _dismiss(inner)
            _dismiss(outer)
            # Title above the owl.
            _t = tk.Label(inner, text="Caseload Tool", bg=WHITE, fg=BLUE,
                          font=("Segoe UI", 20, "bold"))
            _t.pack(pady=(14, 6))
            _dismiss(_t)
            # The animated owl.
            self._lbl = tk.Label(inner, bd=0, highlightthickness=0, bg=WHITE)
            self._lbl.pack(padx=20)
            _dismiss(self._lbl)
            # Startup progress: a status line + a determinate bar, driven by
            # App._splash_step through the boot sequence (login → caseload →
            # Essential Actions → task pass/fail → Mongoose text IDs).
            self._status_lbl = tk.Label(
                inner, text="Starting…", bg=WHITE, fg=BLUE,
                font=("Segoe UI", 10), wraplength=380)
            self._status_lbl.pack(pady=(10, 2))
            _dismiss(self._status_lbl)
            try:
                from tkinter import ttk as _ttk
                self._progress = _ttk.Progressbar(
                    inner, mode="determinate", length=300, maximum=100)
                self._progress.pack(pady=(0, 8))
            except Exception:
                self._progress = None
            # Version + links + author below.
            _v = tk.Label(inner, text=f"Version {__version__}", bg=WHITE,
                          fg="#666666", font=("Segoe UI", 10))
            _v.pack(pady=(8, 4))
            _dismiss(_v)
            for _txt, _url in (
                ("Latest releases & updates", f"{_GITHUB_URL}/releases"),
                ("Report a bug or suggestion", f"{_GITHUB_URL}/issues"),
            ):
                _l = tk.Label(inner, text=_txt, bg=WHITE, fg=LINK, cursor="hand2",
                              font=("Segoe UI", 10, "underline"))
                _l.bind("<Button-1>", _openlink(_url))
                _l.pack(pady=1)
            _n = tk.Label(inner, text=_AUTHOR_NAME, bg=WHITE, fg="#333333",
                          font=("Segoe UI", 10, "bold"))
            _n.pack(pady=(10, 0))
            _dismiss(_n)
            _e = tk.Label(inner, text=_AUTHOR_EMAIL, bg=WHITE, fg=LINK,
                          cursor="hand2", font=("Segoe UI", 10, "underline"))
            _e.bind("<Button-1>", _openlink(f"mailto:{_AUTHOR_EMAIL}"))
            _e.pack(pady=(0, 14))

            self._i = 0
            self._animate()
            # Size to content, centered, on top.
            self.win.update_idletasks()
            w = self.win.winfo_reqwidth()
            h = self.win.winfo_reqheight()
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            self.win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
            try:
                self.win.lift()
            except Exception:
                pass
        except Exception:
            self.close()

    def set_status(self, msg=None, frac=None):
        """Update the startup status line (+ optional 0..1 progress fraction).
        Best-effort and safe to call after close() (no-op then)."""
        if not self.win:
            return
        try:
            if msg is not None and self._status_lbl is not None:
                self._status_lbl.configure(text=msg)
            if frac is not None and self._progress is not None:
                self._progress.configure(
                    value=max(0, min(100, int(round(frac * 100)))))
            self.win.update_idletasks()
        except Exception:
            pass

    def _animate(self):
        if not self.win:
            return
        try:
            self._lbl.configure(image=self._frames[self._i])
            # Play through ONCE then hold the last frame — no loop. The splash
            # stays up (last frame + title/links) until the app closes it when
            # the viewer is ready to interact with.
            if self._i >= len(self._frames) - 1:
                self._job = None
                return
            delay = self._delays[self._i]
            self._i += 1
            self._job = self.win.after(delay, self._animate)
        except Exception:
            pass

    def close(self):
        try:
            if self._job and self.win:
                self.win.after_cancel(self._job)
        except Exception:
            pass
        self._job = None
        try:
            if self.win:
                self.win.destroy()
        except Exception:
            pass
        self.win = None
