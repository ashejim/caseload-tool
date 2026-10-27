"""Tests for src/learner360.py (parsing, contract check, change-watch diff) and
history.persist_momi. Synthetic responses mirror the field shapes captured live
from Learner360 on 2026-10-14 (no real student data).

Run: python tests/test_learner360.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import history, learner360 as l3  # noqa: E402
from src.config import HISTORY_DB  # noqa: E402

# Tests must never touch the real history DB (see tests_never_touch_real_db).
_REAL_DB = (os.path.exists(HISTORY_DB),
            os.path.getmtime(HISTORY_DB) if os.path.exists(HISTORY_DB) else None)

CASELOAD = {
    "content": [
        {"studentWguId": "uuid-a", "studentPidm": 111, "studentId": "000000001",
         "firstName": "A", "lastName": "Z", "momentum": "High",
         "courses": ["C769"]},
        {"studentWguId": "uuid-b", "studentPidm": 222, "studentId": "000000002",
         "momentum": "Low", "courses": ["D413", "C769"]},
        {"studentWguId": "", "studentId": "000000003"},   # no uuid -> skipped
    ],
    "page": {"number": 1, "size": 100, "totalElements": 3, "totalPages": 2},
    "filterOptions": {},
}

MOMI = {
    "currentMomentum": "Med High",
    "weekOverWeekDifference": -2.5,
    "fromTermStartDifference": 7.0,
    "weeklyMomiScores": [
        {"date": "2026-04-06T00:00:00", "momentum": "High", "momentumScore": 81.2,
         "weekOverWeekDifference": 0, "fromTermStartDifference": 0},
        {"date": "04/13/2026", "momentum": "Med High", "momentumScore": 74,
         "weekOverWeekDifference": -7.2, "fromTermStartDifference": -7.2},
    ],
    "factorBreakdowns": [
        {"category": "Assessment Progress", "impactScore": 0.42},
        {"category": "Engagement", "impactScore": -0.13},
    ],
}


def test_caseload_query_matches_ui():
    q = l3.caseload_query(2, 50)
    assert q["page"] == 2 and q["size"] == 50 and q["filters"] == []
    assert "momentum" in q["facetFields"]


def test_parse_caseload_page():
    rows, total = l3.parse_caseload_page(CASELOAD)
    assert total == 2
    assert [r["student_id"] for r in rows] == ["000000001", "000000002"]
    assert rows[0]["wgu_id"] == "uuid-a" and rows[0]["pidm"] == "111"
    assert rows[1]["courses"] == ["D413", "C769"]


def test_page_meta():
    m = l3.page_meta(CASELOAD)
    assert m == {"content": 3, "total_elements": 3, "total_pages": 2,
                 "number": 1, "no_id": 1}, m
    assert l3.page_meta({})["content"] == 0


def test_parse_momi_and_dates():
    m = l3.parse_momi(MOMI)
    assert m["current"] == "Med High"
    assert [w["week_date"] for w in m["weekly"]] == ["2026-04-06", "2026-04-13"]
    assert m["weekly"][1]["score"] == 74.0 and m["weekly"][1]["wow"] == -7.2
    assert m["factors"][1] == {"category": "Engagement", "impact": -0.13}
    assert l3.norm_date("04-15-2026") == "2026-04-15"   # what L360 sends
    assert l3.norm_date("") == "" and l3.norm_date("weird") == "weird"


def test_contract_clean_and_breaking():
    assert l3.check_contract("caseloads/search", CASELOAD) == []
    assert l3.check_contract("momentum-indicator", MOMI) == []
    broken = dict(MOMI, weeklyMomiScores=[{"date": "2026-04-06", "momentum": "High"}])
    msgs = l3.check_contract("momentum-indicator", broken)
    assert msgs and "momentumScore" in msgs[0], msgs
    gone = {k: v for k, v in MOMI.items() if k != "factorBreakdowns"}
    assert any("factorBreakdowns" in m for m in l3.check_contract("momentum-indicator", gone))
    # An empty factor list (student with no data) is NOT drift.
    assert l3.check_contract("momentum-indicator", dict(MOMI, factorBreakdowns=[])) == []
    assert l3.check_contract("momentum-indicator", "html error page")


def test_diff_watch():
    keys = {"momentum-indicator": l3.observed_keys("momentum-indicator", MOMI)}
    old = {"checked_at": "2026-10-14", "bundles": ["main-aaa.js"],
           "flags": {"use-new-caseload-endpoint": "false", "x": "1"}, "keys": keys}
    assert l3.diff_watch(None, old) == ["baseline recorded (first Learner360 check)"]
    assert l3.diff_watch(old, dict(old)) == []
    newkeys = {"momentum-indicator": dict(keys["momentum-indicator"],
                                          weeklyMomiScores=keys["momentum-indicator"]["weeklyMomiScores"] + ["percentile"])}
    new = {"bundles": ["main-bbb.js"],
           "flags": {"use-new-caseload-endpoint": "true", "ea-dashboard": "true"},
           "keys": newkeys}
    msgs = " | ".join(l3.diff_watch(old, new))
    assert "new version" in msgs
    assert "ea-dashboard" in msgs and "removed: x" in msgs
    assert "'use-new-caseload-endpoint' changed: false → true" in msgs
    assert "new field(s) percentile" in msgs


def test_looks_like_login():
    assert l3.looks_like_login("https://access.wgu.edu/pingfed/as/authorization.oauth2?x")
    assert not l3.looks_like_login("https://learner360.wgu.edu/caseload")


def test_momi_capture_due():
    now = datetime(2026, 10, 22, 9, 0)
    assert l3.momi_capture_due(None, 7, now)                    # never captured
    assert l3.momi_capture_due("2026-10-15T09:00:00", 7, now)   # exactly 7 days
    assert not l3.momi_capture_due("2026-10-16 09:00:00", 7, now)
    assert not l3.momi_capture_due(None, 0, now)                # off
    assert l3.momi_capture_due("garbage", 7, now)               # unreadable -> due


NOTES = [
    {"compositeKey": "n1", "createdDate": "2026-10-01T15:30:00Z", "type": "email_to_student",
     "courseCode": "C769", "authorName": "CI Person", "subject": "Hi",
     "text": "<p>Checking   in on Task 2</p>", "authorIsCI": True},
    {"compositeKey": "n2", "createdDate": "2026-10-02T09:00:00.000+0000",
     "type": "instant_message_text", "text": "Outgoing: hello there"},
    {"compositeKey": "n3", "createdDate": "", "type": "admin_note", "text": "no date"},
]


def test_iso_utc_and_parse_notes():
    assert l3.iso_utc("2026-10-01T15:30:00Z") == "2026-10-01T15:30:00.000Z"
    assert l3.iso_utc("2026-10-01T09:30:00-06:00") == "2026-10-01T15:30:00.000Z"
    assert l3.iso_utc("1759332600000") == "2025-10-01T15:30:00.000Z"
    assert l3.iso_utc("junk") == ""
    rows = l3.parse_notes({"content": NOTES})
    assert [r["l360_id"] for r in rows] == ["n1", "n2", "n3"]
    assert rows[0]["created_at"] == "2026-10-01T15:30:00.000Z"
    assert rows[0]["author_is_ci"] and rows[0]["course"] == "C769"
    assert rows[2]["created_at"] == ""
    assert rows[1]["type"] == "Instant Message (IM) / Text"
    assert rows[1]["created_at"] == "2026-10-02T09:00:00.000Z"
    assert l3.sf_type_label("email_from_student") == "Email from Student"
    assert l3.sf_type_label("Live Call and Email to Student") ==         "Live Call and Email to Student"
    assert history.classify_note_direction(
        l3.sf_type_label("email_from_student"), "") == "inbound"
    sh = l3.notes_shape([NOTES, NOTES[:1]])
    assert sh["picked"]["created<-createdDate"] == 3
    assert "email_to_student" in sh["types"]
    assert sh["id_uniqueness"]["compositeKey"].startswith("4/3 ")
    assert all("Checking" not in str(v) for v in sh.values())   # no content


def test_l360_notes_dedupe_against_sf_scrape():
    path = _tmpdb()
    try:
        # SF scrape already holds n1 (same second, SF-style body) and n2 two
        # hours off (clock skew between sources).
        history.persist_notes([
            {"type": "Email to Student", "course": "", "subject": "Hi",
             "text": "Checking in on Task 2", "date": "2026-10-01T15:30:00.000Z"},
            {"type": "Instant Message (IM) / Text", "text": "Outgoing: hello there",
             "date": "2026-10-02T11:00:00.000Z"},
        ], student_id="S1", db_path=path)
        notes = l3.parse_notes(NOTES)
        dry = history.persist_l360_notes({"S1": notes}, dry=True, db_path=path)
        assert (dry["exact"], dry["fuzzy"], dry["new"], dry["no_time"]) == (1, 1, 0, 1), dry
        assert dry["fuzzy_offset_hours"] == {2: 1}
        assert history.notes_count(db_path=path) == 2          # dry wrote nothing
        assert history.l360_notes_last_import(db_path=path) is None
        # A genuinely new note is inserted once; a re-run sees it as 'already'.
        more = notes + l3.parse_notes([{"compositeKey": "n4", "createdDate":
                                        "2026-10-05T12:00:00Z",
                                        "type": "email_from_student",
                                        "text": "question"}])
        r1 = history.persist_l360_notes({"S1": more}, db_path=path)
        assert r1["new"] == 1 and r1["exact"] == 1, r1
        r2 = history.persist_l360_notes({"S1": more}, db_path=path)
        assert r2["new"] == 0 and r2["already"] == 1, r2
        assert history.notes_count(db_path=path) == 3
        # The SF sweep later scraping n4 must UPSERT the l360 row, not add one.
        history.persist_notes([{"type": "Email from Student", "text": "question",
                                "date": "2026-10-05T12:00:00.000Z"}],
                              student_id="S1", db_path=path)
        assert history.notes_count(db_path=path) == 3
        conn = sqlite3.connect(path)
        ch, di = conn.execute("SELECT channel, direction FROM notes WHERE "
                              "note_id = 'l360:n4'").fetchone()
        conn.close()
        assert (ch, di) == ("email", "inbound")
    finally:
        os.remove(path)


def test_l360_matching_is_one_to_one():
    path = _tmpdb()
    try:
        # One stored copy of a templated text; Learner360 has it 3 times (3
        # real sends a few hours apart) → 1 match + 2 new, not 3 matches.
        history.persist_notes([{"type": "Instant Message (IM) / Text",
                                "text": "Outgoing: reminder",
                                "date": "2026-10-01T10:00:00.000Z"}],
                              student_id="S1", db_path=path)
        sends = l3.parse_notes([
            {"compositeKey": f"k{i}", "createdDate": f"2026-10-01T1{i}:00:00Z",
             "type": "instant_message_text", "text": "Outgoing: reminder"}
            for i in range(3)] + [
            {"compositeKey": "k0", "createdDate": "2026-10-01T10:00:00Z",
             "type": "instant_message_text", "text": "Outgoing: reminder"},
            {"compositeKey": "k9", "createdDate": "2026-10-01T11:00:00Z",
             "type": "instant_message_text", "text": "Outgoing:  Reminder"}])
        r = history.persist_l360_notes({"S1": sends}, dry=True, db_path=path)
        assert (r["exact"], r["fuzzy"], r["new"], r["dup_in_360"]) == (1, 0, 2, 2), r
        assert history.stage_l360_notes({"S1": sends}, db_path=path) == 5
        assert history.stage_l360_notes({"S1": sends[:2]}, db_path=path) == 2
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM l360_notes_staging").fetchone()[0] == 2
        conn.close()
    finally:
        os.remove(path)


def test_l360_type_pass_and_reverse_near_match():
    path = _tmpdb()
    try:
        # SF has a mass email rendered differently, 9 h off → 'type' match.
        history.persist_notes([{"type": "Mass Email", "text": "Subject line only",
                                "date": "2026-10-01T21:00:00.000Z"}],
                              student_id="S1", db_path=path)
        notes = l3.parse_notes([
            {"compositeKey": "m1", "createdDate": "2026-10-01T12:00:00Z",
             "type": "mass_email", "text": "<p>Dear student, full body…</p>"},
            {"compositeKey": "t1", "createdDate": "2026-10-03T15:00:00Z",
             "type": "instant_message_text", "text": "Outgoing: new one"}])
        r = history.persist_l360_notes({"S1": notes}, db_path=path)
        assert (r["type"], r["new"]) == (1, 1), r
        assert history.notes_count(db_path=path) == 2
        # Later SF sweep scrapes t1 with a 7 h skew + different rendering →
        # must upsert the l360 row, not add a third.
        history.persist_notes([{"type": "Instant Message (IM) / Text",
                                "text": "Outgoing: new one (SF export)",
                                "date": "2026-10-03T22:00:00.000Z"}],
                              student_id="S1", db_path=path)
        assert history.notes_count(db_path=path) == 2
    finally:
        os.remove(path)


def _tmpdb():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_persist_momi_idempotent():
    path = _tmpdb()
    try:
        rows, _ = l3.parse_caseload_page(CASELOAD)
        students = [dict(rows[0], momi=l3.parse_momi(MOMI))]
        t1 = datetime(2026, 10, 14, 9, 0)
        r1 = history.persist_momi(students, db_path=path, now=t1)
        assert r1 == {"students": 1, "weeks_new": 2, "weeks_updated": 0,
                      "factor_rows": 2}, r1
        # Re-run: same weeks are updates, not duplicates; a new week is new.
        more = l3.parse_momi(dict(MOMI, weeklyMomiScores=MOMI["weeklyMomiScores"] + [
            {"date": "2026-04-20", "momentum": "Med", "momentumScore": 60}]))
        r2 = history.persist_momi([dict(rows[0], momi=more)], db_path=path,
                                  now=datetime(2026, 10, 21, 9, 0))
        assert r2["weeks_new"] == 1 and r2["weeks_updated"] == 2, r2
        hist = history.momi_weekly_for("000000001", db_path=path)
        assert [h["week_date"] for h in hist] == ["2026-04-06", "2026-04-13", "2026-04-20"]
        assert hist[0]["momentum_score"] == 81.2
        st = history.momi_capture_status(db_path=path)
        assert st["students"] == 1 and st["weeks"] == 3
        # Factors are kept per capture day.
        conn = sqlite3.connect(path)
        days = {r[0] for r in conn.execute("SELECT captured_date FROM momi_factors")}
        conn.close()
        assert days == {"2026-10-14", "2026-10-21"}
    finally:
        os.remove(path)


def test_schema_v4_db_upgrades_in_place():
    # An existing v4 DB (no Learner360 tables) gains them on next connect.
    path = _tmpdb()
    try:
        conn = sqlite3.connect(path)
        conn.executescript(history._SCHEMA_DDL.split("-- Learner360 weekly")[0])
        conn.execute("PRAGMA user_version = 4")
        conn.commit(); conn.close()
        assert history.momi_capture_status(db_path=path)["weeks"] == 0
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        conn.close()
    finally:
        os.remove(path)


def test_old_mmddyyyy_weeks_fixed_in_place():
    path = _tmpdb()
    try:
        history.momi_capture_status(db_path=path)          # create schema
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO momi_weekly (student_id, week_date) "
                     "VALUES ('1', '04-15-2026'), ('1', '10-01-2026')")
        conn.commit(); conn.close()
        hist = history.momi_weekly_for("1", db_path=path)  # reconnect -> fix-up
        assert [h["week_date"] for h in hist] == ["2026-04-15", "2026-10-01"], hist
    finally:
        os.remove(path)


ACTIVITY = [
    {"date": "2026-10-20T14:05:00Z", "category": "Course Material",
     "typeDescription": "Viewed course page", "contextDescription": "Unit 3",
     "courseCode": "C769"},
    {"date": "10-21-2026", "category": "Assessment",
     "typeDescription": "Started PA", "contextDescription": "", "courseCode": "C769"},
]


def test_activity_parse_shape_and_query():
    from datetime import datetime as dt
    assert l3.activity_query(dt(2026, 1, 2), dt(2026, 3, 4), "%m-%d-%Y") ==         "?startDate=01-02-2026&endDate=03-04-2026"
    rows = l3.parse_activity({"content": ACTIVITY})
    assert rows[0]["date"] == "2026-10-20" and rows[0]["at"].startswith("2026-10-20T14:05")
    assert rows[1]["date"] == "2026-10-21" and rows[1]["type"] == "Started PA"
    sh = l3.activity_shape([ACTIVITY])
    assert sh["span"] == ("2026-10-20", "2026-10-21")
    assert sh["categories"] == {"Course Material": 1, "Assessment": 1}
    assert "Unit 3" not in str(sh)                     # no context values


def test_persist_activity_idempotent_and_dry():
    path = _tmpdb()
    try:
        rows = l3.parse_activity(ACTIVITY)
        d = history.persist_l360_activity({"S1": rows}, dry=True, db_path=path)
        assert (d["new"], d["refreshed"]) == (2, 0)
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM l360_activity").fetchone()[0] == 0
        conn.close()
        r1 = history.persist_l360_activity({"S1": rows}, db_path=path)
        r2 = history.persist_l360_activity({"S1": rows}, db_path=path)
        assert (r1["new"], r2["new"], r2["refreshed"]) == (2, 0, 2), (r1, r2)
        assert r1["new_by_month"] == {"2026-10": 2}
    finally:
        os.remove(path)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    now = (os.path.exists(HISTORY_DB),
           os.path.getmtime(HISTORY_DB) if os.path.exists(HISTORY_DB) else None)
    assert now == _REAL_DB, "a test created/modified the REAL history.db"
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
