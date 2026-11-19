"""Column picker grouping + search aliases (src/column_groups), the picker's
search over them (src/dialogs.prompt_column_picker) and the Pre-A / OA filter
columns (App._apply_assess_columns). No browser, no DB.

Run: python tests/test_column_groups.py
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import column_groups as cg  # noqa: E402


def test_groups_and_aliases():
    g = cg.group_for
    assert g("Pre-A First %") == g("OA Last Score") == g("Task 2") == "Tasks & assessments"
    assert g("Risk %") == "Momentum & risk"
    assert g("Days Since Course Activity") == "Engagement & signals"
    assert g("Next Planned Course") == g("MentorEmail") == "Degree plan & program"
    assert g("Essential Action") == g("Last Assigned CI Contact") == "Outreach & follow-up"
    assert g("DaysSinceLastCourseContact") == "Outreach & follow-up"
    assert g("Pronouns") == g("Student ID") == "Student & contact"
    assert g("Term End Date") == g("Course Instructor") == "Course & term"
    assert g("Zzz Unknown") == "Other"
    assert " ea" in " " + cg.search_text("Essential Action")
    assert "aci" in cg.search_text("Last Assigned CI Contact")
    assert "pre" in cg.search_text("Pre-A First %")
    m = cg.matches
    assert m("ea", cg.search_text("Essential Action"))
    assert not m("ea", cg.search_text("Pre-A First %"))          # no mid-word hit
    assert m("contact", cg.search_text("DaysSinceLastCourseContact"))
    assert m("last contact", cg.search_text("Last Assigned CI Contact"))
    assert m("pre first", cg.search_text("Pre-A First %"))
    assert not m("risk", cg.search_text("Term End Date"))


def test_sections_order_and_items():
    cols = ["Name", "Pre-A First %", "Risk %", "Essential Action", "Term End Date"]
    sp = [{"course": "C769", "label": "Task 2 prep", "column": "sp:C769:t2"}]
    secs = cg.sections(cols, ["Risk %"], sp)
    heads = [h for h, _ in secs]
    assert heads[0] == "★ In current view" and heads[1] == "Success path · C769"
    assert heads[2:] == ["Tasks & assessments", "Momentum & risk",
                         "Outreach & follow-up", "Student & contact", "Course & term"]
    item = dict((h, items) for h, items in secs)["Tasks & assessments"][0]
    assert item[:2] == ("Pre-A First %", "Pre-A First %") and "tasks & assessments" in item[2]


def test_picker_search_matches_alias_and_group():
    import customtkinter as ctk
    from src import dialogs
    root = ctk.CTk()
    root.withdraw()
    try:
        secs = cg.sections(["Essential Action", "Pre-A First %", "Name"], [], [])
        seen = {}

        def drive():
            d = [w for w in root.winfo_children() if isinstance(w, ctk.CTkToplevel)][-1]
            entry = next(w for w in _walk(d) if isinstance(w, ctk.CTkEntry))

            def labels():
                d.update()
                return sorted(w.cget("text") for w in _walk(d)
                              if isinstance(w, ctk.CTkButton) and w.cget("text") != "Cancel")
            entry.insert(0, "ea")
            seen["ea"] = labels()
            entry.delete(0, "end")
            entry.insert(0, "tasks")                       # a group name
            seen["group"] = labels()
            btn = next(w for w in _walk(d) if isinstance(w, ctk.CTkButton)
                       and w.cget("text") == "Pre-A First %")
            btn.invoke()
        root.after(300, drive)
        key = dialogs.prompt_column_picker(root, secs)
        assert seen["ea"] == ["Essential Action"], seen
        assert seen["group"] == ["Pre-A First %"], seen
        assert key == "Pre-A First %"
    finally:
        root.destroy()


def _walk(w):
    for c in w.winfo_children():
        yield c
        yield from _walk(c)


def test_assess_filter_columns():
    spec = importlib.util.spec_from_file_location(
        "launcher_ag_test", os.path.join(ROOT, "scripts", "launcher.py"))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    r = {}
    L.App._apply_assess_columns(r, {
        "pre": {"sittings": 2, "first_pct": 48, "last_pct": 71},
        "oa": {"used": 2, "passed": False, "approval": {"n": 3},
               "sittings": [{"score": 55}, {"score": None}]}})
    assert r == {"PreAFirstPct": 48, "PreALastPct": 71, "PreASittings": 2,
                 "OAUsed": 2, "OALastScore": 55, "OAPassed": "No",
                 "OAApprovalRequested": "Yes"}, r
    blank = {}
    L.App._apply_assess_columns(blank, None)              # course without data
    assert set(blank.values()) == {""}, blank
    from src import caseload_csv
    assert caseload_csv.display_for_column("PreAFirstPct") == "Pre-A First %"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
