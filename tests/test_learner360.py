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
import _real_db_guard  # noqa: E402,F401  (opening the real history.db raises)

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
        rows = l3.parse_activity(ACTIVITY + [ACTIVITY[1]])   # same-day repeat
        d = history.persist_l360_activity({"S1": rows}, dry=True, db_path=path)
        assert (d["events"], d["rows_new"], d["rows_refreshed"]) == (3, 2, 0), d
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM l360_activity").fetchone()[0] == 0
        conn.close()
        r1 = history.persist_l360_activity({"S1": rows}, db_path=path)
        r2 = history.persist_l360_activity({"S1": rows}, db_path=path)
        assert (r1["rows_new"], r2["rows_new"], r2["rows_refreshed"]) == (2, 0, 2)
        assert r1["events_by_month"] == {"2026-10": 3}
        conn = sqlite3.connect(path)
        occ = dict(conn.execute("SELECT type, occurrences FROM l360_activity"))
        conn.close()
        assert occ == {"Viewed course page": 1, "Started PA": 2}, occ
    finally:
        os.remove(path)


def test_activity_occurrences_column_added_to_existing_table():
    path = _tmpdb()
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE l360_activity (student_id TEXT NOT NULL, "
                     "activity_date TEXT NOT NULL, event_at TEXT NOT NULL "
                     "DEFAULT '', category TEXT NOT NULL DEFAULT '', type TEXT "
                     "NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '', "
                     "course_code TEXT NOT NULL DEFAULT '', first_seen_at TEXT, "
                     "last_seen_at TEXT, PRIMARY KEY (student_id, activity_date, "
                     "event_at, category, type, context, course_code))")
        conn.commit(); conn.close()
        history.persist_l360_activity({"S1": l3.parse_activity(ACTIVITY)},
                                      db_path=path)
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT SUM(occurrences) FROM l360_activity"
                            ).fetchone()[0] == 2
        conn.close()
    finally:
        os.remove(path)


def _seed_engagement(path):
    conn = history._connect(path)
    snap = [  # (date, sid, course, task1, course start)
        ("2026-10-26", "0001", "C1", "2026-10-05 (1)", "2026-09-01"),
        ("2026-10-26", "0002", "C1", "2026-10-06 (1)", "2026-09-01"),
        ("2026-10-26", "0003", "C1", "2026-10-07 (2)", "2026-09-01"),
        ("2026-10-26", "0004", "C2", "", "2026-10-01"),
    ]
    for i, (d, sid, c, t1, st) in enumerate(snap):
        conn.execute("INSERT INTO snapshots (collected_at, collected_date, "
                     "student_id, course_code, task1, extra_json) VALUES "
                     "(?,?,?,?,?,?)", (f"{d}T00:00:{i:02d}", d, sid, c, t1,
                                       '{"CourseStartDate": "%s"}' % st))
    conn.commit(); conn.close()
    act = {}
    for sid, d in (("0001", "2026-10-05"), ("0002", "2026-10-06"),
                   ("0003", "2026-10-07")):
        act[sid] = [
            {"date": d, "category": "Assessment",
             "type": "Dream Machine Assessment Submitted for Evaluation",
             "context": "ABC1 Task 1: Thing submitted for evaluation"},
            {"date": d, "category": "Assessment",       # other course's PA
             "type": "Dream Machine Assessment Submitted for Evaluation",
             "context": "ZZZ9 Task 2: Other submitted for evaluation"},
        ]
    act["0001"] += [
        {"date": "2026-10-20", "category": "Coursework",
         "type": "Learning Resource", "context": "x", "course": "C1"},
        {"date": "2026-10-24", "category": "Coursework",
         "type": "Learning Resource", "context": "y", "course": "C1"},
        {"date": "2026-10-25", "category": "Coursework",
         "type": "Learning Resource", "context": "z", "course": "C9"},
        {"date": "2026-10-10", "category": "Assessment",
         "type": "Dream Machine Assessment Returned for Revision",
         "context": "ABC1 Task 1: Thing returned"},
        {"date": "2026-08-01", "category": "Assessment",   # before course start
         "type": "Dream Machine Performance Assessment Passed",
         "context": "ABC1 Task 1: Thing passed"},
        {"date": "2026-10-15", "category": "Contact",
         "type": "Academic Discussion with Faculty", "context": "", "course": ""},
    ]
    history.persist_l360_activity(act, db_path=path)


def test_engagement_signals_and_learned_codes():
    from datetime import datetime as dt
    path = _tmpdb()
    try:
        _seed_engagement(path)
        today = dt(2026, 10, 27)
        assert history.learn_course_assessment_codes(
            db_path=path, refresh=True, today=today) == {"C1": "ABC1"}
        sig = history.engagement_signals(db_path=path, today=today)
        s1 = sig[("1", "C1")]
        assert s1["days_since_course_activity"] == 3          # Oct 24, C1 only
        assert s1["course_activity_days_14"] == 2
        assert s1["days_since_any_activity"] == 2             # Oct 25, other course
        assert (s1["pa_submissions"], s1["pa_returns"], s1["pa_passes"]) == (1, 1, 0)
        assert s1["faculty_discussions_30"] == 1
        assert sig[("2", "C1")]["days_since_course_activity"] is None
        s4 = sig[("4", "C2")]                                 # no activity at all
        assert s4["days_since_any_activity"] is None and s4["pa_submissions"] is None
    finally:
        os.remove(path)


def test_discovery_baseline_diff():
    from src import learner360 as l3
    gw = "https://faculty-api-gateway.wgu.edu/learner360/v1/"
    store = {}
    l3.discovery_add(store, "GET", gw + "plan/abc12345678", 200,
                     {"terms": [{"code": "x"}]})
    seen = {}
    assert l3.discovery_diff(store, seen) == [("GET /learner360/v1/plan/abc12345678", None)] \
        or l3.discovery_diff(store, seen)[0][1] is None
    l3.discovery_merge_seen(seen, store)
    assert l3.discovery_diff(store, seen) == []           # nothing new now
    # A new field on the known feed, and a brand-new feed.
    l3.discovery_add(store, "GET", gw + "plan/abc12345678", 200,
                     {"terms": [{"code": "x", "cus": 3}], "gpa": 3.1})
    l3.discovery_add(store, "POST", gw + "summary/search", 200, {"a": 1})
    diff = dict(l3.discovery_diff(store, seen))
    key_plan = next(k for k in store if "/plan/" in k)
    assert diff[key_plan] == ["gpa", "terms[].cus"]
    assert diff["POST /learner360/v1/summary/search"] is None
    lines = l3.discovery_diff_lines(l3.discovery_diff(store, seen), store)
    assert any("new data feed: POST /learner360/v1/summary/search" in ln
               for ln in lines)
    assert any("new field(s) on" in ln and "terms[].cus" in ln for ln in lines)


def test_action_signals():
    from datetime import datetime as dt
    path = _tmpdb()
    try:
        _seed_engagement(path)                   # learns C1 → ABC1
        conn = history._connect(path)
        for i, (sid, st) in enumerate((("0011", "2026-09-01"),
                                       ("0012", "2026-09-01"),
                                       ("0013", "2026-10-25"),
                                       ("0014", "2026-09-01"))):
            conn.execute("INSERT INTO snapshots (collected_at, collected_date, "
                         "student_id, course_code, task1, extra_json) VALUES "
                         "(?,?,?,?,?,?)", (f"2026-10-26T01:00:{i:02d}",
                                           "2026-10-26", sid, "C1", "",
                                           '{"CourseStartDate": "%s"}' % st))
        conn.commit(); conn.close()
        cw = lambda d, typ, ctx, c: {"date": d, "category": "Coursework",
                                     "type": typ, "context": ctx, "course": c}
        history.persist_l360_activity({
            "0011": [  # never opened C1 (system events only); busy elsewhere
                cw("2026-09-02", "Learning Resource",
                   "Learning resource 'v4 [] X' provisioned for C1", "C1"),
                cw("2026-09-01", "Term Enrollment", "C1 - X enrolled", "C1"),
                cw("2026-10-20", "Course of Study Link", "Go to C9", "C9"),
                cw("2026-10-27", "Course of Study Link", "after as_of", "C6"),
                {"date": "2026-10-22", "category": "Assessment",
                 "type": "Performance Assessment",
                 "context": "Y completed for C8", "course": "C8"},
                {"date": "2026-10-18", "category": "Assessment",
                 "type": "Objective Assessment",
                 "context": "Z completed for C7", "course": "C7"},
            ],
            "0012": [cw("2026-10-21", "Course of Study Link", "Go", "C1")],
            "0013": [cw("2026-10-20", "Course of Study Link", "Go", "C9")],
            "0014": [{"date": "2026-06-01", "category": "Assessment",
                      "type": "Dream Machine Assessment Submitted for Evaluation",
                      "context": "ABC1 Task 1: T submitted"}],
        }, db_path=path)
        history.mark_l360_activity_imported(db_path=path,
                                            now=dt(2026, 10, 26, 9))
        history.learn_course_assessment_codes(db_path=path, refresh=True,
                                              today=dt(2026, 10, 27))
        sig = history.engagement_signals(db_path=path, today=dt(2026, 10, 27))
        a = sig[("11", "C1")]
        assert a["sig_not_opened"] and not a["sig_working_no_submit"]
        assert a["sig_active_elsewhere"] == "C8, C9, C7"   # C6 is after as_of
        assert a["sig_finished_other"] == "C8 PA passed, C7 OA taken"
        assert a["sig_finished_days"] == 5
        b = sig[("12", "C1")]
        assert b["sig_working_no_submit"] and not b["sig_not_opened"]
        assert b["sig_active_elsewhere"] == ""          # working in C1
        assert not sig[("13", "C1")]["sig_not_opened"]   # started < 5 days ago
        d = sig[("14", "C1")]                            # earlier-term PA work
        assert not d["sig_not_opened"] and not d["sig_working_no_submit"]
        # Viewer summary column.
        import importlib
        launcher = importlib.import_module("scripts.launcher")
        row = {"Task1Status": ""}
        launcher.App._apply_signal_columns(row, a)
        assert row["Signals"] == ("Passed C8 PA · Never opened course"
                                  " · Active elsewhere: C8, C9, C7"), row
        row = {"Task1Status": "Passed"}                  # has a task → opened
        launcher.App._apply_signal_columns(row, a)
        assert row["SigNotOpened"] == "No"
        row = {}
        launcher.App._apply_signal_columns(row, {})
        assert row["Signals"] == "" and row["SigNotOpened"] == ""
    finally:
        os.remove(path)


def test_team_query_ids_and_shape_are_values_free():
    q = l3.team_caseload_query(2, ["u1", "u2"], "values")
    assert q["page"] == 2 and q["filters"] == [
        {"field": "caseloadOwner", "operator": "IN", "values": ["u1", "u2"]}]
    u = "09eba36d-f48c-4bb5-87bd-3a62200438b5"
    assert l3.ids_from([{"name": "Team X", "id": u}, {"id": "not-a-uuid"}]) == [u]
    sh = l3.json_shape({"content": [{"studentId": "000123456", "name": "Pat"}],
                        "page": {"totalElements": 3}})
    assert "000123456" not in str(sh) and "Pat" not in str(sh)
    assert sh["content"][0] == "list[1]" and sh["page"] == {"totalElements": "int"}


def test_roster_coverage_counts():
    from src import l360_roster
    roster = [
        {"CourseCode": "C769", "StudentID": "0001", "StudentEmail": "a@wgu.edu",
         "CourseMentor": "Jim Ashe"},
        {"CourseCode": "C769", "StudentID": "0002", "StudentEmail": "b@wgu.edu",
         "CourseMentor": "Tawnya Lee"},
        {"CourseCode": "C769", "StudentID": "0003", "StudentEmail": "c@wgu.edu",
         "CourseMentor": ""},                                  # unassigned
        {"CourseCode": "X999", "StudentID": "0009", "StudentEmail": "z@wgu.edu",
         "CourseMentor": "Other Person"},                      # other course
    ]
    rows = [
        {"student_id": "1", "email": "a@wgu.edu", "courses": ["C769"],
         "owner": "Ashe, Jim"},
        {"student_id": "", "email": "b@wgu.edu", "courses": ["C769"],
         "owner": "Jeff Smith"},                               # CI disagrees
        {"student_id": "7", "email": "", "courses": ["C769"], "owner": "Jim Ashe"},
    ]
    c = l360_roster.compare_coverage(rows, roster, ["C769"])
    assert (c["roster_assigned"], c["roster_unassigned"]) == (2, 1)
    assert (c["found_by_id"], c["found_by_email"]) == (1, 2)
    assert (c["ci_compared"], c["ci_agree"]) == (2, 1)
    assert c["unassigned_found"] == 0
    assert c["l360_in_courses"] == 2 and c["l360_not_in_roster"] == 1  # "7"
    assert c["l360_with_email"] == 2


def test_l360_session_replaces_a_stale_token():
    import types
    from src.browser_worker import BrowserWorker as BW

    class Page:
        reloads = 0
        waits = 0

        def evaluate(self, js, args):
            auth = (args[1] or {}).get("authorization")
            return {"status": 401 if auth == "old" else 200, "body": "{}"}

        def wait_for_timeout(self, ms):
            Page.waits += 1
            if Page.waits == 2:                     # app refreshes its token
                fake._l360_auth = {"headers": {"authorization": "new"}}

        def reload(self, **kw):
            Page.reloads += 1

    fake = types.SimpleNamespace(_l360_auth=None)
    h = BW._l360_validated_headers(fake, Page(), {"authorization": "old"}, "u1")
    assert h == {"authorization": "new"} and Page.reloads == 0, (h, Page.reloads)
    fake._l360_auth = None
    good = BW._l360_validated_headers(fake, Page(), {"authorization": "ok"}, "u1")
    assert good == {"authorization": "ok"}


def test_roster_replay_of_labeler_log():
    from src import l360_roster
    rows = [{"email": "a@wgu.edu", "courses": ["C769"], "owner": "Jim Ashe"},
            {"email": "b@wgu.edu", "courses": ["C769"], "owner": "Lee, Tawnya"},
            {"email": "n@wgu.edu", "courses": ["D502"], "owner": "Jeff Smith"}]
    log = [
        {"sender_smtp": "A@wgu.edu", "method": "email",
         "categories": ["C769", "Jim"]},                         # same
        {"sender_smtp": "b@wgu.edu", "method": "email",
         "categories": ["C769", "Candice"]},                     # CI differs
        {"sender_smtp": "gone@wgu.edu", "method": "email",
         "categories": ["C964", "Jim"]},                         # lost
        {"sender_smtp": "n@wgu.edu", "method": "none",
         "categories": ["Unidentified"]},                        # newly found
        {"sender_smtp": "me@gmail.com", "method": "none", "categories": []},
    ]
    r = l360_roster.replay_log(rows, log, ["C769", "C964", "D502"])
    assert (r["senders"], r["identified_before"], r["identified_l360"]) == (5, 3, 3), r
    assert (r["both"], r["same_categories"], r["ci_differs"]) == (2, 1, 1), r
    assert (r["lost"], r["newly_identified"], r["course_differs"]) == (1, 1, 0), r


def test_roster_overlay_rules():
    from src import l360_roster
    assert l360_roster.display_name("Ashe, Jim") == "Jim Ashe"
    roster = [
        {"CourseCode": "C769", "StudentID": "0001", "StudentEmail": "a@wgu.edu",
         "CourseMentor": "Jim Ashe"},                       # agrees
        {"CourseCode": "C769", "StudentID": "0002", "StudentEmail": "b@wgu.edu",
         "CourseMentor": "Candice Roe"},                    # reassigned
        {"CourseCode": "C769", "StudentID": "0003", "StudentEmail": "c@wgu.edu",
         "CourseMentor": ""},                               # unassigned → filled
        {"CourseCode": "C769", "StudentID": "0004", "StudentEmail": "d@wgu.edu",
         "CourseMentor": "Other Team"},                     # not in L360: kept
    ]
    team = [
        {"student_id": "1", "course_code": "C769", "email": "a@wgu.edu",
         "name": "A", "owner": "Ashe, Jim"},
        {"student_id": "2", "course_code": "C769", "email": "b@wgu.edu",
         "name": "B", "owner": "Lee, Tawnya"},
        {"student_id": "3", "course_code": "C769", "email": "c@wgu.edu",
         "name": "C", "owner": "Jeff Smith"},
        {"student_id": "9", "course_code": "C769", "email": "n@wgu.edu",
         "name": "New Student", "owner": "Jim Ashe"},       # added
        {"student_id": "8", "course_code": "X999", "email": "x@wgu.edu",
         "name": "X", "owner": "Jim Ashe"},                 # other course: ignored
    ]
    merged, st = l360_roster.overlay(roster, team, ["C769"])
    assert st == {"agreed": 1, "corrected": 1, "filled": 1, "added": 1}, st
    by = {r["StudentEmail"]: r for r in merged}
    assert by["b@wgu.edu"]["CourseMentor"] == "Tawnya Lee"
    assert by["b@wgu.edu"]["CourseMentorScan"] == "Candice Roe"
    assert by["c@wgu.edu"]["CourseMentor"] == "Jeff Smith"
    assert by["d@wgu.edu"]["CourseMentor"] == "Other Team"   # never removed
    assert by["n@wgu.edu"]["Source"] == "learner360" and len(merged) == 5
    assert all("x@wgu.edu" != r["StudentEmail"] for r in merged)


def test_team_roster_store_load_staleness_and_readonly():
    from datetime import datetime as dt, timedelta
    path = _tmpdb()
    try:
        missing = path + ".nope"
        assert history.load_l360_team_roster(db_path=missing) == ([], None)
        assert not os.path.exists(missing)        # read-only: never creates
        rows = [{"student_id": "0001", "email": "a@wgu.edu", "first": "Ann",
                 "last": "Bee", "courses": ["C769", "D502"], "owner": "Ashe, Jim",
                 "mentor": "Pat PM"}]
        t0 = dt(2026, 11, 1, 9, 0)
        assert history.replace_l360_team_roster(rows, db_path=path, now=t0) == 2
        got, at = history.load_l360_team_roster(db_path=path, now=t0)
        assert at.startswith("2026-11-01") and len(got) == 2
        assert {g["course_code"] for g in got} == {"C769", "D502"}
        assert got[0]["name"] == "Ann Bee" and got[0]["mentor_name"] == "Pat PM"
        stale = history.load_l360_team_roster(db_path=path,
                                              now=t0 + timedelta(days=5))
        assert stale[0] == [] and stale[1].startswith("2026-11-01")
        # replace is wholesale
        history.replace_l360_team_roster(rows[:0], db_path=path, now=t0)
        assert history.load_l360_team_roster(db_path=path, now=t0)[0] == []
    finally:
        os.remove(path)


def test_overlay_extras_from_temp_db():
    path = _tmpdb()
    try:
        history.persist_notes([
            {"type": "Email to Student", "text": "<p>old</p>",
             "date": "2026-09-01T10:00:00.000Z", "author": "/005ABC"},
            {"type": "Email from Student", "text": "newest &amp; last",
             "date": "2026-10-30T10:00:00.000Z", "author": "Jim Ashe"},
        ], student_id="000123", db_path=path)
        conn = history._connect(path)
        conn.execute("INSERT INTO l360_ids VALUES ('000123', 'AAAA-uuid', '', '')")
        conn.executemany("INSERT INTO momi_weekly (student_id, week_date, "
                         "momentum_score) VALUES (?,?,?)",
                         [("000123", "2026-10-01", 0.5),
                          ("000123", "2026-09-01", 0.4)])
        conn.commit(); conn.close()
        x = history.overlay_extras(["000123", "999"], db_path=path)
        assert x["uuid_map"] == {"aaaa-uuid": "123"}
        n = x["notes"]["123"]
        assert [k["text"] for k in n] == ["newest & last", "old"]
        assert n[0]["author"] == "Jim Ashe" and n[1]["author"] == ""
        assert x["momi"]["123"] == [["2026-09-01", 0.4], ["2026-10-01", 0.5]]
    finally:
        os.remove(path)


def test_ms_until_due_rechecks_at_due_time():
    from datetime import datetime as _dt
    from src import learner360 as l3
    now = _dt(2026, 11, 3, 8, 12)
    cap = 6 * 3600_000
    # Due at 09:22 → re-check ~71 min later, not 6 h later.
    assert l3.ms_until_due("2026-10-27T09:22:00", 7, cap, now=now) == (70 * 60 + 60) * 1000
    assert l3.ms_until_due("2026-11-02T09:00:00", 7, cap, now=now) == cap   # far off
    assert l3.ms_until_due("2026-10-01T00:00:00", 7, cap, now=now) == 60_000  # overdue
    assert l3.ms_until_due(None, 7, cap, now=now) == cap


def test_discovery_keeps_structure_never_values():
    import json as _json
    from src import learner360 as l3
    store = {}
    uuid = "3f2a9c1e-1111-4222-8333-944455556666"
    body = {"learnerId": uuid, "degreePlan": {"terms": [
        {"termCode": "202611", "courses": [{"code": "C769", "status": "Passed",
                                            "cus": 4, "done": True}]},
        {"termCode": "202605", "courses": [], "note": None}]},
        "byCourse": {"C769": {"x": 1}, "D502": {"x": 2}},
        "email": "student@wgu.edu"}
    l3.discovery_add(store, "GET",
                     f"https://faculty-api-gateway.wgu.edu/learner360/v1/"
                     f"degree-plan/{uuid}?startDate=01-01-2026&x=1", 200, body)
    l3.discovery_add(store, "GET",
                     "https://faculty-api-gateway.wgu.edu/learner360/v1/"
                     "momentum-indicator/abc?y=2", 200, {"m": 1})
    key = "GET /learner360/v1/degree-plan/{id}"
    assert key in store, list(store)
    e = store[key]
    assert e["query"] == ["startDate", "x"] and e["statuses"] == [200]
    sh = e["shape"]
    assert sh["degreePlan"]["terms"][0]["courses"][0] == {
        "code": "str", "status": "str", "cus": "num", "done": "bool"}
    assert list(sh["byCourse"]) == ["{id}"]          # course-code keys collapsed
    dumped = _json.dumps(store)
    for value in (uuid, "202611", "C769", "Passed", "student@wgu.edu",
                  "01-01-2026"):
        assert value not in dumped, value
    lines = l3.discovery_summary(store)
    assert lines[0].startswith("  [NEW] GET /learner360/v1/degree-plan/{id}")
    assert any("[known]" in ln for ln in lines)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
