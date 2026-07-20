"""Tests for src.student_lookup.task_info_from_grid — the fallback that recovers
a grid task's pass/fail when its TaskNhoverText tooltip is missing/unparseable,
so a passed student isn't dropped out of the caseload pass/fail scan (and hence
their Success Path). See the browser worker's _grid_rows_to_task_status.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.student_lookup import task_info_from_grid  # noqa: E402


def test_status_word_wins():
    info = task_info_from_grid("Passed", "2026-07-17 (1)")
    assert info["state"] == "passed", info
    assert info["date"] == "2026-07-17"
    assert info["attempts"] == 1


def test_returned_and_in_process_words():
    assert task_info_from_grid("Returned", "05/07/2026 (2)")["state"] == "returned"
    assert task_info_from_grid("In Process", "06/30/2026 (0)")["state"] == "pending"
    assert task_info_from_grid("Revisions Needed", "1/2/2026 (1)")["state"] \
        == "returned"


def test_glyph_fallback_when_no_status_word():
    # The grid cell carries a status glyph the CSV strips; use it when there's
    # no status word.
    assert task_info_from_grid("", "2026-07-17✓ (1)")["state"] == "passed"
    assert task_info_from_grid("", "⌛06/30/2026 (0)")["state"] == "pending"


def test_dated_cell_without_status_or_glyph_counts_as_submitted():
    info = task_info_from_grid("", "2026-07-17 (1)")
    assert info["state"] == "submitted", info
    assert info["date"] == "2026-07-17"


def test_empty_cell_returns_none():
    assert task_info_from_grid("", "") is None
    assert task_info_from_grid("", "   ") is None
    assert task_info_from_grid(None, None) is None


def test_status_word_beats_glyph():
    # A status word is authoritative even if a (stale) glyph disagrees.
    assert task_info_from_grid("Returned", "2026-07-17✓ (2)")["state"] == "returned"


def test_attempts_and_date_parsed_from_cell():
    info = task_info_from_grid("Passed", "07/17/2026 (3)")
    assert info["date"] == "07/17/2026"
    assert info["attempts"] == 3
    # No attempt count -> 0.
    assert task_info_from_grid("Passed", "2026-07-17")["attempts"] == 0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
