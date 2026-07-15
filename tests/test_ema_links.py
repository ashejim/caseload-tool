"""Tests for src/ema_links.py — EMA Score Report URL parse/build."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import ema_links  # noqa: E402

SAMPLE = ("https://tasks.wgu.edu/student/009930908/course/33860018"
          "/task/4521/score-report")


def test_parse_extracts_ids():
    assert ema_links.parse_ema_url(SAMPLE) == {
        "student_id": "009930908", "course_id": "33860018", "task_id": "4521"}


def test_parse_rejects_non_matches():
    assert ema_links.parse_ema_url("") is None
    assert ema_links.parse_ema_url(None) is None
    assert ema_links.parse_ema_url("https://example.com/foo") is None
    # Right host but the brief sign-in URL (no /score-report) must not match.
    assert ema_links.parse_ema_url(
        "https://tasks.wgu.edu/cb?code=abc") is None


def test_build_and_roundtrip():
    url = ema_links.build_ema_url("009930908", "33860018", "4521")
    assert url == SAMPLE
    assert ema_links.parse_ema_url(url) == {
        "student_id": "009930908", "course_id": "33860018", "task_id": "4521"}


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
