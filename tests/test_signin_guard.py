"""Sign-in protection (handoff docs/CHROME_BROWSER_HANDOFF.md): while a WGU /
Salesforce sign-in page is open the worker holds navigating commands, the
caseload loader never jumps away from a sign-in page, and session tokens are
masked in the activity log. Stubs only — no browser, no network, no DB.

Run: python tests/test_signin_guard.py
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import browser_worker  # noqa: E402

BW = browser_worker.BrowserWorker


class Page:
    def __init__(self, url):
        self.url = url
        self.gotos = []

    def goto(self, url, **kw):
        self.gotos.append(url)

    def locator(self, sel):
        return types.SimpleNamespace(count=lambda: 0)


def _worker():
    logs = []
    w = types.SimpleNamespace(on_status=logs.append, _raise_browser_window=lambda: None)
    for n in ("_signin_in_progress", "_hold_for_signin"):
        setattr(w, n, getattr(BW, n).__get__(w))
    for n in ("_SIGNIN_URL_MARKERS", "_SIGNIN_SAFE_CMDS", "_SIGNIN_WAIT_MAX_S"):
        setattr(w, n, getattr(BW, n))
    return w, logs


def test_hold_while_signing_in_then_resume():
    w, logs = _worker()
    ctx = types.SimpleNamespace(pages=[
        Page("https://wgu.lightning.force.com/lightning/n/Caseload_App_Page"),
        Page("https://authenticator.pingone.com/pingid/ppm/auth?x=1")])
    assert w._hold_for_signin(ctx, ("COURSE_ASSESS", "C949", [], None)) is True
    assert any("Waiting for you to finish signing in" in m for m in logs), logs
    assert w._hold_for_signin(ctx, ("L360_PROBE", None)) is False   # never navigates
    assert w._hold_for_signin(ctx, ("L360_OPEN", None, True)) is True
    assert sum("Waiting" in m for m in logs) == 1                   # logged once
    ctx.pages[1].url = "https://wgu.lightning.force.com/lightning/page/home"
    assert w._hold_for_signin(ctx, ("L360_OPEN", None, True)) is False
    assert logs[-1].startswith("✓ Signed in")
    # Mongoose's own sign-in page (generic /login) doesn't pause everything.
    ctx.pages[1].url = "https://app.mongooseresearch.com/login"
    assert w._hold_for_signin(ctx, ("EA_SCRAPE",)) is False


def test_stale_signin_tab_stops_blocking():
    w, logs = _worker()
    ctx = types.SimpleNamespace(pages=[Page("https://access.wgu.edu/pingfed/x")])
    assert w._hold_for_signin(ctx, ("X",)) is True
    w._signin_wait_t0 -= BW._SIGNIN_WAIT_MAX_S + 1
    assert w._hold_for_signin(ctx, ("X",)) is False
    assert any("10+ minutes" in m for m in logs)


def test_caseload_loader_does_not_navigate_from_signin_page():
    logs = []
    pg = Page("https://access.wgu.edu/pingfed/idp/SSO.ping")
    ctx = types.SimpleNamespace(pages=[pg], new_page=lambda: pg)
    w = types.SimpleNamespace(on_status=logs.append, _raise_browser_window=lambda: None,
                              _active_page=lambda c: pg,
                              _dismiss_open_modals=lambda t: [],
                              _await_caseload_list=lambda t: (_ for _ in ()).throw(
                                  RuntimeError("no table")))
    w._looks_like_login = BW._looks_like_login.__get__(w)
    target, table = BW._open_caseload_table(w, ctx)
    assert pg.gotos == [], pg.gotos                     # SSO flow left alone
    assert target is None and any("Not signed in" in m for m in logs), logs


def test_log_redacts_session_tokens():
    spec = importlib.util.spec_from_file_location(
        "launcher_redact_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    r = L._redact_secrets
    line = ("Page.goto interrupted by another navigation to https://wgu.my.salesforce"
            ".com/secur/frontdoor.jsp?sid=00D5w0000ABC!AQ0AQ.Xyz123&retURL=%2Fhome")
    out = r(line)
    assert "00D5w0000ABC" not in out and "sid=…" in out and "retURL=%2Fhome" in out
    assert r("x?code=4/0AbCdEf&state=1") == "x?code=…&state=1"
    assert "eyJhbGciOi" not in r("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc")
    plain = "Using course code (manual): C769 — score=55, Task 2 passed"
    assert r(plain) == plain


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
