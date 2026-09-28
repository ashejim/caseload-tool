"""Tests for src/bot_drafts.py — parsing/stripping bot reply drafts."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import bot_drafts as bd  # noqa: E402


def _answer_html(answer="The answer.", banner="Automated reply.",
                 snapshot="Student snapshot: ...", footer="Reply to follow up."):
    """A draft body shaped like phase7_email_agent.build_answer_html:
    fenced internal block, fenced banner, fenced body (answer + footer),
    then a quoted original the client must NOT touch."""
    return (
        f"<body>{bd.INTERNAL_MARK_START}"
        f'<div style="border:2px solid #c00000">{snapshot}</div>'
        f"{bd.INTERNAL_MARK_END}\n"
        f"{bd.BANNER_MARK_START}<p><i>{banner}</i></p>{bd.BANNER_MARK_END}\n"
        f"{bd.BODY_MARK_START}<p>{answer}</p>\n<hr><p><i>{footer}</i></p>"
        f"{bd.BODY_MARK_END}"
        "<blockquote>quoted original student email</blockquote></body>"
    )


def test_strip_internal_removes_only_the_internal_block():
    html = _answer_html()
    out = bd.strip_internal(html)
    assert "Student snapshot" not in out
    assert bd.INTERNAL_MARK_START not in out and bd.INTERNAL_MARK_END not in out
    # everything else survives
    assert "The answer." in out
    assert "Automated reply." in out
    assert "quoted original" in out


def test_student_html_as_bot_keeps_banner_drops_internal():
    out = bd.student_html(_answer_html(), as_bot=True)
    assert "Student snapshot" not in out          # internal gone
    assert "Automated reply." in out              # banner stays (labeled)
    assert "The answer." in out
    assert "quoted original" in out


def test_student_html_as_me_drops_banner_and_internal():
    out = bd.student_html(_answer_html(), as_bot=False)
    assert "Student snapshot" not in out          # internal gone
    assert "Automated reply." not in out          # banner gone (own voice)
    assert bd.BANNER_MARK_START not in out
    assert "The answer." in out                   # answer + quote survive
    assert "quoted original" in out


def test_editable_body_and_banner_extraction():
    html = _answer_html(answer="Do X then Y.", banner="Auto reply label.")
    body = bd.editable_body(html)
    assert "Do X then Y." in body
    assert "Reply to follow up." in body      # footer is inside the body
    assert bd.BODY_MARK_START not in body      # fences excluded
    assert "quoted original" not in body       # quote is OUTSIDE the body
    assert "Student snapshot" not in body       # internal is outside too
    assert "Auto reply label." in bd.banner_html(html)
    # absent -> ''
    assert bd.editable_body("<body>none</body>") == ""
    assert bd.banner_html("<body>none</body>") == ""


def test_rebuild_for_send_edited_as_me():
    html = _answer_html()
    out = bd.rebuild_for_send(html, "<p>My edited answer.</p>", as_bot=False)
    assert "My edited answer." in out          # edit spliced in
    assert "The answer." not in out            # original body replaced
    assert "Automated reply." not in out       # banner dropped (own voice)
    assert "quoted original" in out            # quote preserved
    assert "Student snapshot" not in out       # internal gone
    # no leftover comment fences
    for mark in bd._ALL_MARKS:
        assert mark not in out


def test_rebuild_for_send_edited_as_bot_keeps_banner():
    out = bd.rebuild_for_send(_answer_html(), "<p>Tweaked.</p>", as_bot=True)
    assert "Tweaked." in out
    assert "Automated reply." in out           # banner kept (labeled)
    assert "quoted original" in out
    for mark in bd._ALL_MARKS:
        assert mark not in out


def test_rebuild_for_send_unedited_matches_student_html():
    html = _answer_html()
    assert bd.rebuild_for_send(html, None, True) == bd.student_html(html, True)
    assert bd.rebuild_for_send(html, None, False) == bd.student_html(html, False)


def test_internal_block_extraction():
    inner = bd.internal_block(_answer_html(snapshot="Student snapshot: Jane"))
    assert "Student snapshot: Jane" in inner
    # fences themselves are not included
    assert bd.INTERNAL_MARK_START not in inner
    assert bd.INTERNAL_MARK_END not in inner
    # absent -> empty
    assert bd.internal_block("<body>no markers</body>") == ""


def test_ack_draft_has_no_banner():
    # ack bodies carry the internal block but no banner fence
    ack = (f"<body>{bd.INTERNAL_MARK_START}<div>snap</div>"
           f"{bd.INTERNAL_MARK_END}\n<p>We received your email.</p></body>")
    # as_me and as_bot are identical when there's no banner to strip
    assert bd.student_html(ack, as_bot=True) == bd.student_html(ack, as_bot=False)
    assert "snap" not in bd.student_html(ack, as_bot=True)
    assert "We received your email." in bd.student_html(ack, as_bot=True)


def test_from_parts_parses_props():
    d = bd.BotDraft.from_parts(
        entry_id="E1", store_id="S1", to="Jim Ashe",
        subject="RE: C769: Task 2",
        props={"BotCourse": "C769", "BotMessageKey": "<abc@x>",
               "BotAction": "answer_draft", "BotBestSim": "0.767",
               "BotQuestion": "How do I move Task 2 to Task 3?",
               "BotVersion": "1"},
        html=_answer_html())
    assert d.course == "C769"
    assert d.action == "answer_draft"
    assert d.is_answer
    assert d.best_sim == 0.767
    assert not d.low_confidence      # 0.767 >= 0.55 threshold
    assert d.topic is None
    assert d.question == "How do I move Task 2 to Task 3?"
    # absent BotQuestion -> empty string, never None
    assert bd.BotDraft.from_parts("E", "S", "t", "s", {}, "").question == ""
    assert d.version == "1"


def test_low_confidence_boundary():
    hi = bd.BotDraft.from_parts("E", "S", "to", "s",
                                {"BotBestSim": "0.60"}, "")
    lo = bd.BotDraft.from_parts("E", "S", "to", "s",
                                {"BotBestSim": "0.50"}, "")
    none = bd.BotDraft.from_parts("E", "S", "to", "s", {}, "")
    assert hi.low_confidence is False
    assert lo.low_confidence is True
    assert none.best_sim is None and none.low_confidence is False


def test_best_sim_bad_value_is_none():
    d = bd.BotDraft.from_parts("E", "S", "to", "s",
                               {"BotBestSim": "n/a"}, "")
    assert d.best_sim is None


def test_is_bot_draft_detection():
    assert bd.is_bot_draft({"BotVersion": "1"}, "")
    assert bd.is_bot_draft({"BotCourse": "C769"}, "")
    # no props but fenced body still counts
    assert bd.is_bot_draft({}, _answer_html())
    # a reviewer's own mail: no props, no fences
    assert not bd.is_bot_draft({}, "<body>my own draft</body>")


def test_ack_topic_preserved():
    d = bd.BotDraft.from_parts("E", "S", "to", "s",
                               {"BotAction": "ack_draft",
                                "BotTopic": "extension_term"}, "")
    assert d.action == "ack_draft"
    assert not d.is_answer
    assert d.topic == "extension_term"


def test_student_email_parsed():
    d = bd.BotDraft.from_parts("E", "S", "to", "s",
                               {"BotStudentEmail": "stu@wgu.edu"}, "")
    assert d.student_email == "stu@wgu.edu"
    assert bd.BotDraft.from_parts("E", "S", "to", "s", {}, "").student_email \
        == ""


def test_caseload_pm_cc_resolution():
    rows = [
        {"StudentEmail": "a@wgu.edu", "MentorEmail": "pm.a@wgu.edu"},
        {"Email": "b@wgu.edu", "MentorName": "Doe, Jane"},   # name fallback
        {"StudentEmail": "c@wgu.edu"},                        # no PM at all
    ]
    assert bd.find_caseload_row(rows, "A@WGU.EDU") is rows[0]   # case-insens.
    assert bd.default_cc_for(rows, "a@wgu.edu") == "pm.a@wgu.edu"
    # "Last, First" -> "First Last" so the comma isn't read as a separator
    assert bd.default_cc_for(rows, "b@wgu.edu") == "Jane Doe"
    assert bd.default_cc_for(rows, "c@wgu.edu") == ""
    assert bd.default_cc_for(rows, "missing@wgu.edu") == ""
    assert bd.find_caseload_row([], "a@wgu.edu") is None
    assert bd.find_caseload_row(rows, "") is None


def test_gating_and_settings_helpers():
    from types import SimpleNamespace
    off = SimpleNamespace(bot_drafts_enabled=False,
                          bot_drafts_folder="", bot_drafts_mailbox="")
    on = SimpleNamespace(bot_drafts_enabled=True,
                         bot_drafts_folder="  Team Drafts ",
                         bot_drafts_mailbox="ugcapstoneit@wgu.edu")
    assert bd.feature_enabled(off) is False
    assert bd.feature_enabled(on) is True
    # folder: empty -> default; set -> trimmed
    assert bd.watch_folder_name(off) == bd.DEFAULT_WATCH_FOLDER
    assert bd.watch_folder_name(on) == "Team Drafts"
    # mailbox: empty -> '' (personal); set -> trimmed
    assert bd.watch_mailbox(off) == ""
    assert bd.watch_mailbox(on) == "ugcapstoneit@wgu.edu"
    # missing attributes entirely -> safe defaults (getattr fallbacks)
    bare = SimpleNamespace()
    assert bd.feature_enabled(bare) is False
    assert bd.watch_folder_name(bare) == bd.DEFAULT_WATCH_FOLDER
    assert bd.watch_mailbox(bare) == ""


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
