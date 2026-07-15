"""Tests for src/names.py — name capitalization + loose matching."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import names  # noqa: E402


def test_capitalize_standard():
    # ALL-CAPS -> Title, lowercase -> Title, mixed case preserved
    assert names.capitalize_name("JANE", mode="standard") == "Jane"
    assert names.capitalize_name("john", mode="standard") == "John"
    assert names.capitalize_name("McDonald", mode="standard") == "McDonald"
    assert names.capitalize_name("O'Brien", mode="standard") == "O'Brien"
    assert names.capitalize_name("mary-jane", mode="standard") == "Mary-Jane"
    assert names.capitalize_name("", mode="standard") == ""


def test_capitalize_modes():
    # 'off' returns as-is; 'lower' only fixes lowercase, leaves ALL-CAPS alone
    assert names.capitalize_name("jANE", mode="off") == "jANE"
    assert names.capitalize_name("john smith", mode="lower") == "John Smith"
    assert names.capitalize_name("JANE", mode="lower") == "JANE"   # caps left alone


def test_set_cap_mode_affects_default():
    names.set_cap_mode("off")
    assert names.capitalize_name("JANE") == "JANE"     # default now 'off'
    names.set_cap_mode("standard")
    assert names.capitalize_name("JANE") == "Jane"
    names.set_cap_mode("")                              # blank -> 'standard'
    assert names.capitalize_name("bob") == "Bob"


def test_names_loosely_match():
    assert names.names_loosely_match("Jim Ashe", "Ashe, Jim") is True
    assert names.names_loosely_match("Dr. Jim Ashe", "Jim Ashe") is True
    assert names.names_loosely_match("Jim Albert Ashe", "Jim Ashe") is True
    assert names.names_loosely_match("Jim Smith", "Bob Smith") is False  # 1 overlap
    assert names.names_loosely_match("", "Jim Ashe") is False
    assert names.names_loosely_match("Dr.", "Jim Ashe") is False   # only a title


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
