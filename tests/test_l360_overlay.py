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
        pg.wait_for_timeout(700)
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
        pg.wait_for_timeout(700)
        assert pg.evaluate(
            "document.querySelectorAll('[data-cn=badge]').length") == 1
        assert pg.evaluate("window.__cnOverlay.matched()") == 1
        # Toggle off via the pill → badges removed; state remembered.
        pg.evaluate("""() => document.querySelector('[data-cn=pill]')
                             .shadowRoot.querySelector('.pill').click()""")
        pg.wait_for_timeout(700)
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
        pg.wait_for_timeout(700)
        assert pg.evaluate(
            "document.querySelectorAll('[data-cn=badge]').length") == 2
        # New snapshot (risk changed) → next tick re-renders.
        rows2 = [dict(ROWS[0], MomentumRisk="5")] + ROWS[1:]
        w._overlay_snap = l360_overlay.build_snapshot(rows2, STEPS, version=2)
        BW._overlay_tick(w, ctx)
        pg.wait_for_timeout(700)
        html = pg.evaluate("""() => document.querySelector('[data-cn=badge]')
                                     .shadowRoot.innerHTML""")
        assert ">5%<" in html and "r-low" in html
        # A page opened AFTER install gets the script via the init script.
        pg2 = ctx.new_page()
        pg2.goto("https://learner360.wgu.edu/caseload")
        BW._overlay_tick(w, ctx)
        pg2.wait_for_timeout(700)
        assert pg2.evaluate(
            "document.querySelectorAll('[data-cn=badge]').length") == 2
        # Clear (overlay switched off) removes badges.
        BW._overlay_clear(w, ctx)
        pg.wait_for_timeout(700)
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
        pg.wait_for_timeout(700)
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
        pg.wait_for_timeout(700)
        html = pg.evaluate("""() => { const h = document.querySelector(
            '[data-cn=panel]'); return h ? h.shadowRoot.innerHTML : ''; }""")
        assert "Pat Lee" in html and "Task 3 prep" in html and "risk 35%" in html
        assert "Checking in on Task 3" in html and "<svg" in html
        assert "Open EA" in html and "now 62" in html
        # SPA navigation away → panel gone; back → panel returns.
        pg.evaluate("history.pushState({}, '', '/caseload')")
        pg.wait_for_timeout(700)
        assert pg.evaluate("!!document.querySelector('[data-cn=panel]')") is False
        pg.evaluate(f"history.pushState({{}}, '', '/learner/{UUID}')")
        pg.wait_for_timeout(700)
        assert pg.evaluate("!!document.querySelector('[data-cn=panel]')") is True
        # Collapse via the tab; remembered.
        pg.evaluate("""() => document.querySelector('[data-cn=panel]').shadowRoot
                             .querySelector('.tab').click()""")
        pg.wait_for_timeout(700)
        assert pg.evaluate("""() => document.querySelector('[data-cn=panel]')
            .shadowRoot.querySelector('.panel').classList.contains('collapsed')""")
        assert pg.evaluate("localStorage.getItem('cnPanelCollapsed')") == "1"
        # Unknown learner → no panel.
        pg.evaluate("history.pushState({}, '', "
                    "'/learner/11111111-2222-3333-4444-555555555555')")
        pg.wait_for_timeout(700)
        assert pg.evaluate("!!document.querySelector('[data-cn=panel]')") is False
        b.close()


ASSESS = {"course": "C949", "cut": 74, "oa_code": "VXO1",
          "oa": {"used": 3, "max": 4, "passed": False, "next": None,
                 "sittings": [{"n": 1, "date": "2026-08-01", "score": 61, "passed": False},
                              {"n": 2, "date": "2026-09-01", "score": 66, "passed": False},
                              {"n": 3, "date": "2026-10-01", "score": 70, "passed": False}]},
          "pre": {"sittings": 2, "rows": [], "first_pct": 58, "last_pct": 80,
                  "last_date": "2026-10-20", "since_last_oa": 1, "code": "PVXO"},
          "coaching": {"date": "2026-10-01", "minutes": 55, "suggested": ["Heaps"],
                       "ratings": [{"num": "4048.4.4", "title": "Explains Algorithms",
                                    "rating": "Approaching"}]},
          "prep": {"attempt": 2, "suggested": ["Hash tables"],
                   "not_competent": ["Explains Algorithms"]},
          "engagement": {},
          "titles": {"4048.4.4": "Explains Algorithms"},
          "types": {"oa": True, "pa": False},
          "attempts": [
              {"n": 1, "date": "2026-08-01", "score": 61, "passed": False,
               "comps": [{"num": "4048.4.4", "title": "Explains Algorithms",
                          "rating": "Approaching", "pct": 55}], "topics": ["Heaps"]},
              {"n": 2, "date": "2026-09-01", "score": 66, "passed": False,
               "comps": [{"num": "4048.4.4", "title": "Explains Algorithms",
                          "rating": "Approaching", "pct": 60}],
               "topics": ["Hash tables"], "minutes": 50},
              {"n": 3, "date": "2026-10-01", "score": 70, "passed": False}],
          "links": {"cos": "https://my.wgu.edu/courses/course/1/for/2",
                    "coaching": "https://my.wgu.edu/coaching-report/x"}}
ASSESS["pre"]["rows"] = [{"n": 1, "date": "2026-07-20", "pct": 58, "passed": False,
                          "comps": {"4048.4.4": 50}},
                         {"n": 2, "date": "2026-10-20", "pct": 80, "passed": True,
                          "comps": {"4048.4.4": 82}}]
U2 = "11111111-2222-4333-8444-555555555555"


def test_cohort_student_panel_with_assessments():
    """A C949 team student (not on my caseload) gets a panel on their
    Learner360 page with the Assessments section, links, the attempt-4 hint
    and a working ↻ refresh; their list row gets an OA badge."""
    from playwright.sync_api import sync_playwright
    extras = {"uuid_map": {U2: "555555"}, "assess": {"555555": {"C949": ASSESS}},
              "cohort": {"555555": [{"course": "C949", "owner": "A CI",
                                     "team": "WST- CS Apps", "program": "BSCS",
                                     "momentum": "Low", "term_end": "2027-01-31",
                                     "departed": ""}]}}
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=3, extras=extras)
    e = snap["students"]["555555"][0]
    assert e["course"] == "C949" and e["assess"]["oa"]["used"] == 3
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
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(700)
        badges = pg.evaluate("""() => [...document.querySelectorAll('[data-cn=badge]')]
                                  .map(h => h.shadowRoot.innerHTML).join('|')""")
        assert "OA 3/4 · 70" in badges, badges            # the 3rd (team) row
        pg.evaluate(f"() => history.pushState(null, '', '/learner/{U2}')")
        pg.wait_for_timeout(700)
        panel = pg.evaluate("""() => { const h = document.querySelector('[data-cn=panel]');
                                     return h ? h.shadowRoot.innerHTML : ''; }""")
        assert "Assessments" in panel and "cut 74" in panel and "3 of 4 used" in panel
        assert "attempt 4" in panel and "Hash tables" in panel
        assert "WST- CS Apps student" in panel and "first 58%" in panel
        assert "Course page" in panel and "Coaching report" in panel
        pg.evaluate("""() => document.querySelector('[data-cn=panel]').shadowRoot
                          .querySelector('[data-act=refresh-assess]').click()""")
        out = pg.evaluate("window.__cnOverlay.takeOutbox()")
        assert out == [{"type": "refresh_assess", "sid": "555555", "course": "C949"}]
        # Summary grid: per-competency % across Pre-A first/last + OA 1-3.
        assert "<table class=\"grid\"" in panel and "Pre 1" in panel and "OA 3" in panel
        assert ">55%<" in panel and ">82%<" in panel
        # Tabs: OA 2 detail, then Pre-A sittings.
        def tab(label):
            pg.evaluate("""l => [...document.querySelector('[data-cn=panel]').shadowRoot
                .querySelectorAll('.tb')].find(b => b.textContent === l).click()""", label)
            pg.wait_for_timeout(300)
            return pg.evaluate("""() => document.querySelector('[data-cn=panel]')
                                     .shadowRoot.innerHTML""")
        t2 = tab("OA 2")
        assert "score 66" in t2 and "Hash tables" in t2 and "50 min" in t2
        tp = tab("Pre-A")
        assert "2026-07-20" in tp and "58%" in tp
        assert "Tasks" not in tp                         # OA course: no PA tab
        b.close()


def test_pa_only_course_box_shows_tasks_not_oa():
    """A PA course (types pa only): tasks move into the Assessments box and
    no OA / pre-A lines appear."""
    extras = {"uuid_map": {UUID: "123456"},
              "assess": {"123456": {"C769": {
                  "course": "C769", "cut": None, "oa_code": "",
                  "oa": {"used": 0, "max": 4, "passed": False, "sittings": [],
                         "next": None},
                  "pre": {"sittings": 0, "rows": []}, "coaching": None,
                  "prep": None, "attempts": [], "titles": {},
                  "types": {"oa": False, "pa": True}, "engagement": {},
                  "links": {"cos": "https://my.wgu.edu/courses/course/9/for/8"}}}}}
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=4, extras=extras)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body="<html><body>x</body></html>"))
        pg = ctx.new_page()
        pg.goto(f"https://learner360.wgu.edu/learner/{UUID}")
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(700)
        html = pg.evaluate("""() => document.querySelector('[data-cn=panel]')
                                 .shadowRoot.innerHTML""")
        box = html[html.index('class="asm"'):]
        assert "Tasks" in box and "T1" in box and "Course page" in box
        assert ">OA<" not in box and "Pre-A" not in box and "of 4 used" not in box
        b.close()


def test_pronoun_edit_round_trip():
    import types
    from playwright.sync_api import sync_playwright
    from src.browser_worker import BrowserWorker as BW
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=4, extras=EXTRAS)
    got = []
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body="<html><body></body></html>"))
        pg = ctx.new_page()
        pg.goto(f"https://learner360.wgu.edu/learner/{UUID}")
        w = types.SimpleNamespace(_overlay_on=True, _overlay_ctx_id=None,
                                  _overlay_snap=snap,
                                  on_overlay_actions=lambda a: got.extend(a))
        w._overlay_pages = BW._overlay_pages.__get__(w)
        BW._overlay_tick(w, ctx)
        pg.wait_for_timeout(700)
        root = "document.querySelector('[data-cn=panel]').shadowRoot"
        assert "pronouns unknown" in pg.evaluate(f"() => {root}.innerHTML")
        pg.evaluate(f"() => {root}.querySelector('[data-act=edit-pron]').click()")
        pg.wait_for_timeout(700)
        pg.evaluate(f"""() => [...{root}.querySelectorAll('.opt')]
                       .find(b => b.dataset.v === 'she/her').click()""")
        pg.wait_for_timeout(700)
        html = pg.evaluate(f"() => {root}.innerHTML")
        assert "she/her" in html and "pronouns unknown" not in html
        BW._overlay_tick(w, ctx)                       # worker collects outbox
        assert got == [{"type": "pronouns", "sid": "123456", "value": "she/her"}]
        BW._overlay_tick(w, ctx)                       # outbox now empty
        assert len(got) == 1
        b.close()


def test_app_applies_only_valid_pronoun_edits():
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location(
        "launcher_ov_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    saved, logs = [], []
    real = L.history.set_student_pronouns
    try:
        L.history.set_student_pronouns = lambda sid, v, **kw: saved.append((sid, v))
        rows = [{"StudentID": "000123456", "Name": "Pat Lee"}]
        a = types.SimpleNamespace(
            _caseload_rows=rows, _append_log=lambda m, **kw: logs.append(m),
            _refresh_caseload_panel=lambda: logs.append("refresh"),
            root=types.SimpleNamespace(after=lambda ms, fn: fn()))
        L.App._on_overlay_actions(a, [
            {"type": "pronouns", "sid": "123456", "value": "  they/them  "},
            {"type": "pronouns", "sid": "999999", "value": "he/him"},  # not mine
            {"type": "delete-everything", "sid": "123456"},            # unknown
            {"type": "pronouns", "sid": "123456", "value": "x" * 99},  # capped
        ])
        assert saved == [("000123456", "they/them"), ("000123456", "x" * 30)]
        assert rows[0]["Pronouns"] == "x" * 30 and "refresh" in logs
    finally:
        L.history.set_student_pronouns = real


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
