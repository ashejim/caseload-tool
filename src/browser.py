from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import BrowserContext, sync_playwright

from src.config import BROWSER_DATA_DIR


@contextmanager
def persistent_context(headless: bool = False) -> Iterator[BrowserContext]:
    """Launch Microsoft Edge with a persistent user-data dir so SSO/
    Salesforce cookies survive across runs.

    Edge is preinstalled on Windows 10/11 and is full-featured Chromium —
    unlike Playwright's bundled Chrome for Testing build, it has the
    proprietary codecs, Widevine DRM, and background services that some
    WGU pages need. Falls back to bundled Chromium if Edge somehow isn't
    available on the user's machine.
    """
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    launch_kwargs = dict(
        user_data_dir=str(BROWSER_DATA_DIR),
        headless=headless,
        viewport={"width": 1400, "height": 900},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=CalculateNativeWinOcclusion",
        ],
        ignore_default_args=["--enable-automation"],
    )
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                channel="msedge", **launch_kwargs,
            )
        except Exception:
            # Edge launch failed — fall back to bundled Chromium so the
            # script at least runs. Some WGU pages may still misbehave.
            context = p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            yield context
        finally:
            context.close()
