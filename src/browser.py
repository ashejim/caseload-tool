from contextlib import contextmanager
from typing import Iterator, Optional

from playwright.sync_api import BrowserContext, sync_playwright

from src.config import BROWSER_DATA_DIR

# What the running session launched (src/browser_engines.resolve) — read by
# the worker (window checks) and the app (Settings / log).
ACTIVE_ENGINE: dict = {}


def _kill_stale_profile_edge(exe: str = "msedge.exe",
                             profile=BROWSER_DATA_DIR) -> None:
    """Kill leftover browser processes (``exe``, default msedge.exe) still
    holding OUR automation profile. When the browser crashes, its background
    helpers can outlive the window and keep the user-data dir locked — the
    next launch then starts and immediately dies, in a loop ("Browser window
    was closed — reopening…" over and over; observed live 2026-08-31). Only
    processes whose command line references our profile dir are touched —
    the user's personal browser is untouched. Windows-only, best-effort,
    silent."""
    import re as _re
    import subprocess
    import sys as _sys
    if not _sys.platform.startswith("win"):
        return
    if not _re.fullmatch(r"[A-Za-z0-9_.-]+\.exe", exe or ""):
        return
    needle = str(profile)
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='" + exe + "'\" | "
          "Where-Object { $_.CommandLine -like '*" + needle + "*' } | "
          "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force "
          "-ErrorAction Stop } catch {} }")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _mark_profile_clean(profile=BROWSER_DATA_DIR) -> None:
    """Reset the profile's crashed-exit flag before launch. After a real
    crash the profile stays marked `exit_type: Crashed` (seen live after the
    2026-08-30 Edge-update crashes), which changes Edge's startup behavior
    (session-restore prompts / recovery paths) for every later session.
    Standard automation hygiene: stamp it Normal pre-launch — the browser
    isn't running at this point (the stale-process sweep just ran)."""
    import json
    prefs = profile / "Default" / "Preferences"
    try:
        with open(prefs, encoding="utf-8") as f:
            data = json.load(f)
        prof = data.setdefault("profile", {})
        if prof.get("exit_type") != "Normal":
            prof["exit_type"] = "Normal"
            prof["exited_cleanly"] = True
            with open(prefs, "w", encoding="utf-8") as f:
                json.dump(data, f)
    except Exception:
        pass


@contextmanager
def persistent_context(headless: bool = False,
                       engine: Optional[dict] = None) -> Iterator[BrowserContext]:
    """Launch the automation browser with a persistent user-data dir so SSO/
    Salesforce cookies survive across runs.

    Microsoft Edge by default — preinstalled on Windows 10/11 and
    full-featured Chromium (codecs, Widevine DRM, background services some
    WGU pages need), unlike Playwright's bundled Chrome for Testing. Settings
    ▸ Automation browser can pick Chrome / Brave / Vivaldi / another Chromium
    .exe instead (src/browser_engines.py; ``engine`` overrides). Each has its
    own profile folder. If the chosen browser won't start, falls back to Edge,
    then to bundled Chromium.
    """
    if engine is None:
        try:
            from src import browser_engines
            from src.config import load_settings
            engine = browser_engines.resolve(load_settings())
        except Exception:
            engine = {}
    profile = engine.get("profile") or BROWSER_DATA_DIR
    exe = engine.get("exe") or "msedge.exe"
    profile.mkdir(parents=True, exist_ok=True)
    # A crashed previous session can leave browser helpers holding the
    # profile lock — clear them so this launch doesn't die instantly.
    _kill_stale_profile_edge(exe, profile)
    _mark_profile_clean(profile)
    # These flags are best-effort mitigations for Chromium backgrounding
    # / throttling behaviors that affect Playwright-launched browsers:
    # - `--disable-blink-features=AutomationControlled` + dropping
    #   `--enable-automation` hide the "I'm a bot" signals so sites
    #   that block automated browsers don't refuse first load.
    # - The four `--disable-*backgrounding*` / occlusion flags target
    #   known throttling issues where user-opened tabs and popups stall
    #   until a Playwright action shifts focus. They do NOT fully fix
    #   the about:blank popup hang on fresh launch — see launcher.py
    #   TODO and the README workaround.
    # Page size: a visible browser follows its WINDOW (no_viewport), so
    # resizing / maximizing it gives pages the room — a fixed viewport pinned
    # every page at 1400×900 whatever the window size. It still opens at the
    # old size (--window-size) so automation sees the width it always had.
    # Headless has no window, so it keeps the fixed viewport.
    size = ({"viewport": {"width": 1400, "height": 900}} if headless
            else {"no_viewport": True})
    launch_kwargs = dict(
        user_data_dir=str(profile),
        headless=headless,
        **size,
        args=[
            *([] if headless else ["--window-size=1400,950"]),
            "--disable-blink-features=AutomationControlled",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            # Keep background/minimized tabs from being throttled or frozen.
            # Edge "sleeping tabs" / efficiency mode freeze a backgrounded tab's
            # JS (we minimize the browser after the caseload loads), which makes
            # JS-driven UI like Mongoose's department dropdown unresponsive until
            # the process is restarted. Disable the Chromium throttles AND Edge's
            # sleeping-tab/efficiency features. Unknown feature names are ignored
            # by Chromium, so the best-guess Edge names are harmless if wrong.
            # msImplicitSignin / msSeamlessWebToBrowserSignIn: when the WGU
            # Microsoft SSO page loads, Edge tries to sign the BROWSER
            # PROFILE into the managed tenant and can RESTART ITSELF to
            # apply org policies — observed live 2026-09-01 as a clean Edge
            # exit ~10s after launch on cold-SSO startups (no crash dump,
            # no event-log entry). Disabling implicit profile sign-in stops
            # the self-restart; web SSO cookies are unaffected.
            "--disable-features="
            "CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,"
            "msSleepingTabs,msEfficiencyMode,HeavyAdIntervention,"
            "msImplicitSignin,msSeamlessWebToBrowserSignIn",
            "--disable-background-mode",
            "--disable-sync",
        ],
        ignore_default_args=["--enable-automation"],
    )
    if engine.get("executable_path"):
        first = {"executable_path": engine["executable_path"]}
    else:
        first = {"channel": engine.get("channel") or "msedge"}
    if first.get("channel") != "msedge":
        # A browser that HANGS on launch (seen with Vivaldi) must fail over to
        # Edge rather than freeze startup — Playwright's default is 3 min.
        first["timeout"] = 45_000
    with sync_playwright() as p:
        ACTIVE_ENGINE.clear()
        try:
            context = p.chromium.launch_persistent_context(**first, **launch_kwargs)
            ACTIVE_ENGINE.update(engine or {"key": "edge", "exe": "msedge.exe"})
        except Exception as e:
            if first.get("channel") == "msedge":
                # Edge launch failed — bundled Chromium so the script at least
                # runs. Some WGU pages may still misbehave.
                context = p.chromium.launch_persistent_context(**launch_kwargs)
                ACTIVE_ENGINE.update(key="bundled", exe="chrome.exe",
                                     note=f"Edge didn't start ({e}).")
            else:
                # The chosen browser didn't start → Edge, in Edge's profile.
                from src import browser_engines
                edge = browser_engines.ENGINES["edge"]
                launch_kwargs["user_data_dir"] = str(BROWSER_DATA_DIR)
                BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
                context = p.chromium.launch_persistent_context(
                    channel="msedge", **launch_kwargs)
                ACTIVE_ENGINE.update(
                    key="edge", exe=edge["exe"], label=edge["label"],
                    note=f"{engine.get('label') or 'The chosen browser'} didn't "
                         f"start ({str(e).splitlines()[0][:120]}) — using Edge.")
        try:
            yield context
        finally:
            context.close()
