"""Tests for the monthly-throughput chart data (history.monthly_throughput):
unique student-assignments per month stacked by course, the rolling last-30-day
bar, the average-load figure, the three contacts-line metrics, and the course
filter. Seeds snapshots + notes directly into a temp history DB.
"""
import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import history  # noqa: E402

_NOW = datetime.datetime(2026, 8, 15)


def _seed(snaps, notes):
    """snaps: (collected_date, student_id, course_code).
    notes: (created_at, student_id, course_code, channel, direction)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = history._connect(path)
    for i, (date, sid, cc) in enumerate(snaps):
        conn.execute(
            "INSERT INTO snapshots (collected_at, collected_date, student_id, "
            "course_code) VALUES (?,?,?,?)",
            (f"{date}T00:00:{i:02d}", date, sid, cc))
    for i, (ca, sid, cc, ch, di) in enumerate(notes):
        conn.execute(
            "INSERT INTO notes (note_id, student_id, course_code, channel, "
            "direction, created_at) VALUES (?,?,?,?,?,?)",
            (f"n{i}", sid, cc, ch, di, ca))
    conn.commit()
    conn.close()
    return path


_SNAPS = [
    ("2026-06-05", "s1", "C1"),
    ("2026-06-20", "s1", "C1"),   # dup within June -> still one unique
    ("2026-06-10", "s2", "C1"),
    ("2026-06-12", "s3", "C2"),
    ("2026-07-05", "s1", "C1"),
    ("2026-07-05", "s4", "C1"),
    ("2026-08-10", "s5", "C1"),
    ("2026-08-11", "s3", "C2"),
]
_NOTES = [
    ("2026-06-05", "s1", "C1", "text", "outbound"),
    ("2026-06-06", "s1", "C1", "text", "outbound"),
    ("2026-06-07", "s2", "C1", "email", "inbound"),    # inbound: "all" only
    ("2026-07-10", "s4", "C1", "call", "outbound"),
    ("2026-08-02", "s5", "C1", "note", "outbound"),    # note channel: "all" only
]


def _months(d):
    return {m["month"]: m for m in d["months"]}


def test_unique_students_stacked_by_course():
    p = _seed(_SNAPS, _NOTES)
    try:
        d = history.monthly_throughput(db_path=p, now=_NOW)
        m = _months(d)
        assert m["2026-06"]["by_course"] == {"C1": 2, "C2": 1}
        assert m["2026-06"]["total"] == 3          # stack sums to the bar
        assert m["2026-07"]["by_course"] == {"C1": 2}
        assert m["2026-08"]["total"] == 2
        assert d["courses"] == ["C1", "C2"]
    finally:
        os.unlink(p)


def test_avg_load_excludes_last30():
    p = _seed(_SNAPS, _NOTES)
    try:
        d = history.monthly_throughput(db_path=p, now=_NOW)
        assert d["months_count"] == 3              # June, July, Aug (not last30)
        assert abs(d["avg_load"] - (3 + 2 + 2) / 3) < 1e-9
        last30 = _months(d)["last30"]
        # only 2026-08-10 (s5/C1) + 2026-08-11 (s3/C2) fall in the last 30 days
        assert last30["by_course"] == {"C1": 1, "C2": 1}
        assert last30["total"] == 2
    finally:
        os.unlink(p)


def test_contacts_metric_sent():
    p = _seed(_SNAPS, _NOTES)
    try:
        m = _months(history.monthly_throughput(db_path=p, contacts="sent",
                                               now=_NOW))
        assert m["2026-06"]["contacts"] == 2       # two outbound texts to s1
        assert m["2026-07"]["contacts"] == 1       # one outbound call
        assert m["2026-08"]["contacts"] == 0       # 'note' channel isn't outreach
    finally:
        os.unlink(p)


def test_contacts_mine_only():
    p = _seed(_SNAPS, [])
    try:
        conn = history._connect(p)
        conn.execute("UPDATE snapshots SET extra_json = ?",
                     ('{"LoggedInUserId": "005ME0000000001AAA"}',))
        rows = [  # (id, created, author)
            ("s1", "2026-06-05T10:00:00.000Z", "/005ME0000000001AAA"),  # me (SF)
            ("s2", "2026-06-06T10:00:00.000Z", "/005PM0000000009AAA"),  # PM (SF)
            ("l360:a", "2026-06-07T10:00:00.000Z", "Jim Ashe"),        # me (360)
            ("l360:b", "2026-06-08T10:00:00.000Z", "Pat Mentor"),      # PM (360)
        ]
        for nid, ca, au in rows:
            conn.execute("INSERT INTO notes (note_id, student_id, course_code, "
                         "channel, direction, created_at, author) VALUES "
                         "(?,?,?,?,?,?,?)", (nid, "s1", "C1", "email",
                                            "outbound", ca, au))
        # Staged Learner360 copies of MY SF notes teach the name (≥5 pairs).
        for i in range(5):
            ca = f"2026-05-0{i + 1}T09:00:00.000Z"
            conn.execute("INSERT INTO notes (note_id, student_id, created_at, "
                         "author) VALUES (?,?,?,?)",
                         (f"old{i}", "s1", ca, "/005ME0000000001AAA"))
            conn.execute("INSERT INTO l360_notes_staging (student_id, created_at, "
                         "author) VALUES (?,?,?)", ("s1", ca, "Jim Ashe"))
        conn.commit()
        conn.close()
        allm = _months(history.monthly_throughput(db_path=p, now=_NOW))
        assert allm["2026-06"]["contacts"] == 4
        d = history.monthly_throughput(db_path=p, mine=True, now=_NOW)
        assert _months(d)["2026-06"]["contacts"] == 2, _months(d)["2026-06"]
        assert d["mine_keys"] == {"sf": True, "l360": True}
        assert history.my_note_author_keys(db_path=p)["l360_name"] == "Jim Ashe"
    finally:
        os.unlink(p)


def test_contacts_metric_unique_and_all():
    p = _seed(_SNAPS, _NOTES)
    try:
        mu = _months(history.monthly_throughput(db_path=p, contacts="unique",
                                                now=_NOW))
        assert mu["2026-06"]["contacts"] == 1      # both texts are the same s1
        ma = _months(history.monthly_throughput(db_path=p, contacts="all",
                                                now=_NOW))
        assert ma["2026-06"]["contacts"] == 3      # 2 texts + 1 inbound email
        assert ma["2026-08"]["contacts"] == 1      # the admin note counts here
    finally:
        os.unlink(p)


def test_course_filter_restricts_both_series():
    p = _seed(_SNAPS, _NOTES)
    try:
        d = history.monthly_throughput(db_path=p, courses={"C2"}, contacts="all",
                                       now=_NOW)
        m = _months(d)
        assert "2026-07" not in m                  # no C2 students in July
        assert m["2026-06"]["by_course"] == {"C2": 1}
        # every seeded note is on C1, so a strict C2 filter drops them all
        assert all(mo["contacts"] == 0 for mo in d["months"])
    finally:
        os.unlink(p)


def test_date_window_limits_months():
    p = _seed(_SNAPS, _NOTES)
    try:
        d = history.monthly_throughput(db_path=p, date_from="2026-07-01",
                                       date_to="2026-08-31", now=_NOW)
        assert [m["month"] for m in d["months"] if m["month"] != "last30"] \
            == ["2026-07", "2026-08"]
    finally:
        os.unlink(p)


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
