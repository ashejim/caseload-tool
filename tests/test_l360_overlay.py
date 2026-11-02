"""Stage 2 overlay (P0): snapshot builder (pure) + the injected script, run in
headless Chromium against a MOCK Learner360 page (served locally via
page.route — nothing goes to WGU). Uses no real data and no real DB.

Run: python tests/test_l360_overlay.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import l360_overlay  # noqa: E402
from src.l360_overlay_js import OVERLAY_JS  # noqa: E402

ROWS = [
    {"StudentID": "000123456", "Name": "Pat Lee", "CourseCode": "C769",
     "Task1Status": "Passed", "Task1Date": "2026-10-01", "Task1Count": "1",
     "Task2Status": "Returned", "Task2Date": "2026-10-20", "Task2Count": "2",
     "MomentumRisk": "34.5", "DaysSinceLastContact": "3",
     "LastActionType": "Email", "EssentialAction": "Task unlock",
     "sp:C769:t3prep": "due", "sp:C769:t2prep": "done",
     "DaysSinceCourseActivity": "12"},
    {"StudentID": "000999888", "Name": "Sam Roe", "CourseCode": "D502",
     "MomentumRisk": "", "DaysSinceLastContact": ""},
]
STEPS = {"C769": [("sp:C769:t2prep", "Task 2 prep"),
                  ("sp:C769:t3prep", "Task 3 prep")]}


def test_snapshot_builder():
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=7)
    assert snap["v"] == 7 and set(snap["students"]) == {"123456", "999888"}
    e = snap["students"]["123456"][0]
    assert [t["kind"] for t in e["tasks"]] == ["pass", "return", "none"]
    assert e["risk"] == 34.5 and e["next_step"] == "Task 3 prep"
    assert e["ea"] == "Task unlock" and e["days_contact"] == 3.0
    e2 = snap["students"]["999888"][0]
    assert e2["risk"] is None and e2["next_step"] == ""


PAGE = """<!doctype html><html><body>
<table><thead><tr><th>Name</th><th>ID</th><th>Courses</th></tr></thead>
<tbody id="b">
<tr><td><a href="#">Pat Lee</a></td><td>000123456</td><td>C769</td></tr>
<tr><td><a href="#">Sam Roe</a></td><td>000999888</td><td>D502</td></tr>
<tr><td><a href="#">Not Mine</a></td><td>000555555</td><td>C769</td></tr>
</tbody></table></body></html>"""


def test_overlay_in_headless_chromium():
    from playwright.sync_api import sync_playwright
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=1)
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body=PAGE))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")
        assert pg.evaluate("window.__cnOverlay.version()") == -1
        assert pg.evaluate("d => window.__cnOverlay.setData(d)", snap) == 1
        pg.wait_for_timeout(200)
        n = pg.evaluate("document.querySelectorAll('[data-cn=badge]').length")
        assert n == 2, n                                  # not the 3rd student
        html = pg.evaluate("""() => document.querySelector('[data-cn=badge]')
                                     .shadowRoot.innerHTML""")
        assert "T1" in html and "t-pass" in html and "t-return" in html
        assert "35%" in html and "Task 3 prep" in html and ">EA<" in html
        # Learner360 re-renders its table (paging) → badges come back.
        pg.evaluate("""() => { const b = document.getElementById('b');
            b.innerHTML = '<tr><td><a>Sam Roe</a></td><td>000999888</td>'
                        + '<td>D502</td></tr>'; }""")
        pg.wait_for_timeout(200)
        assert pg.evaluate(
            "document.querySelectorAll('[data-cn=badge]').length") == 1
        assert pg.evaluate("window.__cnOverlay.matched()") == 1
        # Toggle off via the pill → badges removed; state remembered.
        pg.evaluate("""() => document.querySelector('[data-cn=pill]')
                             .shadowRoot.querySelector('.pill').click()""")
        pg.wait_for_timeout(200)
        assert pg.evaluate(
            "document.querySelectorAll('[data-cn=badge]').length") == 0
        # Not active on other hosts.
        ctx.route("https://example.org/**",
                  lambda r: r.fulfill(status=200, body="<html></html>"))
        pg.goto("https://example.org/")
        assert pg.evaluate("typeof window.__cnOverlay") == "undefined"
        b.close()


def test_worker_tick_installs_pushes_and_clears():
    import types
    from playwright.sync_api import sync_playwright
    from src.browser_worker import BrowserWorker as BW
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body=PAGE))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")   # BEFORE overlay on
        w = types.SimpleNamespace(_overlay_on=True, _overlay_ctx_id=None,
                                  _overlay_snap=l360_overlay.build_snapshot(
                                      ROWS, STEPS, version=1))
        w._overlay_pages = BW._overlay_pages.__get__(w)
        BW._overlay_tick(w, ctx)
        pg.wait_for_timeout(200)
        assert pg.evaluate(
            "document.querySelectorAll('[data-cn=badge]').length") == 2
        # New snapshot (risk changed) → next tick re-renders.
        rows2 = [dict(ROWS[0], MomentumRisk="5")] + ROWS[1:]
        w._overlay_snap = l360_overlay.build_snapshot(rows2, STEPS, version=2)
        BW._overlay_tick(w, ctx)
        pg.wait_for_timeout(200)
        html = pg.evaluate("""() => document.querySelector('[data-cn=badge]')
                                     .shadowRoot.innerHTML""")
        assert ">5%<" in html and "r-low" in html
        # A page opened AFTER install gets the script via the init script.
        pg2 = ctx.new_page()
        pg2.goto("https://learner360.wgu.edu/caseload")
        BW._overlay_tick(w, ctx)
        pg2.wait_for_timeout(200)
        assert pg2.evaluate(
            "document.querySelectorAll('[data-cn=badge]').length") == 2
        # Clear (overlay switched off) removes badges.
        BW._overlay_clear(w, ctx)
        pg.wait_for_timeout(200)
        assert pg.evaluate(
            "document.querySelectorAll('[data-cn=badge]').length") == 0
        b.close()


def test_overlay_handles_div_grid_markup():
    from playwright.sync_api import sync_playwright
    grid = """<!doctype html><html><body><div role="grid">
      <div role="rowgroup"><div role="row">
        <div role="cell"><a>Pat Lee</a></div><div role="cell">000123456</div>
        <div role="cell">C769</div></div></div></div></body></html>"""
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body=grid))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")
        pg.evaluate("d => window.__cnOverlay.setData(d)",
                    l360_overlay.build_snapshot(ROWS, STEPS, version=1))
        pg.wait_for_timeout(200)
        assert pg.evaluate(
            "document.querySelectorAll('[data-cn=badge]').length") == 1
        b.close()


UUID = "09eba36d-f48c-4bb5-87bd-3a62200438b5"
EXTRAS = {"uuid_map": {UUID: "123456"},
          "notes": {"123456": [{"date": "2026-10-30", "type": "Email to Student",
                                "author": "Jim Ashe", "direction": "outbound",
                                "text": "Checking in on Task 3"}]},
          "momi": {"123456": [["2026-09-01", 0.4], ["2026-10-01", 0.55],
                              ["2026-10-29", 0.62]]}}


def test_student_panel_on_learner_page():
    from playwright.sync_api import sync_playwright
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=3, extras=EXTRAS)
    assert snap["uuid_map"] == {UUID: "123456"}
    assert [s["status"] for s in snap["students"]["123456"][0]["steps"]] ==         ["done", "due"]
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body="<html><body><h1>Learner</h1>"
                                           "</body></html>"))
        pg = ctx.new_page()
        pg.goto(f"https://learner360.wgu.edu/learner/{UUID}")
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(250)
        html = pg.evaluate("""() => { const h = document.querySelector(
            '[data-cn=panel]'); return h ? h.shadowRoot.innerHTML : ''; }""")
        assert "Pat Lee" in html and "Task 3 prep" in html and "risk 35%" in html
        assert "Checking in on Task 3" in html and "<svg" in html
        assert "Open EA" in html and "now 62" in html
        # SPA navigation away → panel gone; back → panel returns.
        pg.evaluate("history.pushState({}, '', '/caseload')")
        pg.wait_for_timeout(200)
        assert pg.evaluate("!!document.querySelector('[data-cn=panel]')") is False
        pg.evaluate(f"history.pushState({{}}, '', '/learner/{UUID}')")
        pg.wait_for_timeout(200)
        assert pg.evaluate("!!document.querySelector('[data-cn=panel]')") is True
        # Collapse via the tab; remembered.
        pg.evaluate("""() => document.querySelector('[data-cn=panel]').shadowRoot
                             .querySelector('.tab').click()""")
        pg.wait_for_timeout(250)
        assert pg.evaluate("""() => document.querySelector('[data-cn=panel]')
            .shadowRoot.querySelector('.panel').classList.contains('collapsed')""")
        assert pg.evaluate("localStorage.getItem('cnPanelCollapsed')") == "1"
        # Unknown learner → no panel.
        pg.evaluate("history.pushState({}, '', "
                    "'/learner/11111111-2222-3333-4444-555555555555')")
        pg.wait_for_timeout(200)
        assert pg.evaluate("!!document.querySelector('[data-cn=panel]')") is False
        b.close()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
