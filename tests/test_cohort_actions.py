"""Course-cohort batch actions: batch.source / courses and email.cc_aci
(src/scenarios), the editor's "Students from" + "CC the student's assigned CI"
round trip, cohort rows mapped for the batch loop, and ACI CC resolution.
Stubs only — no browser, no Outlook, no DB.

Run: python tests/test_cohort_actions.py
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.scenarios import (  # noqa: E402
    BatchConfig, EmailConfig, ScenarioConfig, _batch_from_dict,
    _email_from_dict, _email_to_dict)


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "launcher_ca_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    return L


def test_config_round_trip():
    b = _batch_from_dict({"filters": [{"column": "OA Used", "op": "is", "value": "0"}],
                          "source": "Cohort", "courses": ["c949", " "]})
    assert b.source == "cohort" and b.courses == ["C949"]
    d = _batch_from_dict({"filters": []})
    assert d.source == "caseload" and d.courses == []
    assert _batch_from_dict({"source": "weird"}).source == "caseload"
    e = _email_from_dict({"subject": "s", "cc_aci": True})
    assert e.cc_aci is True and _email_to_dict(e)["cc_aci"] is True
    assert "cc_aci" not in _email_to_dict(EmailConfig(subject="s"))   # kept clean


def test_cohort_rows_mapped_for_batch_loop():
    L = _launcher()
    logs = []
    from src import cohort_view
    real = cohort_view.load
    try:
        cohort_view.load = lambda courses: [
            {"Name": "", "StudentID": "000111", "CourseCode": "C949",
             "Contact id": "003AAA", "CourseMentor": "Ann CI", "_cohort": True},
            {"Name": "Bo", "StudentID": "000222", "CourseCode": "C949",
             "Contact id": "003BBB", "CourseMentor": "Cy CI", "_cohort": True}]
        a = types.SimpleNamespace(_append_log=lambda m, **kw: logs.append(m))
        scn = ScenarioConfig(name="c949 pre-A", hotkey="", close_tab_after=False,
                             batch=BatchConfig(source="cohort", courses=["C949"]))
        rows = L.App._cohort_batch_rows(a, scn)
        assert [r["contactID"] for r in rows] == ["003AAA", "003BBB"]
        assert rows[0]["Name"] == "000111" and rows[0]["Student ID"] == "000111"
        assert a._cohort_batch_all is rows and "not on your caseload" in logs[-1]
        scn.batch.courses = []
        assert L.App._cohort_batch_rows(a, scn) == [] and "needs a course" in logs[-1]
        cohort_view.load = lambda courses: []
        scn.batch.courses = ["C949"]
        assert L.App._cohort_batch_rows(a, scn) == [] and "l360cohort" in logs[-1]
    finally:
        cohort_view.load = real


def test_aci_cc_resolved_once_per_ci():
    L = _launcher()
    asked, logs = [], []
    known = {"Ann CI": "ann.ci@wgu.edu"}
    a = types.SimpleNamespace(
        _resolve_aci_email=lambda n: known.get(n, ""),
        _prompt_aci_email=lambda n: (asked.append(n) or
                                     ("cy.ci@wgu.edu" if n == "Cy CI" else "")),
        _append_log=lambda m, **kw: logs.append(m))
    rows = [{"_cohort": True, "CourseMentor": "Ann CI"},
            {"_cohort": True, "CourseMentor": "Cy CI"},
            {"_cohort": True, "CourseMentor": "Cy CI"},
            {"_cohort": True, "CourseMentor": "Di CI"},
            {"CourseMentor": "Me"}]                       # caseload: skipped
    scn = ScenarioConfig(name="x", hotkey="", close_tab_after=False,
                         email=EmailConfig(subject="s", cc_aci=True))
    L.App._resolve_cohort_aci_cc(a, scn, rows)
    assert [r.get("_aci_cc", "") for r in rows] == [
        "ann.ci@wgu.edu", "cy.ci@wgu.edu", "cy.ci@wgu.edu", "", ""]
    assert asked == ["Cy CI", "Di CI"]                    # once each, known skipped
    assert any("No email for 1" in m for m in logs)
    # Option off → nothing touched.
    rows2 = [{"_cohort": True, "CourseMentor": "Ann CI"}]
    scn.email.cc_aci = False
    L.App._resolve_cohort_aci_cc(a, scn, rows2)
    assert "_aci_cc" not in rows2[0]


def test_editor_students_from_round_trip():
    import customtkinter as ctk
    L = _launcher()
    root = ctk.CTk()
    root.withdraw()
    try:
        scn = ScenarioConfig(
            name="c949 pre-A", hotkey="", close_tab_after=False,
            email=EmailConfig(subject="Hi", body_html_file="x.html", cc_aci=True),
            batch=BatchConfig(filters=[{"column": "Pre-A First %", "op": "less than",
                                        "value": "83"}],
                              source="cohort", courses=["C949"]))
        seen = []
        ed = L.ScenarioEditor(
            ctk.CTkFrame(root), scn, get_columns=lambda: ["Name", "Risk %"],
            get_cohort_columns=lambda c: seen.append(c) or ["OA Used", "Pre-A First %"])
        root.update()
        assert ed.batch_source_var.get() == "Course cohort"
        assert ed._filter_columns() == ["OA Used", "Pre-A First %"] and seen[-1] == ["C949"]
        out = ed.serialize()
        assert out["batch"]["source"] == "cohort" and out["batch"]["courses"] == ["C949"]
        assert out["email"].get("cc_aci") is True
        ed.batch_source_var.set("My caseload")
        out2 = ed.serialize()
        assert "source" not in out2["batch"]
        assert ed._filter_columns() == ["Name", "Risk %"]
    finally:
        root.destroy()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
