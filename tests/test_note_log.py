"""Tests for src.note_log.resolve_student_id — recovering the StudentID for a
filed-note record whose worker-side id came back empty (the deep-link fire path
opens a record by Contact id, so there's no on-page Caseload table to scrape).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.note_log import resolve_student_id  # noqa: E402


def _rows(*triples):
    return [{"Name": n, "StudentID": s, "CourseCode": c} for n, s, c in triples]


def test_single_exact_match():
    rows = _rows(("Patrick Kolanda", "009676653", "C769"),
                 ("Aaron Schneider", "010101010", "C769"))
    assert resolve_student_id(rows, "Patrick Kolanda", "C769") == "009676653"


def test_same_name_two_courses_resolved_by_course():
    # Same student on two courses -> the course disambiguates.
    rows = _rows(("Jane Doe", "001", "C769"), ("Jane Doe", "001", "D502"))
    assert resolve_student_id(rows, "Jane Doe", "D502") == "001"
    # Even without a course, one distinct id -> still resolvable.
    assert resolve_student_id(rows, "Jane Doe", "") == "001"


def test_ambiguous_two_different_ids_returns_empty():
    # Two DIFFERENT students share a name and course -> never guess.
    rows = _rows(("John Smith", "001", "C769"), ("John Smith", "002", "C769"))
    assert resolve_student_id(rows, "John Smith", "C769") == ""


def test_ambiguous_across_courses_narrowed_by_course():
    rows = _rows(("John Smith", "001", "C769"), ("John Smith", "002", "D502"))
    assert resolve_student_id(rows, "John Smith", "D502") == "002"
    # No course hint + two distinct ids -> empty (won't guess).
    assert resolve_student_id(rows, "John Smith", "") == ""


def test_no_match_and_empty_inputs():
    rows = _rows(("Patrick Kolanda", "009676653", "C769"))
    assert resolve_student_id(rows, "Nobody Here", "C769") == ""
    assert resolve_student_id(rows, "", "C769") == ""
    assert resolve_student_id([], "Patrick Kolanda", "C769") == ""


def test_row_without_id_is_skipped():
    rows = _rows(("Patrick Kolanda", "", "C769"),
                 ("Patrick Kolanda", "009676653", "C769"))
    assert resolve_student_id(rows, "Patrick Kolanda", "C769") == "009676653"


def test_injected_matcher_and_normalizer():
    # A loose matcher (case/spacing) + a course normalizer are honored.
    rows = _rows(("KOLANDA, PATRICK", "009676653", " c769 "))
    loose = lambda a, b: sorted(a.replace(",", " ").lower().split()) \
        == sorted(b.replace(",", " ").lower().split())
    assert resolve_student_id(
        rows, "Patrick Kolanda", "C769",
        norm_course=lambda c: c.strip().upper(), names_match=loose) == "009676653"


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
