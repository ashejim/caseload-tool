"""Tests for src.hotkeys — hotkey-spec string conversions."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import hotkeys  # noqa: E402


# --- to_pynput_hotkey_string -----------------------------------------------

def test_pynput_modifiers_and_letter():
    assert hotkeys.to_pynput_hotkey_string("Ctrl+Shift+1") == \
        "<ctrl>+<shift>+1"


def test_pynput_function_key():
    assert hotkeys.to_pynput_hotkey_string("F1") == "<f1>"


def test_pynput_control_alias_and_super():
    assert hotkeys.to_pynput_hotkey_string("Control+Win+a") == \
        "<ctrl>+<cmd>+a"


def test_pynput_named_key_wrapped():
    assert hotkeys.to_pynput_hotkey_string("Ctrl+space") == "<ctrl>+<space>"


def test_pynput_empty_raises():
    try:
        hotkeys.to_pynput_hotkey_string("  ")
    except ValueError:
        return
    raise AssertionError("expected ValueError on empty spec")


# --- standalone_fkey_vk ----------------------------------------------------

def test_standalone_fkey_vk_f1():
    assert hotkeys.standalone_fkey_vk("F1") == 0x70


def test_standalone_fkey_vk_f24():
    assert hotkeys.standalone_fkey_vk("F24") == 0x70 + 23


def test_standalone_fkey_vk_rejects_modified():
    assert hotkeys.standalone_fkey_vk("Ctrl+F1") is None


def test_standalone_fkey_vk_rejects_non_fkey():
    assert hotkeys.standalone_fkey_vk("A") is None
    assert hotkeys.standalone_fkey_vk("F25") is None


# --- keysym_to_hotkey_part -------------------------------------------------

def test_keysym_function_key_kept():
    assert hotkeys.keysym_to_hotkey_part("F5") == "F5"


def test_keysym_single_char_uppercased():
    assert hotkeys.keysym_to_hotkey_part("a") == "A"


def test_keysym_named_passthrough():
    assert hotkeys.keysym_to_hotkey_part("space") == "space"


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
