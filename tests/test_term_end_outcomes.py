"""Tests for history.term_end_outcomes — the snapshot-based pass/non-pass
classifier that reproduces WGU's official pass rate where the archive can't.

Uses a temp DB seeded with synthetic snapshots + outcomes so the classification
(passed / notpass_term_end / early_exit / in_progress) and the window/rate math
are pinned down without touching the real history.db.

Run: python tests/test_term_end_outcomes.py
"""
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import history  # noqa: E402

ASOF = "2026-09-14"          # newest snapshot date the tests pin "today" to
_checks = 0


def _eq(a, b, msg):
    global _checks
    _checks += 1
    if a != b:
        raise AssertionError(f"{msg}: got {a!r}, expected {b!r}")


def _seed(conn, snaps, outcomes):
    """snaps: list of (sid, course, collected_date, extra{}). outcomes: list of
    (sid, course, outcome, pass_date)."""
    conn.executescript(history._SCHEMA_DDL)
    for sid, cc, d, extra in snaps:
        conn.execute(
            "INSERT INTO snapshots (collected_at, collected_date, student_id, "
            "course_code, name, latest_task_status, extra_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (d + "T00:00:00", d, sid, cc, f"Student {sid}",
             extra.get("_task", ""), json.dumps(extra)))
    for sid, cc, oc, pd in outcomes:
        # minimal outcomes row (only the columns term_end_outcomes reads)
        conn.execute(
            "INSERT INTO outcomes (student_id, course_code, name, outcome, "
            "pass_date) VALUES (?,?,?,?,?)",
            (sid, cc, f"Student {sid}", oc, pd))
    conn.commit()


def _run(snaps, outcomes, **kw):
    # Anchor an active student seen at ASOF (distinct course 'ZZZ', far-future
    # term end) so the DB's newest snapshot pins asof=ASOF — mirroring the real
    # world where the app runs regularly. Without it, a temp DB whose latest
    # snapshot predates "today" would make departure detection impossible (and
    # rightly so). The anchor is always in_progress and, on a distinct course,
    # never lands in a windowed/course-filtered count.
    snaps = list(snaps) + [("ANCHOR", "ZZZ", ASOF,
                            {"TermEndDate": "2027-12-31"})]
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        _seed(conn, snaps, outcomes)
        conn.close()
        return history.term_end_outcomes(db_path=path, **kw)
    finally:
        os.remove(path)


def test_passer_dated_by_pass_date():
    # A passer resolves on pass_date, regardless of snapshot term end.
    r = _run(
        [("1", "C769", "2026-07-10", {"TermEndDate": "2026-11-30"})],
        [("1", "C769", "passed", "2026-07-15")],
        date_from="2026-07-01", date_to="2026-07-31")
    _eq(r["passed"], 1, "passer counted")
    _eq(r["notpass_term_end"], 0, "passer not a non-pass")
    _eq(r["term_end_rate"], 100.0, "all-pass rate")


def test_term_end_nonpasser_from_snapshot_only():
    # Never in outcomes; term end reached, still on caseload as of asof -> a
    # term-end non-passer dated by the effective end. This is the archive's blind
    # spot that snapshots recover.
    r = _run(
        [("2", "C769", ASOF, {"TermEndDate": "2026-08-31"})],
        [],
        date_from="2026-08-01", date_to="2026-08-31")
    _eq(r["notpass_term_end"], 1, "snapshot-only non-passer caught")
    _eq(r["early_exit"], 0, "not an early exit (present through term end)")
    _eq(r["term_end_rate"], 0.0, "0/1 pass rate")


def test_ic_end_overrides_term_end():
    # Effective end = IC date when present (earlier deadline).
    r = _run(
        [("3", "C769", ASOF,
          {"TermEndDate": "2026-12-31", "Icenddate": "2026-08-15"})],
        [], date_from="2026-08-01", date_to="2026-08-31")
    _eq(r["notpass_term_end"], 1, "resolved by IC end, in window")
    _eq(r["students"]["notpass_term_end"][0]["effective_end"], "2026-08-15",
        "effective end uses IC date")


def test_early_exit_left_well_before_term_end():
    # Fell off the caseload in July with a November term end -> early_exit,
    # dated by last-seen, and NOT in the term-end rate.
    r = _run(
        [("4", "C769", "2026-07-05", {"TermEndDate": "2026-11-30"})],
        [], date_from="2026-07-01", date_to="2026-07-31")
    _eq(r["early_exit"], 1, "early departure caught")
    _eq(r["notpass_term_end"], 0, "not a term-end non-pass")
    _eq(r["term_end_rate"], None, "no term-end cohort in window")


def test_early_exit_excluded_from_headline_rate():
    # One passer + one early exit in-window: headline rate ignores the early
    # exit; the incl-early rate does not.
    r = _run(
        [("5", "C769", "2026-07-20", {"TermEndDate": "2026-12-31"}),
         ("6", "C769", "2026-07-10", {"TermEndDate": "2026-12-31"})],
        [("5", "C769", "passed", "2026-07-20")],
        date_from="2026-07-01", date_to="2026-07-31")
    _eq(r["passed"], 1, "one passer")
    _eq(r["early_exit"], 1, "one early exit")
    _eq(r["term_end_rate"], 100.0, "headline rate excludes early exit")
    _eq(round(r["rate_incl_early"], 1), 50.0, "incl-early rate counts it")


def test_in_progress_excluded():
    # Still enrolled, term end in the future -> excluded entirely.
    r = _run(
        [("7", "C769", ASOF, {"TermEndDate": "2027-01-31"})],
        [], courses=["C769"])   # filter out the harness anchor (course ZZZ)
    _eq(r["in_progress_excluded"], 1, "future term end + present = in progress")
    _eq(r["passed"] + r["notpass_term_end"] + r["early_exit"], 0,
        "nothing resolved")


def test_window_filters_by_resolution_date():
    # Two term-end non-passers, only one inside the window.
    snaps = [("8", "C769", ASOF, {"TermEndDate": "2026-08-31"}),
             ("9", "C769", ASOF, {"TermEndDate": "2026-05-31"})]
    r = _run(snaps, [], date_from="2026-08-01", date_to="2026-08-31")
    _eq(r["notpass_term_end"], 1, "only the August term end is in window")


def test_course_filter():
    snaps = [("10", "C769", ASOF, {"TermEndDate": "2026-08-31"}),
             ("11", "C964", ASOF, {"TermEndDate": "2026-08-31"})]
    r = _run(snaps, [], courses=["C769"],
             date_from="2026-08-01", date_to="2026-08-31")
    _eq(r["notpass_term_end"], 1, "course filter restricts cohort")
    _eq(r["courses"], ["C769"], "only filtered course present")


def test_archived_notpass_future_term_is_early_exit():
    # An archived not_passed whose term end is still in the future = an early
    # departure (withdrawal), not a term-end non-pass.
    r = _run(
        [("12", "C769", "2026-07-08", {"TermEndDate": "2026-11-30"})],
        [("12", "C769", "not_passed", "")],
        date_from="2026-07-01", date_to="2026-07-31")
    _eq(r["early_exit"], 1, "withdrawal with future term end -> early exit")
    _eq(r["notpass_term_end"], 0, "not counted at term end")


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests, {_checks} checks passed")


if __name__ == "__main__":
    _main()
