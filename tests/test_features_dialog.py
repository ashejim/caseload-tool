"""First-run "Choose what runs in the background" dialog (src/features.py +
App._show_features_dialog): builds in a hidden Tk root, Continue applies the
recommended choices + the Learner360 default layout and kicks off the newly
enabled jobs. save_settings is stubbed — the real settings file is never
written. No browser, no DB.

Run: python tests/test_features_dialog.py
"""
import importlib.util
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "launcher_feat_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    return L


def _find(widget, cls, text):
    for w in widget.winfo_children():
        try:
            if isinstance(w, cls) and w.cget("text") == text:
                return w
        except Exception:
            pass
        hit = _find(w, cls, text)
        if hit is not None:
            return hit
    return None


def test_first_run_continue_applies_recommended():
    import customtkinter as ctk
    from src import l360_overlay
    from src.config import Settings
    L = _launcher()
    saved, kicked, closed = [], [], []
    real = L.save_settings
    root = ctk.CTk()
    root.withdraw()
    try:
        L.save_settings = lambda s: saved.append(True)
        st = Settings()              # dataclass defaults (an existing user's
        st.l360_auto_momi_days = 7   # weekly imports were on)
        a = types.SimpleNamespace(
            root=root, settings=st,
            _append_log=lambda m, **kw: kicked.append(("log", m)),
            _apply_overlay_setting=lambda: kicked.append("overlay"),
            _l360_startup_jobs=lambda: None,
            _roster_rearm=lambda ms: kicked.append(("scan", ms)),
            _maybe_course_watch=lambda: None,
            _maybe_auto_l360_roster=lambda: None)
        L.App._show_features_dialog(a, first_run=True,
                                    on_close=lambda: closed.append(True))
        root.update()
        dlg = [w for w in root.winfo_children() if isinstance(w, ctk.CTkToplevel)][-1]
        assert _find(dlg, ctk.CTkCheckBox, "Course watch (assessment details)") is not None
        _find(dlg, ctk.CTkButton, "Continue").invoke()
        root.update()
        assert saved and closed == [True]
        assert st.l360_overlay_enabled and st.l360_open_on_startup
        assert st.course_scan_auto and not st.course_watch_enabled
        assert st.l360_auto_momi_days == 0 and st.l360_auto_roster_days == 1
        assert json.loads(st.l360_list_cols) == l360_overlay.DEFAULT_LIST_COLS
        assert st.l360_list_hide_native is True
        assert "overlay" in kicked and ("scan", 15_000) in kicked
    finally:
        L.save_settings = real
        root.destroy()


def test_help_open_shows_current_state_and_cancel_changes_nothing():
    import customtkinter as ctk
    from src.config import Settings
    L = _launcher()
    saved = []
    real = L.save_settings
    root = ctk.CTk()
    root.withdraw()
    try:
        L.save_settings = lambda s: saved.append(True)
        st = Settings()
        st.course_watch_enabled = True
        a = types.SimpleNamespace(root=root, settings=st)
        L.App._show_features_dialog(a)
        root.update()
        dlg = [w for w in root.winfo_children() if isinstance(w, ctk.CTkToplevel)][-1]
        cb = _find(dlg, ctk.CTkCheckBox, "Course watch (assessment details)")
        assert cb.get() == 1                         # reflects current settings
        assert _find(dlg, ctk.CTkButton, "Save") is not None
        _find(dlg, ctk.CTkButton, "Cancel").invoke()
        root.update()
        assert not saved and st.course_watch_enabled and st.l360_list_cols == ""
    finally:
        L.save_settings = real
        root.destroy()


def test_welcome_browser_choice_switches_now():
    import customtkinter as ctk
    from src import browser_engines
    from src.config import Settings
    L = _launcher()
    saved, calls = [], []
    real_s, real_i = L.save_settings, browser_engines.installed
    root = ctk.CTk()
    root.withdraw()
    try:
        L.save_settings = lambda s: saved.append(s.browser_engine)
        browser_engines.installed = lambda: [("edge", "Microsoft Edge (recommended)"),
                                             ("chrome", "Google Chrome")]
        st = Settings()
        a = types.SimpleNamespace(
            root=root, settings=st,
            _apply_advanced_mode=lambda: None,
            _switch_browser_now=lambda: calls.append("switch"),
            _show_features_dialog=lambda **kw: calls.append("features"))
        L.App._show_first_run_setup(a)
        root.update()
        dlg = [w for w in root.winfo_children() if isinstance(w, ctk.CTkToplevel)][-1]
        menu = next(w for w in _walk(dlg) if isinstance(w, ctk.CTkOptionMenu))
        assert menu.get() == "Microsoft Edge (recommended)"      # Edge preselected
        menu.set("Google Chrome")
        menu._variable.set("Google Chrome")
        _find(dlg, ctk.CTkButton, "Continue").invoke()
        root.update()
        root.after(300, root.quit)
        root.mainloop()
        assert st.browser_engine == "chrome" and saved == ["chrome"]
        assert calls[0] == "switch" and "features" in calls
    finally:
        L.save_settings, browser_engines.installed = real_s, real_i
        root.destroy()


def _walk(w):
    for c in w.winfo_children():
        yield c
        yield from _walk(c)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
