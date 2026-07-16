"""Tests for the non-COM parts of src.outlook_email — the Outlook Classic
capability probe and the canonical requirement message. The actual send path
needs a live Outlook and isn't unit-tested.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import outlook_email  # noqa: E402


def test_classic_available_returns_tristate_without_crashing():
    # True (registered) / False (not) / None (can't tell). Must never raise,
    # never launch Outlook.
    result = outlook_email.classic_available()
    assert result in (True, False, None)


def test_required_message_names_classic_and_the_fix():
    msg = outlook_email.OUTLOOK_CLASSIC_REQUIRED_MSG
    assert "Outlook Classic" in msg
    # Names the unsupported variants so the message is actionable.
    assert "new Outlook" in msg
    assert "web" in msg


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
