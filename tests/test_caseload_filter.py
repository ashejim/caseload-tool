"""Tests for the "Task N" facet-routing helpers in src.caseload_filter.

A single visible "Task N" filter column is rewritten at eval time to one of
three hidden facets (Task{N}Date / Task{N}Count / Task{N}Status) based on the
operator and, for ambiguous text ops, the value. See rewrite_task_filter.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import caseload_filter  # noqa: E402


# --- is_task_facet_col -----------------------------------------------------

def test_is_task_facet_col_matches_hidden_facets():
    assert caseload_filter.is_task_facet_col("Task1Date")
    assert caseload_filter.is_task_facet_col("Task2Count")
    assert caseload_filter.is_task_facet_col("Task12Status")


def test_is_task_facet_col_rejects_visible_and_other():
    assert not caseload_filter.is_task_facet_col("Task 1")
    assert not caseload_filter.is_task_facet_col("Task1")
    assert not caseload_filter.is_task_facet_col("StudentID")
    assert not caseload_filter.is_task_facet_col("")
    assert not caseload_filter.is_task_facet_col(None)


# --- resolve_filter_columns ------------------------------------------------

def test_resolve_filter_columns_resolves_column_and_ref_value():
    headers = ["StudentID", "MomentumScore"]
    # Display name resolves to the CSV header (identity here — real resolution
    # is covered in caseload_csv's own tests; this checks the wiring + the
    # {ref} value path).
    f = {"column": "StudentID", "op": "equals", "value": "{MomentumScore}"}
    out = caseload_filter.resolve_filter_columns(f, headers)
    assert out["value"] == "{MomentumScore}"
    assert out["op"] == "equals"  # untouched keys pass through


def test_resolve_filter_columns_plain_value_untouched():
    f = {"column": "StudentID", "op": "contains", "value": "012"}
    out = caseload_filter.resolve_filter_columns(f, ["StudentID"])
    assert out["value"] == "012"


# --- rewrite_task_filter ---------------------------------------------------

def test_rewrite_non_task_passes_through():
    f = {"column": "StudentID", "op": "is", "value": "x"}
    assert caseload_filter.rewrite_task_filter(f) is f


def test_rewrite_date_op_routes_to_date_facet():
    out = caseload_filter.rewrite_task_filter(
        {"column": "Task2", "op": "is after", "value": "2026-01-01"})
    assert out["column"] == "Task2Date"


def test_rewrite_numeric_op_routes_to_count_facet():
    out = caseload_filter.rewrite_task_filter(
        {"column": "Task3", "op": "at least", "value": "2"})
    assert out["column"] == "Task3Count"


def test_rewrite_empty_op_routes_to_date_facet():
    out = caseload_filter.rewrite_task_filter(
        {"column": "Task1", "op": "is not empty", "value": ""})
    assert out["column"] == "Task1Date"


def test_rewrite_text_op_integer_value_routes_to_count():
    out = caseload_filter.rewrite_task_filter(
        {"column": "Task2", "op": "is", "value": "2, 3"})
    assert out["column"] == "Task2Count"


def test_rewrite_text_op_submitted_routes_to_date_presence():
    out = caseload_filter.rewrite_task_filter(
        {"column": "Task2", "op": "is", "value": "Submitted"})
    assert out == {"column": "Task2Date", "op": "is not empty", "value": ""}
    # Negated form flips to is-empty.
    neg = caseload_filter.rewrite_task_filter(
        {"column": "Task2", "op": "is not", "value": "Submitted"})
    assert neg == {"column": "Task2Date", "op": "is empty", "value": ""}


def test_rewrite_text_op_status_word_routes_to_status():
    out = caseload_filter.rewrite_task_filter(
        {"column": "Task2", "op": "is", "value": "Returned"})
    assert out["column"] == "Task2Status"


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
