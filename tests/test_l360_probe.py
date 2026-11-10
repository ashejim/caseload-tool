"""`l360probe:` structure probe (src/l360_probe) in headless Chromium against
MOCK pages (served locally via page.route — nothing goes to WGU). Checks the
probe tells a plain table from a virtualized ARIA grid and NEVER returns cell
values (names / ids). Uses no real data and no real DB.

Run: python tests/test_l360_probe.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import l360_overlay  # noqa: E402
from src.l360_overlay_js import OVERLAY_JS  # noqa: E402
from src.l360_probe import PROBE_JS, summarize  # noqa: E402

TABLE = """<!doctype html><html><body><div style="height:200px;overflow:auto">
<table class="mat-mdc-table"><thead><tr><th>Name</th><th>Student ID</th>
<th>Courses</th></tr></thead><tbody>
<tr><td><a href="#">Pat Lee</a></td><td>000123456</td><td>C769</td></tr>
<tr><td><a href="#">Sam Roe</a></td><td>000999888</td><td>D502</td></tr>
</tbody></table></div></body></html>"""

GRID = """<!doctype html><html><body>
<div role="grid" aria-rowcount="250" class="MuiDataGrid-root">
 <div role="row"><div role="columnheader">Name</div>
  <div role="columnheader">Student ID</div></div>
 <div class="MuiDataGrid-virtualScroller" style="height:120px;overflow:auto">
  <div style="height:13000px;position:relative">
   <div role="row" aria-rowindex="2" style="position:absolute;transform:translateY(0px)">
    <div role="gridcell" aria-colindex="1">Pat Lee</div>
    <div role="gridcell" aria-colindex="2">000123456</div></div>
   <div role="row" aria-rowindex="3" style="position:absolute;transform:translateY(52px)">
    <div role="gridcell" aria-colindex="1">Sam Roe</div>
    <div role="gridcell" aria-colindex="2">000999888</div></div>
  </div></div></div></body></html>"""

ROWS = [{"StudentID": "000123456", "Name": "Pat Lee", "CourseCode": "C769"}]
SECRETS = ("Pat Lee", "Sam Roe", "123456", "999888")


def _run(html, overlay=False):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="msedge")
        ctx = b.new_context()
        if overlay:
            ctx.add_init_script(OVERLAY_JS)
        page = ctx.new_page()
        page.route("https://learner360.wgu.edu/**", lambda r: r.fulfill(
            status=200, content_type="text/html", body=html))
        page.goto("https://learner360.wgu.edu/caseload")
        if overlay:
            snap = l360_overlay.build_snapshot(ROWS, {}, version=1)
            page.evaluate("d => window.__cnOverlay.setData(d)", snap)
            page.wait_for_selector('[data-cn="badge"]', state="attached")
        res = page.evaluate(PROBE_JS)
        b.close()
    return res


def test_plain_table_with_overlay():
    res = _run(TABLE, overlay=True)
    blob = json.dumps(res)
    for s in SECRETS:
        assert s not in blob, f"probe leaked {s!r}"
    L = res["lists"][0]
    assert L["el"].startswith("table") and L["has_badges"]
    assert [h["label"] for h in L["headers"]] == ["Name", "Student ID", "Courses"]
    cells = L["row_samples"][0]["cells"]
    assert cells[1]["sid"] == "yes" and cells[0]["badge"] == "yes"
    assert res["prefixes"].get("mat") and res["badges"] == 1
    text = "\n".join(summarize(res))
    assert "headers: Name" in text and "SID" in text
    for s in SECRETS:
        assert s not in text


def test_virtual_grid():
    res = _run(GRID)
    blob = json.dumps(res)
    for s in SECRETS:
        assert s not in blob, f"probe leaked {s!r}"
    L = res["lists"][0]
    assert L["el"].startswith("div[role=grid]") and L["aria"]["rowcount"] == "250"
    r = L["row_samples"][0]
    assert r["style"]["pos"] == "absolute" and r["style"]["tf"] == "yes"
    assert r["rowindex"] == "yes" and "Npx" in r["inline_style"]
    vs = [a for a in L["row_chain"] if a["ov"] == "auto"]
    assert vs and vs[0]["sh"] > vs[0]["ch"]        # the virtual viewport
    assert "Mui" in res["prefixes"]
    text = "\n".join(summarize(res))
    assert "aria-rowcount=250" in text and "between rows+list" in text


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
