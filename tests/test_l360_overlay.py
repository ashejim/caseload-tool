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


def test_panel_fields_catalog_values_and_picker():
    from playwright.sync_api import sync_playwright
    roster = l360_overlay.roster_fields([
        {"StudentID": "000123456", "CourseCode": "C769", "TermSAP": "Yes",
         "AcademicStanding": "Good Standing", "NameUrl": "https://x",
         "Task1hoverText": "tip"},
        {"StudentID": "000555555", "CourseCode": "C949", "TotalCompletedCU": "88"}])
    assert set(roster["123456"]["C769"]) == {"StudentID", "CourseCode", "TermSAP",
                                             "AcademicStanding"}   # links/tips skipped
    extras = {"uuid_map": {UUID: "123456", U2: "555555"}, "roster": roster,
              "cohort": {"555555": [{"course": "C949", "owner": "X", "team": "T"}]}}
    snap = l360_overlay.build_snapshot(
        ROWS, STEPS, version=5, extras=extras,
        fields=["DaysSinceCourseActivity", "CB:TermSAP", "CB:TotalCompletedCU"])
    cat = dict(snap["field_catalog"])
    assert cat["CB:TermSAP"] == "CB · Term SAP" and "CB:NameUrl" not in cat
    assert "DaysSinceCourseActivity" in cat
    e = snap["students"]["123456"][0]
    assert e["fields"] == [[cat["DaysSinceCourseActivity"], "12"],
                           ["CB · Term SAP", "Yes"]]
    assert snap["students"]["555555"][0]["fields"] == [["CB · Total Completed CU", "88"]]
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
        root = "document.querySelector('[data-cn=panel]').shadowRoot"
        html = pg.evaluate(f"() => {root}.innerHTML")
        assert "CB · Term SAP" in html and "⚙ Fields (3)" in html
        pg.evaluate(f"() => {root}.querySelector('[data-act=pick-fields]').click()")
        pg.wait_for_timeout(200)
        pg.evaluate(f"""() => {{ const s = {root}.querySelector('.fsearch');
            s.value = 'standing'; s.dispatchEvent(new Event('input')); }}""")
        opts = pg.evaluate(f"() => [...{root}.querySelectorAll('.fopt')].map(l => l.textContent.trim())")
        assert opts == ["CB · Academic Standing"], opts
        pg.evaluate(f"() => {root}.querySelector('.fopt input').click()")
        pg.evaluate(f"() => {root}.querySelector('[data-f=save]').click()")
        out = pg.evaluate("window.__cnOverlay.takeOutbox()")
        assert out == [{"type": "panel_fields", "keys": [   # picker (A-Z) order
            "CB:AcademicStanding", "CB:TermSAP", "CB:TotalCompletedCU",
            "DaysSinceCourseActivity"]}], out
        b.close()


MAT_PAGE = """<!doctype html><html><body><div class="table-wrapper">
<table role="table" class="mat-mdc-table cdk-table"><thead role="rowgroup">
<tr role="row" class="mat-mdc-header-row cdk-header-row">
<th class="mat-sort-header mat-mdc-header-cell cdk-header-cell cdk-column-name mat-column-name">Name</th>
<th class="mat-sort-header mat-mdc-header-cell cdk-header-cell cdk-column-studentId mat-column-studentId">Id</th>
<th class="mat-mdc-header-cell cdk-header-cell cdk-column-courses mat-column-courses">Active Courses</th>
</tr></thead><tbody role="rowgroup" id="b">
<tr role="row" class="mat-mdc-row cdk-row"><td class="mat-mdc-cell cdk-cell cdk-column-name mat-column-name"><a>Pat Lee</a></td>
<td class="mat-mdc-cell cdk-cell cdk-column-studentId mat-column-studentId">000123456</td>
<td class="mat-mdc-cell cdk-cell cdk-column-courses mat-column-courses">C769</td></tr>
<tr role="row" class="mat-mdc-row cdk-row"><td class="mat-mdc-cell cdk-cell cdk-column-name mat-column-name"><a>Not Mine</a></td>
<td class="mat-mdc-cell cdk-cell cdk-column-studentId mat-column-studentId">000555555</td>
<td class="mat-mdc-cell cdk-cell cdk-column-courses mat-column-courses">C769</td></tr>
</tbody></table></div></body></html>"""

LIST_ASSESS = {"123456": {"C769": {
    "cut": 65,
    "oa": {"used": 2, "max": 4, "passed": False, "next": None, "sittings": [
        {"n": 1, "date": "2026-09-01", "score": 55, "passed": False},
        {"n": 2, "date": "2026-10-01", "score": None, "passed": False}]},
    "attempts": [{"n": 1, "comps": [
        {"num": "1", "title": "Algorithms", "pct": 40, "rating": "APPROACHING"},
        {"num": "2", "title": "Data", "pct": 80, "rating": "COMPETENT"}]}],
    "pre": {"sittings": 2, "first_pct": 50, "last_pct": 70}}}}


def test_list_column_values():
    snap = l360_overlay.build_snapshot(
        ROWS, STEPS, version=3, extras={"assess": LIST_ASSESS},
        list_cols=["@oa", "@oa1", "@oa2", "@oa3", "@weak", "@pre",
                   "LastActionType", "bogus"])
    keys = [k for k, _ in snap["list_cols"]]
    assert keys == ["@oa", "@oa1", "@oa2", "@oa3", "@weak", "@pre",
                    "LastActionType"]                    # unknown key dropped
    c = snap["students"]["123456"][0]["cols"]
    assert c["@oa"][:2] == ["2/4 · 55", "mid"] and "OA 1 · 2026-09-01" in c["@oa"][2]
    assert c["@oa1"][:2] == ["55", "fail"] and c["@oa2"][:2] == ["✗", "fail"]
    assert "@oa3" not in c                                # no 3rd attempt
    assert c["@weak"][:2] == ["Algorithms · 40%", "fail"]
    assert c["@pre"][:2] == ["70% (2)", "pass"]
    assert c["LastActionType"][0] == "Email"
    assert snap["students"]["999888"][0]["cols"] == {}
    assert snap["list_catalog"][0] == ["@pron", "Pronouns"]
    # No columns chosen → no per-entry column data at all.
    plain = l360_overlay.build_snapshot(ROWS, STEPS, version=3)
    assert plain["list_cols"] == [] and "cols" not in plain["students"]["123456"][0]


def test_list_columns_in_material_table():
    from playwright.sync_api import sync_playwright
    snap = l360_overlay.build_snapshot(
        ROWS, STEPS, version=9, extras={"assess": LIST_ASSESS},
        list_cols=["@oa", "@oa1", "LastActionType"])
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body=MAT_PAGE))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(500)
        heads = pg.evaluate("() => [...document.querySelectorAll('thead th')].map(t => t.textContent.trim())")
        assert heads == ["Name", "Id", "Active Courses", "OA attempts", "OA 1",
                         dict(snap["list_cols"])["LastActionType"],
                         "⚙ Columns (3)"], heads
        cls = pg.evaluate("() => document.querySelector('thead th[data-cn]').className")
        assert "mat-mdc-header-cell" in cls and "cdk-column" not in cls \
            and "mat-sort-header" not in cls
        rows = pg.evaluate("""() => [...document.querySelectorAll('#b tr')].map(tr =>
            [...tr.querySelectorAll('td[data-cn=col]')].map(td => td.textContent.trim()))""")
        assert rows[0] == ["2/4 · 55", "55", "Email", ""], rows
        assert rows[1] == ["", "", "", ""], rows              # not mine: blanks
        tip = pg.evaluate("() => document.querySelector('#b td[data-cn=col]').title")
        assert "OA 1 · 2026-09-01 · score 55" in tip
        # Angular re-renders a page of rows → new <tr>s get the columns too.
        pg.evaluate("""() => { const b = document.getElementById('b');
            const tr = b.rows[0].cloneNode(true);
            tr.querySelectorAll('[data-cn]').forEach(x => x.remove());
            delete tr.dataset.cnCols; b.innerHTML = ''; b.appendChild(tr); }""")
        pg.wait_for_timeout(500)
        again = pg.evaluate("() => [...document.querySelectorAll('#b tr td[data-cn=col]')].map(td => td.textContent.trim())")
        assert again == ["2/4 · 55", "55", "Email", ""], again
        # ⚙ Columns → pick → save → outbox; headers follow at once.
        pg.evaluate("() => document.querySelector('thead th[data-cn] button, thead button').click()")
        pg.wait_for_timeout(200)
        proot = "document.querySelector('[data-cn=colpick]').shadowRoot"
        pg.evaluate(f"""() => {{ const i = [...{proot}.querySelectorAll('input[type=checkbox]')]
            .find(x => x.value === '@pre'); i.click(); }}""")
        pg.evaluate(f"() => {proot}.querySelector('[data-b=save]').click()")
        out = pg.evaluate("window.__cnOverlay.takeOutbox()")
        assert out == [{"type": "list_cols", "set": None, "hide_native": False,
                        "keys": ["@oa", "@oa1", "LastActionType", "@pre"]}], out
        pg.wait_for_timeout(300)
        heads = pg.evaluate("() => [...document.querySelectorAll('thead th[data-cn]')].map(t => t.textContent.trim())")
        assert heads[-2:] == ["Pre-A", "⚙ Columns (4)"], heads
        # Overlay off (pill click) → every added cell is removed.
        pg.evaluate("() => document.querySelector('[data-cn=pill]').shadowRoot.querySelector('.pill').click()")
        pg.wait_for_timeout(400)
        assert pg.evaluate("() => document.querySelectorAll('[data-cn=col]').length") == 0
        b.close()


def test_app_saves_only_known_list_cols():
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location(
        "launcher_lc_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    saved, logs = [], []
    real = L.save_settings
    try:
        L.save_settings = lambda s: saved.append(s.l360_list_cols)
        a = types.SimpleNamespace(
            _caseload_rows=ROWS, _overlay_extras_cache={},
            settings=types.SimpleNamespace(l360_list_cols=""),
            _append_log=lambda m, **kw: logs.append(m),
            _push_overlay_snapshot=lambda: logs.append("push"))
        for n in ("_list_sets", "_clean_list_keys"):
            setattr(a, n, getattr(L.App, n).__get__(a))
        a._LIST_SETS_MAX = 20
        a.settings.l360_list_sets = a.settings.l360_list_set = ""
        L.App._set_list_cols(a, ["@oa", "@oa", "nope", "LastActionType", 7])
        assert saved == ['["@oa", "LastActionType"]'] and "push" in logs
        assert L.App._list_cols(a) == ["@oa", "LastActionType"]
    finally:
        L.save_settings = real


def test_list_column_sets_switch_save_delete():
    from playwright.sync_api import sync_playwright
    sets = {"OA review": ["@oa", "@oa1", "@oa2"], "Outreach": ["LastActionType"],
            "Stale": ["@oa", "gone-field"]}
    snap = l360_overlay.build_snapshot(
        ROWS, STEPS, version=11, extras={"assess": LIST_ASSESS},
        list_cols=["@oa", "@oa1", "@oa2"], list_sets=sets, list_set="OA review")
    assert snap["list_set"] == "OA review"
    assert snap["list_sets"]["Stale"] == ["@oa"]           # unknown key dropped
    assert l360_overlay.build_snapshot(ROWS, STEPS, list_sets=sets,
                                       list_set="nope")["list_set"] == ""
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body=MAT_PAGE))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(500)
        heads = "() => [...document.querySelectorAll('thead th[data-cn]')].map(t => t.textContent.trim())"
        opts = pg.evaluate("() => [...document.querySelectorAll('thead select option')].map(o => [o.value, o.selected])")
        assert opts == [["OA review", True], ["Outreach", False], ["Stale", False]], opts
        # Header switcher → that set's columns at once + a message to the app.
        pg.evaluate("""() => { const s = document.querySelector('thead select');
            s.value = 'Outreach'; s.dispatchEvent(new Event('change')); }""")
        pg.wait_for_timeout(400)
        h = pg.evaluate(heads)
        assert h[0] == dict(snap["list_catalog"])["LastActionType"] and len(h) == 2, h
        assert pg.evaluate("window.__cnOverlay.takeOutbox()") == [
            {"type": "list_set_use", "name": "Outreach"}]
        # Picker: choose a set to edit, add a column, Save “Outreach”.
        proot = "document.querySelector('[data-cn=colpick]').shadowRoot"
        pg.evaluate("() => document.querySelector('thead button').click()")
        pg.wait_for_timeout(200)
        assert pg.evaluate(f"() => {proot}.querySelector('[data-b=save]').textContent")             == "Save “Outreach”"
        pg.evaluate(f"""() => [...{proot}.querySelectorAll('input[type=checkbox]')]
            .find(x => x.value === '@pre').click()""")
        pg.evaluate(f"() => {proot}.querySelector('[data-b=save]').click()")
        assert pg.evaluate("window.__cnOverlay.takeOutbox()") == [
            {"type": "list_cols", "keys": ["LastActionType", "@pre"], "set": "Outreach",
         "hide_native": False}]
        # Save as new set…
        pg.wait_for_timeout(300)
        pg.evaluate("() => document.querySelector('thead button').click()")
        pg.wait_for_timeout(200)
        pg.evaluate(f"() => {proot}.querySelector('[data-b=saveas]').click()")
        pg.evaluate(f"""() => {{ const n = {proot}.querySelector('.nm');
            n.value = '  Weekly   check '; }}""")
        pg.evaluate(f"() => {proot}.querySelector('[data-b=asok]').click()")
        assert pg.evaluate("window.__cnOverlay.takeOutbox()") == [
            {"type": "list_cols", "keys": ["LastActionType", "@pre"], "set": "Weekly check",
         "hide_native": False}]
        pg.wait_for_timeout(300)
        opts = pg.evaluate("() => [...document.querySelectorAll('thead select option')].map(o => o.value)")
        assert "Weekly check" in opts, opts
        # Delete the selected set (columns stay on screen).
        pg.evaluate("() => document.querySelector('thead button').click()")
        pg.wait_for_timeout(200)
        pg.evaluate(f"() => {proot}.querySelector('[data-b=del]').click()")
        assert pg.evaluate("window.__cnOverlay.takeOutbox()") == [
            {"type": "list_set_delete", "name": "Weekly check"}]
        assert pg.evaluate(f"() => {proot}.querySelector('[data-b=save]').textContent") == "Apply"
        b.close()


def test_app_list_sets_save_use_delete():
    import importlib.util
    import json
    import types
    spec = importlib.util.spec_from_file_location(
        "launcher_ls_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    logs = []
    real = L.save_settings
    try:
        L.save_settings = lambda s: None
        st = types.SimpleNamespace(l360_list_cols="", l360_list_sets="",
                                   l360_list_set="")
        a = types.SimpleNamespace(
            _caseload_rows=ROWS, _overlay_extras_cache={}, settings=st,
            _LIST_SETS_MAX=2,
            _append_log=lambda m, **kw: logs.append(m),
            _push_overlay_snapshot=lambda: logs.append("push"))
        for n in ("_list_sets", "_clean_list_keys", "_list_cols"):
            setattr(a, n, getattr(L.App, n).__get__(a))
        L.App._set_list_cols(a, ["@oa", "bad"], "  OA   review ")
        assert json.loads(st.l360_list_sets) == {"OA review": ["@oa"]}
        assert st.l360_list_set == "OA review" and a._list_cols() == ["@oa"]
        L.App._set_list_cols(a, ["LastActionType"], "Outreach")
        L.App._set_list_cols(a, ["@pre"], "Third")            # over the cap
        assert set(json.loads(st.l360_list_sets)) == {"OA review", "Outreach"}
        assert st.l360_list_set == "" and a._list_cols() == ["@pre"]
        L.App._use_list_set(a, "OA review")
        assert a._list_cols() == ["@oa"] and st.l360_list_set == "OA review"
        L.App._use_list_set(a, "missing")                    # ignored
        assert st.l360_list_set == "OA review"
        L.App._delete_list_set(a, "OA review")
        assert set(json.loads(st.l360_list_sets)) == {"Outreach"}
        assert st.l360_list_set == "" and a._list_cols() == ["@oa"]   # cols stay
        L.App._set_list_cols(a, ["@oa1"])                    # plain Apply
        assert st.l360_list_set == "" and a._list_cols() == ["@oa1"]
    finally:
        L.save_settings = real


def test_columns_button_pinned_and_on_unmatched_list():
    from playwright.sync_api import sync_playwright
    ths = "".join(f'<th class="mat-mdc-header-cell cdk-column-c{i}">Col {i}</th>'
                  for i in range(16))
    tds = "".join(f'<td class="mat-mdc-cell cdk-column-c{i}">v</td>' for i in range(16))
    page = f"""<!doctype html><html><body>
    <div class="table-wrapper" style="width:700px;overflow:auto">
    <table class="mat-mdc-table" style="width:2400px"><thead><tr>
    <th class="mat-mdc-header-cell cdk-column-name">Name</th>
    <th class="mat-mdc-header-cell cdk-column-studentId">Id</th>{ths}</tr></thead>
    <tbody><tr><td class="mat-mdc-cell"><a>Nobody</a></td>
    <td class="mat-mdc-cell">000777777</td>{tds}</tr></tbody></table></div></body></html>"""
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=2)   # 777777 not ours
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context(viewport={"width": 1000, "height": 600})
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html", body=page))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(500)
        btn = pg.evaluate("""() => { const b = document.querySelector('thead th[data-cn] button');
            if (!b) return null;
            const r = b.getBoundingClientRect(),
                  w = document.querySelector('.table-wrapper').getBoundingClientRect();
            return {text: b.textContent.trim(), right: r.right, wrapRight: w.right,
                    pos: getComputedStyle(b.closest('th')).position}; }""")
        assert btn and btn["text"] == "⚙ Columns", btn
        assert btn["pos"] == "sticky" and btn["right"] <= btn["wrapRight"] + 1, btn
        b.close()


def test_owl_icon_and_fallback():
    from playwright.sync_api import sync_playwright
    from src import l360_overlay_js as m
    assert m.OVERLAY_JS.count("data:image/png;base64,") == 1
    broken = m._OVERLAY_JS.replace("__CN_OWL__", "data:image/png;base64,AAAA")
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=1, extras=EXTRAS)
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        for js, good in ((m.OVERLAY_JS, True), (broken, False)):
            ctx = b.new_context()
            ctx.add_init_script(js)
            ctx.route("https://learner360.wgu.edu/**",
                      lambda r: r.fulfill(status=200, content_type="text/html",
                                          body="<html><body></body></html>"))
            pg = ctx.new_page()
            pg.goto(f"https://learner360.wgu.edu/learner/{UUID}")
            pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
            pg.wait_for_timeout(700)
            pill = "document.querySelector('[data-cn=pill]').shadowRoot"
            panel = "document.querySelector('[data-cn=panel]').shadowRoot"
            got = pg.evaluate(f"""() => ({{
                owl: !!{pill}.querySelector('img.owl'),
                loaded: ({pill}.querySelector('img.owl') || {{}}).naturalWidth || 0,
                dot: !!{pill}.querySelector('.dot'),
                tab: !!{panel}.querySelector('.tab img.towl') }})""")
            if good:
                assert got["owl"] and got["loaded"] == 64 and not got["dot"] and got["tab"], got
            else:
                assert not got["owl"] and got["dot"] and not got["tab"], got
            ctx.close()
        b.close()


def test_owl_badge_on_wgu_logo():
    from playwright.sync_api import sync_playwright
    head = ('<mat-toolbar style="display:flex;align-items:center;height:56px;padding:0 12px">'
            '<svg width="24" height="24" class="menu-icon"></svg>'
            '<img alt="WGU" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" '
            'style="width:90px;height:32px;margin-left:12px"></mat-toolbar>')
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=1)
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        for body, want in ((head + MAT_PAGE, True), (MAT_PAGE, False)):
            ctx = b.new_context(viewport={"width": 1200, "height": 700})
            ctx.add_init_script(OVERLAY_JS)
            def serve(route, html=body):
                route.fulfill(status=200, content_type="text/html", body=html)
            ctx.route("https://learner360.wgu.edu/**", lambda r: serve(r))
            pg = ctx.new_page()
            pg.goto("https://learner360.wgu.edu/caseload")
            pg.wait_for_timeout(500)
            root = "document.querySelector('[data-cn=owlbadge]')"
            if not want:                       # no logo → no badge
                assert pg.evaluate(f"() => !{root}")
                ctx.close()
                continue
            pos = pg.evaluate(f"""() => {{
                const b = {root}.shadowRoot.querySelector('.b').getBoundingClientRect(),
                      l = document.querySelector('img[alt=WGU]').getBoundingClientRect();
                return {{bl: b.left, br: b.right, bt: b.top, bb: b.bottom,
                         lr: l.right, lt: l.top, lb: l.bottom}}; }}""")
            # Overlaps the logo's right edge, vertically within the logo's band.
            assert pos["bl"] < pos["lr"] < pos["br"], pos
            assert pos["bt"] >= pos["lt"] - 4 and pos["bb"] <= pos["lb"] + 4, pos
            assert "waiting" in pg.evaluate(f"() => {root}.shadowRoot.querySelector('.b').title")
            pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
            pg.wait_for_timeout(300)
            pg.evaluate(f"() => {root}.shadowRoot.querySelector('.b').click()")   # hide
            pg.wait_for_timeout(300)
            st = pg.evaluate(f"""() => [{root}.shadowRoot.querySelector('.b').className,
                document.querySelectorAll('[data-cn=badge]').length]""")
            assert "off" in st[0] and st[1] == 0, st
            ctx.close()
        b.close()


WIDE_PAGE = """<!doctype html><html><body><div class="table-wrapper">
<table class="mat-mdc-table"><thead><tr>
<th class="mat-mdc-header-cell cdk-column-name">Name</th>
<th class="mat-mdc-header-cell cdk-column-studentId" aria-sort="ascending">Id</th>
<th class="mat-mdc-header-cell cdk-column-program">Program</th>
<th class="mat-mdc-header-cell cdk-column-courses">Active Courses</th>
<th class="mat-mdc-header-cell cdk-column-preferredName">Preferred Name</th>
</tr></thead><tbody id="b">
<tr><td class="mat-mdc-cell cdk-column-name"><a>Pat Lee</a></td>
<td class="mat-mdc-cell cdk-column-studentId">000123456</td>
<td class="mat-mdc-cell cdk-column-program">BSCS</td>
<td class="mat-mdc-cell cdk-column-courses">C769</td>
<td class="mat-mdc-cell cdk-column-preferredName">Pat</td></tr>
<tr><td class="mat-mdc-cell cdk-column-name"><a>Not Mine</a></td>
<td class="mat-mdc-cell cdk-column-studentId">000555555</td>
<td class="mat-mdc-cell cdk-column-program">BSIT</td>
<td class="mat-mdc-cell cdk-column-courses">C769</td>
<td class="mat-mdc-cell cdk-column-preferredName">Nim</td></tr>
</tbody></table></div></body></html>"""


def test_why_empty_reasons():
    W = l360_overlay.why_empty
    off = {}
    assert "Course scan" in W("@oa", None, "C949", off)
    assert "click to set" in W("@pron", None, "C949", off)
    on = {"course_scan": True}
    assert W("@oa", None, "C949", on) == "No OA attempts recorded for C949."
    assert "performance-assessed" in W("@oa", None, "C769",
                                       {"types": {"C769": {"oa": False}}})
    assert "Course watch" in W("@weak", {"oa": {}}, "C949", on)
    assert W("@oa3", {"oa": {"used": 2}}, "C949", on) == "No attempt 3 yet."
    assert "Academic activity" in W("DaysSinceCourseActivity", None, "C769", off)
    assert "Degree plans" in W("NextPlannedCourse", None, "C769", off)
    assert W("NextPlannedCourse", None, "C769", {"plans": True}).startswith("Blank")
    assert "course scan" in W("CB:TermSAP", None, "C769", off).lower()


def test_default_layout_hides_moves_and_explains():
    from playwright.sync_api import sync_playwright
    rows = [dict(ROWS[0], MyCourseContact="2026-11-01", Pronouns="")]
    snap = l360_overlay.build_snapshot(
        rows, STEPS, version=4, extras={"assess": {}},
        list_cols=l360_overlay.DEFAULT_LIST_COLS, list_hide_native=True,
        features={})
    e = snap["students"]["123456"][0]
    assert "@pre" in e["why"] and "Course scan" in e["why"]["@pre"]
    assert "@pron" in e["why"] and e["cols"]["MyCourseContact"][0] == "2026-11-01"
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context(viewport={"width": 1400, "height": 600})
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body=WIDE_PAGE))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")
        pg.evaluate("""() => { window.__sorts = 0; document.querySelector(
            'th.cdk-column-studentId').addEventListener('click', () => window.__sorts++); }""")
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(600)
        visible = pg.evaluate("""() => [...document.querySelectorAll('thead th')]
            .filter(t => getComputedStyle(t).display !== 'none').map(t => t.textContent.trim())""")
        aci = dict(snap["list_catalog"])["MyCourseContact"]
        assert visible == ["Name", "Preferred Name", "Pronouns", "Id ▲", "Active Courses",
                           aci, "Pre-A", "OA attempts", "EA (open Essential Action)",
                           "⚙ Columns (8)"], visible
        cells = pg.evaluate("""() => [...document.querySelectorAll('#b tr')].map(tr =>
            [...tr.children].filter(t => getComputedStyle(t).display !== 'none')
              .map(t => [t.textContent.trim(), t.title]))""")
        mine, other = cells
        assert [c[0] for c in mine[:6]] == ["Pat Lee", "Pat", "—", "000123456", "C769",
                                            "2026-11-01"], mine
        assert mine[1][0] == "Pat" and mine[3][0] == "000123456" and mine[4][0] == "C769"
        assert "click to set" in mine[2][1] and "Course scan" in mine[6][1], mine
        assert other[1][0] == "Nim" and other[3][0] == "000555555", other   # copies for all rows
        assert other[2][1].startswith("Not on your caseload"), other
        # Program is hidden; its data is still in the DOM (untouched).
        assert pg.evaluate("() => getComputedStyle(document.querySelector('td.cdk-column-program')).display") == "none"
        # Clicking a moved column's header sorts in Learner360.
        pg.evaluate("""() => [...document.querySelectorAll('thead th[data-cn]')]
            .find(t => t.textContent.startsWith('Id')).click()""")
        assert pg.evaluate("window.__sorts") == 1
        # Click-to-set pronouns from the list.
        pg.evaluate("""() => { const tr = document.querySelector('#b tr');
            [...tr.children].filter(t => t.dataset.cn === 'col')[1].click(); }""")
        pg.wait_for_timeout(200)
        pg.evaluate("""() => [...document.querySelector('[data-cn=pronmenu]').shadowRoot
            .querySelectorAll('button')].find(b => b.dataset.v === 'they/them').click()""")
        assert pg.evaluate("window.__cnOverlay.takeOutbox()") == [
            {"type": "pronouns", "sid": "123456", "value": "they/them"}]
        pg.wait_for_timeout(300)
        pron = pg.evaluate("""() => [...document.querySelector('#b tr').children]
            .filter(t => t.dataset.cn === 'col')[1].textContent.trim()""")
        assert pron == "they/them", pron
        # Overlay off → Learner360's own columns come back, ours go.
        pg.evaluate("() => document.querySelector('[data-cn=pill]').shadowRoot.querySelector('.pill').click()")
        pg.wait_for_timeout(400)
        assert pg.evaluate("() => getComputedStyle(document.querySelector('td.cdk-column-program')).display") != "none"
        assert pg.evaluate("() => document.querySelectorAll('[data-cn=col]').length") == 0
        b.close()


def test_picker_reset_to_recommended_and_learner360_group():
    from playwright.sync_api import sync_playwright
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=5, list_cols=["@oa"])
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body=WIDE_PAGE))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(400)
        pg.evaluate("() => document.querySelector('thead th[data-cn] button, thead button').click()")
        pg.wait_for_timeout(200)
        proot = "document.querySelector('[data-cn=colpick]').shadowRoot"
        groups = pg.evaluate(f"() => [...{proot}.querySelectorAll('.grp')].map(g => g.textContent)")
        assert groups[0] == "Learner360 columns", groups
        opts = pg.evaluate(f"() => [...{proot}.querySelectorAll('input[type=checkbox]')].map(i => i.value)")
        assert "L360:preferredName" in opts and "L360:name" not in opts     # Name always stays
        pg.evaluate(f"() => {proot}.querySelector('[data-b=reset]').click()")
        pg.evaluate(f"() => {proot}.querySelector('[data-b=save]').click()")
        assert pg.evaluate("window.__cnOverlay.takeOutbox()") == [
            {"type": "list_cols", "keys": l360_overlay.DEFAULT_LIST_COLS, "set": None,
             "hide_native": True}]
        b.close()


def test_features_mapping():
    import types
    from src import features
    st = types.SimpleNamespace(l360_overlay_enabled=False, l360_open_on_startup=False,
                               l360_auto_roster_days=1, course_scan_auto=False,
                               course_watch_enabled=False, l360_auto_momi_days=7,
                               l360_auto_notes_days=7, l360_auto_activity_days=7,
                               l360_auto_plan_days=7)
    d = features.defaults()
    assert d == {"l360": True, "roster": True, "course_scan": True,
                 "course_watch": False, "momi": False, "notes": False,
                 "activity": False, "plans": False}
    changed = features.apply(st, d)
    assert st.l360_overlay_enabled and st.l360_open_on_startup and st.course_scan_auto
    assert st.l360_auto_roster_days == 1 and st.l360_auto_momi_days == 0
    assert st.l360_auto_plan_days == 0 and not st.course_watch_enabled
    assert "Team roster (Learner360)" not in changed          # already on
    features.apply(st, {"plans": True})
    assert st.l360_auto_plan_days == 7 and features.is_on(st, "plans")
    assert features.why_off(st, "plans") is None and "MOMI" in features.why_off(st, "momi")


def test_app_accepts_learner360_keys_and_hide_flag():
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location(
        "launcher_nk_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    real = L.save_settings
    try:
        L.save_settings = lambda s: None
        st = types.SimpleNamespace(l360_list_cols="", l360_list_sets="",
                                   l360_list_set="", l360_list_hide_native=False)
        a = types.SimpleNamespace(
            _caseload_rows=ROWS, _overlay_extras_cache={}, settings=st,
            _LIST_SETS_MAX=20, _append_log=lambda m, **kw: None,
            _push_overlay_snapshot=lambda: None)
        for n in ("_list_sets", "_clean_list_keys", "_list_cols"):
            setattr(a, n, getattr(L.App, n).__get__(a))
        L.App._set_list_cols(a, ["L360:preferredName", "L360:bad key", "@pron",
                                 "L360:x;drop"], None, True)
        assert a._list_cols() == ["L360:preferredName", "@pron"]
        assert st.l360_list_hide_native is True
        L.App._set_list_cols(a, ["@oa"])                      # flag left as is
        assert st.l360_list_hide_native is True
    finally:
        L.save_settings = real


EMA = {"C769": {"1": {"course_id": "33860018", "task_id": "4521"}}}
LINKED_ASSESS = {"123456": {"C769": dict(LIST_ASSESS["123456"]["C769"], links={
    "coaching": "https://my.wgu.edu/coaching-report/v4-0/studentPidm/1/assessmentId/2/courseVersionId/3",
    "preA": "https://my.wgu.edu/pre-assessment/report/9",
    "cos": "https://my.wgu.edu/courses/course/3/for/1"})}}


def test_assessment_links_in_snapshot():
    snap = l360_overlay.build_snapshot(
        ROWS, STEPS, version=6, extras={"assess": LINKED_ASSESS},
        list_cols=["@oa", "@oa1", "@pre", "LastActionType"], ema_map=EMA)
    e = snap["students"]["123456"][0]
    t1, t2 = e["tasks"][0], e["tasks"][1]
    assert t1["url"] == ("https://tasks.wgu.edu/student/000123456/course/33860018"
                         "/task/4521/score-report")
    assert t2["url"] == ""                                  # not learned yet
    assert e["cols"]["@oa"][3].startswith("https://my.wgu.edu/coaching-report")
    assert "coaching report" in e["cols"]["@oa1"][2]
    assert e["cols"]["@pre"][3] == "https://my.wgu.edu/pre-assessment/report/9"
    assert len(e["cols"]["LastActionType"]) == 3            # plain field: no link
    for bad in ("http://my.wgu.edu/x", "https://evil.example/x",
                "https://wgu.edu.evil.com/x", "javascript:alert(1)"):
        assert l360_overlay.safe_url(bad) == "", bad
    assert l360_overlay.safe_url("https://tasks.wgu.edu/a") == "https://tasks.wgu.edu/a"


def test_clicking_tasks_and_assessments():
    from playwright.sync_api import sync_playwright
    snap = l360_overlay.build_snapshot(
        ROWS, STEPS, version=7, extras={"assess": LINKED_ASSESS},
        list_cols=["@oa", "@pre"], ema_map=EMA)
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.add_init_script(OVERLAY_JS)
        ctx.add_init_script("window.__opened = []; window.open = (u) => { window.__opened.push(u); return null; };")
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body=MAT_PAGE))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(500)
        chips = "document.querySelector('[data-cn=badge]').shadowRoot"
        pg.evaluate(f"() => {chips}.querySelector('[data-task=\"1\"]').click()")   # learned
        pg.evaluate(f"() => {chips}.querySelector('[data-task=\"2\"]').click()")   # not yet
        assert pg.evaluate("window.__cnOverlay.takeOutbox()") == [
            {"type": "open_task", "sid": "123456", "course": "C769", "n": 2}]
        pg.evaluate("""() => document.querySelector('#b tr td[data-cn=col] span').click()""")
        pg.evaluate("""() => document.querySelectorAll('#b tr')[0]
            .querySelectorAll('td[data-cn=col] span')[1].click()""")
        opened = pg.evaluate("window.__opened")
        assert opened == [("https://tasks.wgu.edu/student/000123456/course/"
                           "33860018/task/4521/score-report"),
                          LINKED_ASSESS["123456"]["C769"]["links"]["coaching"],
                          LINKED_ASSESS["123456"]["C769"]["links"]["preA"]], opened
        b.close()


def test_app_open_task_only_for_my_students():
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location(
        "launcher_ot_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    calls = []
    a = types.SimpleNamespace(
        _caseload_rows=[{"StudentID": "000123456", "Name": "Pat Lee",
                         "CourseCode": "C769"}],
        _append_log=lambda m, **kw: None,
        _open_task_report=lambda *x: calls.append(x),
        root=types.SimpleNamespace(after=lambda ms, fn: fn()))
    L.App._on_overlay_actions(a, [
        {"type": "open_task", "sid": "123456", "course": "C769", "n": 2},
        {"type": "open_task", "sid": "999999", "course": "C769", "n": 1},   # not mine
        {"type": "open_task", "sid": "123456", "course": "", "n": 99},      # bad n
        {"type": "open_task", "sid": "123456", "n": "x"},
        {"type": "open_task", "sid": "123456", "n": 3}])                    # course from row
    assert calls == [("000123456", "C769", 2, "Pat Lee"),
                     ("000123456", "C769", 3, "Pat Lee")], calls


def test_ea_column_click_and_search():
    from playwright.sync_api import sync_playwright
    snap = l360_overlay.build_snapshot(ROWS, STEPS, version=8, list_cols=["@ea"])
    e1, e2 = snap["students"]["123456"][0], snap["students"]["999888"][0]
    assert e1["cols"]["@ea"][:2] == ["Task unlock", "mid"] and e1["cols"]["@ea"][3] == "app:ea"
    assert e2["why"]["@ea"] == "No open Essential Action for this student."
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        ctx.add_init_script(OVERLAY_JS)
        ctx.route("https://learner360.wgu.edu/**",
                  lambda r: r.fulfill(status=200, content_type="text/html",
                                      body=MAT_PAGE))
        pg = ctx.new_page()
        pg.goto("https://learner360.wgu.edu/caseload")
        pg.evaluate("d => window.__cnOverlay.setData(d)", snap)
        pg.wait_for_timeout(500)
        pg.evaluate("() => document.querySelector('#b tr td[data-cn=col] span').click()")
        pg.evaluate("() => document.querySelector('[data-cn=badge]').shadowRoot.querySelector('[data-ea]').click()")
        assert pg.evaluate("window.__cnOverlay.takeOutbox()") == [
            {"type": "open_ea", "sid": "123456"}, {"type": "open_ea", "sid": "123456"}]
        # The picker finds it by "EA".
        pg.evaluate("() => document.querySelector('thead button').click()")
        pg.wait_for_timeout(200)
        proot = "document.querySelector('[data-cn=colpick]').shadowRoot"
        pg.evaluate(f"""() => {{ const q = {proot}.querySelector('.q');
            q.value = 'ea'; q.dispatchEvent(new Event('input')); }}""")
        found = pg.evaluate(f"() => [...{proot}.querySelectorAll('label')].map(l => l.textContent.trim())")
        assert any(t.startswith("EA (open Essential Action)") for t in found), found
        b.close()


def test_app_open_ea_only_for_my_students():
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location(
        "launcher_ea_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    calls = []
    a = types.SimpleNamespace(
        _caseload_rows=[{"StudentID": "000123456", "Name": "Pat Lee"}],
        _append_log=lambda m, **kw: None,
        _open_ea_dashboard=lambda *x: calls.append(x),
        root=types.SimpleNamespace(after=lambda ms, fn: fn()))
    L.App._on_overlay_actions(a, [{"type": "open_ea", "sid": "123456"},
                                  {"type": "open_ea", "sid": "999999"}])
    assert calls == [("000123456", "Pat Lee")], calls


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
