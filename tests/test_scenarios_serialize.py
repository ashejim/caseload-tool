"""Round-trip tests for the config-object → dict serializers in
src.scenarios (_email_to_dict / _text_to_dict / _note_to_dict /
_branch_to_dict), the inverse of the _*_from_dict family.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scenarios import (  # noqa: E402
    EmailConfig, TextConfig, BranchConfig,
    _email_from_dict, _text_from_dict, _note_from_dict, _branches_from_list,
    _email_to_dict, _text_to_dict, _note_to_dict, _branch_to_dict,
)


def test_email_to_dict_none_passthrough():
    assert _email_to_dict(None) is None


def test_email_roundtrip():
    src = {
        "subject": "Hi", "body_html_file": "b.html", "to": "s@x.com",
        "signature_file": "sig", "inline_images": ["a.png"],
        "cc_pm": True, "pick_template": False,
        "font_family": "Calibri", "font_size": 11,
    }
    cfg = _email_from_dict(src)
    out = _email_to_dict(cfg)
    for k, v in src.items():
        assert out[k] == v, k


def test_text_to_dict_none_passthrough():
    assert _text_to_dict(None) is None


def test_text_roundtrip():
    cfg = _text_from_dict({
        "body": "yo", "body_file": "", "window_start_hour": 9,
        "window_end_hour": 17, "inbox_label": "C769", "commit": True,
    })
    out = _text_to_dict(cfg)
    assert out["body"] == "yo"
    assert out["window_start_hour"] == 9
    assert out["window_end_hour"] == 17
    assert out["inbox_label"] == "C769"
    assert out["commit"] is True
    assert out["schedule"] is True


def test_note_roundtrip_and_optional_keys():
    src = {
        "interaction_format": "Single Interaction",
        "interaction_type": "Admin Note",
        "body": "note body",
        "academic_activities": ["Set Academic Goals"],
        "submit": False, "append_clipboard": True,
        "enter_additional_text": True,
        "course_code_override": "C770", "subject": "Subj",
    }
    out = _note_to_dict(_note_from_dict(src))
    for k, v in src.items():
        assert out[k] == v, k


def test_note_to_dict_omits_blank_optionals():
    out = _note_to_dict(_note_from_dict({
        "interaction_format": "Single Interaction",
        "interaction_type": "Live Call", "body": "x",
    }))
    assert "course_code_override" not in out
    assert "subject" not in out


def test_branch_roundtrip_nested():
    branch = _branches_from_list([{
        "title": "Passed", "conditions": [{"column": "Task1"}],
        "email": {"subject": "Well done", "to": "s@x.com"},
        "text": {"body": "gg"},
        "notes": [{"interaction_type": "Admin Note", "body": "n"}],
        "color": "#abcdef",
    }])[0]
    out = _branch_to_dict(branch)
    assert out["title"] == "Passed"
    assert out["conditions"] == [{"column": "Task1"}]
    assert out["email"]["subject"] == "Well done"
    assert out["text"]["body"] == "gg"
    assert out["notes"][0]["interaction_type"] == "Admin Note"
    assert out["color"] == "#abcdef"


def test_branch_to_dict_handles_empty_email_text():
    branch = BranchConfig(title="Bare", conditions=[], email=None,
                          text=None, notes=[], color="")
    out = _branch_to_dict(branch)
    assert out["email"] is None
    assert out["text"] is None
    assert out["notes"] == []
    assert out["title"] == "Bare"


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
