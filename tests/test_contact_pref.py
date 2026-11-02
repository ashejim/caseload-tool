"""Tests for history.contact_preference() + set_contact_preference().

Auto-inferred from the note history (channel the student engages on); a manual
instructor override wins. Run: python tests/test_contact_pref.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import history  # noqa: E402


def _tmp_db():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(p)
    return p


def _note(ntype, text, date):
    return {"url": "", "type": ntype, "text": text, "course": "C769",
            "date": date, "author": "x", "subject": "s"}


def test_no_history_is_none():
    db = _tmp_db()
    try:
        assert history.contact_preference("111", db_path=db) == {
            "pref": "", "source": "none"}
    finally:
        os.unlink(db)


def test_auto_from_most_engaged_channel():
    db = _tmp_db()
    try:
        # replies by text twice, email once -> text is the engaged channel
        history.persist_notes([
            _note("Instant Message (IM) / Text", "Incoming: hi", "2026-07-01T09:00:00Z"),
            _note("Instant Message (IM) / Text", "Incoming: yo", "2026-07-02T09:00:00Z"),
            _note("Email from Student", "thanks", "2026-07-03T09:00:00Z"),
        ], student_id="111", db_path=db)
        assert history.contact_preference("111", db_path=db) == {
            "pref": "text", "source": "auto"}
    finally:
        os.unlink(db)


def test_manual_override_wins():
    db = _tmp_db()
    try:
        history.persist_notes(
            [_note("Instant Message (IM) / Text", "Incoming: hi", "2026-07-01T09:00:00Z")],
            student_id="111", db_path=db)
        history.set_contact_preference("111", "email", db_path=db)
        assert history.contact_preference("111", db_path=db) == {
            "pref": "email", "source": "manual"}
        # clearing reverts to auto
        history.set_contact_preference("111", "", db_path=db)
        assert history.contact_preference("111", db_path=db)["source"] == "auto"
    finally:
        os.unlink(db)


def test_invalid_pref_rejected():
    db = _tmp_db()
    try:
        try:
            history.set_contact_preference("111", "carrier pigeon", db_path=db)
            assert False, "should have raised"
        except ValueError:
            pass
    finally:
        if os.path.exists(db):   # invalid pref raises before the db is created
            os.unlink(db)



def test_bulk_matches_per_student():
    db = _tmp_db()
    try:
        history.persist_notes([   # replies by text twice, email once → text
            _note("Instant Message (IM) / Text", "Incoming: hi", "2026-10-01T10:00:00.000Z"),
            _note("Instant Message (IM) / Text", "Incoming: ok", "2026-10-02T10:00:00.000Z"),
            _note("Email from Student", "q", "2026-10-03T10:00:00.000Z"),
        ], student_id="000111", db_path=db)
        history.persist_notes([   # only calls (both directions count) → call
            _note("Live Call", "talked", "2026-10-04T10:00:00.000Z"),
        ], student_id="000222", db_path=db)
        history.persist_notes([   # tie text vs email → cheaper (text)
            _note("Instant Message (IM) / Text", "Incoming: a", "2026-10-05T10:00:00.000Z"),
            _note("Email from Student", "b", "2026-10-06T10:00:00.000Z"),
        ], student_id="000333", db_path=db)
        history.persist_notes([   # outbound only → no preference
            _note("Email to Student", "hello", "2026-10-07T10:00:00.000Z"),
        ], student_id="000444", db_path=db)
        history.set_contact_preference("000555", "email", db_path=db)  # manual
        bulk = history.all_contact_preferences(db_path=db)
        for sid in ("000111", "000222", "000333", "000444", "000555"):
            one = history.contact_preference(sid, db_path=db)
            got = bulk.get(sid.lstrip("0"))
            assert (got or ("", "none")) == (one["pref"], one["source"]), (sid, got, one)
        assert bulk["111"] == ("text", "auto") and bulk["555"] == ("email", "manual")
    finally:
        os.unlink(db)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
