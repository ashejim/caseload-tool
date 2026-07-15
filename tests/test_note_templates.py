"""Tests for the note-template core (src/scenarios.py).

Covers the pure, headless pieces the fire-time UI builds on:
  - parse_note_template_text: pasted `Label: default` block -> fields (the
    fast authoring path), including the cloud-team session-note example.
  - render_note_template: filled fields -> note body, with the multiline
    label-on-its-own-line rule and the empty-value "keep the skeleton" rule.
  - _note_template_from_dict / note_template_to_dict / load_note_templates:
    round-trip through the scenarios.yaml `note_templates:` block, dropping
    junk (no name, no valid fields, bad kind).

Run: python tests/test_note_templates.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scenarios import (  # noqa: E402
    NoteTemplate, NoteTemplateField, load_note_templates,
    note_template_to_dict, parse_note_template_text, render_note_template,
    _note_template_from_dict,
)

CLOUD_EXAMPLE = """\
Purpose:
Attempt: N/A
Prev. Assessment attempt score(s): N/A
Student Action Items:
Next Call: N/A
Summary:"""


# --- parse_note_template_text ---------------------------------------------

def test_parse_splits_label_and_default():
    fields = parse_note_template_text(CLOUD_EXAMPLE)
    labels = [f.label for f in fields]
    assert labels == [
        "Purpose", "Attempt", "Prev. Assessment attempt score(s)",
        "Student Action Items", "Next Call", "Summary",
    ]
    # Trailing text after the first colon is the default; blanks stay empty.
    by_label = {f.label: f.default for f in fields}
    assert by_label["Attempt"] == "N/A"
    assert by_label["Next Call"] == "N/A"
    assert by_label["Purpose"] == ""
    assert by_label["Summary"] == ""


def test_parse_splits_on_first_colon_only():
    # A colon inside the default must not create a second split.
    (f,) = parse_note_template_text("Prev score(s): 72, ratio 1:2")
    assert f.label == "Prev score(s)"
    assert f.default == "72, ratio 1:2"


def test_parse_skips_blank_and_labelless_lines():
    fields = parse_note_template_text("\n\nPurpose:\n\n:orphan\nNoColon\n")
    labels = [f.label for f in fields]
    assert labels == ["Purpose", "NoColon"]      # ":orphan" (empty label) dropped
    assert fields[0].kind == "text"              # default kind


# --- render_note_template --------------------------------------------------

def test_render_inline_and_multiline():
    tmpl = NoteTemplate(name="T", fields=[
        NoteTemplateField(label="Attempt", default="N/A"),
        NoteTemplateField(label="Summary", kind="multiline"),
    ])
    body = render_note_template(tmpl, {
        "Attempt": "2",
        "Summary": "Talked through OA.\nNext steps set.",
    })
    assert body == (
        "Attempt: 2\n"
        "Summary:\n"
        "Talked through OA.\nNext steps set."
    )


def test_render_missing_value_uses_default_and_keeps_skeleton():
    tmpl = NoteTemplate(name="T", fields=[
        NoteTemplateField(label="Attempt", default="N/A"),
        NoteTemplateField(label="Purpose"),              # empty default
        NoteTemplateField(label="Summary", kind="multiline"),  # empty multiline
    ])
    # No values supplied at all -> defaults; empty fields keep the bare label.
    body = render_note_template(tmpl, {})
    assert body == "Attempt: N/A\nPurpose:\nSummary:"


# --- dict round-trip + loader ---------------------------------------------

def test_to_dict_omits_empty_keys():
    t = NoteTemplate(name="Cloud Session Note", fields=[
        NoteTemplateField(label="Attempt", default="N/A", kind="dropdown",
                          choices=["N/A", "1", "2", "3"]),
        NoteTemplateField(label="Summary", kind="multiline"),
    ], courses=["C769"], interaction_type="Session Note")
    d = note_template_to_dict(t)
    assert d["name"] == "Cloud Session Note"
    assert d["courses"] == ["C769"]
    assert d["interaction_type"] == "Session Note"
    assert "note_type" not in d                    # empty omitted
    # The multiline field carries no default/choices, so those keys are omitted.
    summary = d["fields"][1]
    assert summary == {"label": "Summary", "kind": "multiline"}
    # A plain text field with no extras is just its label.
    assert note_template_to_dict(
        NoteTemplate(name="X", fields=[NoteTemplateField(label="Note")])
    )["fields"][0] == {"label": "Note"}


def test_from_dict_round_trip_and_bad_kind():
    t = NoteTemplate(name="T", fields=[
        NoteTemplateField(label="A", default="N/A", kind="dropdown",
                          choices=["N/A", "1"]),
        NoteTemplateField(label="B", kind="bogus"),   # invalid -> text
    ])
    back = _note_template_from_dict(note_template_to_dict(t))
    assert back.name == "T"
    assert back.fields[0].kind == "dropdown"
    assert back.fields[0].choices == ["N/A", "1"]
    assert back.fields[1].kind == "text"              # coerced


def test_from_dict_drops_junk():
    assert _note_template_from_dict({"fields": []}) is None        # no name
    assert _note_template_from_dict({"name": "  "}) is None        # blank name
    assert _note_template_from_dict("not a dict") is None
    # A template with a name but only junk fields survives with empty fields.
    t = _note_template_from_dict({"name": "T", "fields": [{"kind": "text"}]})
    assert t is not None and t.fields == []           # field w/o label dropped


def test_load_note_templates_from_yaml():
    import yaml
    doc = {
        "scenarios": {},
        "note_templates": [
            {"name": "Cloud Session Note", "courses": ["C769"],
             "fields": [
                 {"label": "Attempt", "default": "N/A", "kind": "dropdown",
                  "choices": ["N/A", "1", "2"]},
                 {"label": "Summary", "kind": "multiline"},
             ]},
            {"fields": [{"label": "x"}]},   # no name -> dropped
        ],
    }
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
        from pathlib import Path
        tmpls = load_note_templates(Path(path))
    finally:
        os.unlink(path)
    assert len(tmpls) == 1
    assert tmpls[0].name == "Cloud Session Note"
    assert tmpls[0].courses == ["C769"]
    assert [f.label for f in tmpls[0].fields] == ["Attempt", "Summary"]


def test_load_note_templates_missing_block():
    import yaml
    from pathlib import Path
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump({"scenarios": {}}, fh)
        assert load_note_templates(Path(path)) == []
    finally:
        os.unlink(path)


# --- markdown bold (note_form) --------------------------------------------

def test_strip_md_bold_removes_markers():
    from src.note_form import strip_md_bold
    assert strip_md_bold("**Purpose**: talked. **Next**: retest") == \
        "Purpose: talked. Next: retest"
    assert strip_md_bold("no bold here") == "no bold here"
    assert strip_md_bold("") == ""
    # A lone / unmatched marker is left as-is (needs a closing pair).
    assert strip_md_bold("2 ** 3 = math") == "2 ** 3 = math"


def test_md_bold_to_html_wraps_after_escaping():
    import html
    from src.note_form import md_bold_to_html
    # Applied to already-escaped text: inner text stays escaped, tags added.
    assert md_bold_to_html(html.escape("**Purpose**")) == "<b>Purpose</b>"
    assert md_bold_to_html(html.escape("a <b> & **x**")) == \
        "a &lt;b&gt; &amp; <b>x</b>"
    assert md_bold_to_html(html.escape("plain")) == "plain"


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
