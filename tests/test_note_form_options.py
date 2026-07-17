"""Tests for the note-form option helpers in src.note_form:
interaction-type lists per format, and the activities-disabled rule.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import note_form  # noqa: E402


def test_types_for_multiple_returns_multi_list():
    assert note_form.types_for_format("Multiple Interactions") is \
        note_form.INTERACTION_TYPES_MULTI


def test_types_for_single_returns_single_list():
    assert note_form.types_for_format("Single Interaction") is \
        note_form.INTERACTION_TYPES_SINGLE


def test_types_for_unknown_defaults_to_single():
    assert note_form.types_for_format("") is note_form.INTERACTION_TYPES_SINGLE


def test_activities_disabled_for_outbound_single():
    assert note_form.activities_disabled_for(
        "Single Interaction", "Admin Note")
    assert note_form.activities_disabled_for(
        "Single Interaction", "Email to Student")


def test_activities_enabled_for_engagement_single():
    assert not note_form.activities_disabled_for(
        "Single Interaction", "Live Call")


def test_activities_never_disabled_for_multiple():
    # Every Multiple-Interaction type keeps activities enabled.
    for typ in note_form.ACTIVITY_DISABLE_TYPES_SINGLE:
        assert not note_form.activities_disabled_for(
            "Multiple Interactions", typ)


def test_format_list_matches_the_two_supported_formats():
    assert note_form.INTERACTION_FORMATS == [
        "Single Interaction", "Multiple Interactions"]


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
