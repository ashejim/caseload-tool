"""Tests for src/dates.py — the consolidated date/timezone helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dates  # noqa: E402


def test_effective_tz_known_and_fallback():
    assert dates.effective_tz("EST") == "EST"
    assert dates.effective_tz("  CST ") == "CST"
    assert dates.effective_tz("") == "MST"          # blank -> Mountain default
    assert dates.effective_tz("ZZZ") == "MST"       # unknown -> Mountain default


def test_to_iso_date_formats():
    assert dates.to_iso_date("2026-7-5") == "2026-07-05"     # zero-pads
    assert dates.to_iso_date("07/05/2026") == "2026-07-05"   # MM/DD/YYYY
    assert dates.to_iso_date("2026-07-05") == "2026-07-05"
    assert dates.to_iso_date("") == ""
    assert dates.to_iso_date("not a date") == "not a date"   # unchanged


def test_days_until_and_since_are_inverse():
    from datetime import date, timedelta
    future = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")
    past = (date.today() - timedelta(days=3)).strftime("%Y-%m-%d")
    assert dates.days_until(future) == 5
    assert dates.days_until(past) == -3
    assert dates.days_since(past) == 3
    assert dates.days_since(future) == -5
    # inverse relationship
    assert dates.days_since(future) == -dates.days_until(future)


def test_days_helpers_handle_junk():
    assert dates.days_until("") is None
    assert dates.days_until("garbage") is None
    assert dates.days_since(None) is None
    # accepts a full timestamp (reads leading YYYY-MM-DD)
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    assert dates.days_until(today + "T14:00:00") == 0


def test_student_local_time_unknown_tz_is_blank():
    assert dates.student_local_time("") == ""
    assert dates.student_local_time("ZZZ") == ""
    # a known tz returns a non-empty "H:MM AM/PM" string
    out = dates.student_local_time("EST")
    assert out and ("AM" in out or "PM" in out)


def test_text_message_reexports_stay_in_sync():
    # text_message re-imports these from dates; the objects must be identical.
    from src import text_message as tm
    assert tm.TZ_ABBR_TO_IANA is dates.TZ_ABBR_TO_IANA
    assert tm.effective_tz is dates.effective_tz
    assert tm.DEFAULT_TZ_ABBR == dates.DEFAULT_TZ_ABBR


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
