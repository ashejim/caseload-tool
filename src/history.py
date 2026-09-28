"""Local longitudinal history of the caseload's dynamic fields.

The Salesforce caseload is exported as a CSV and reloaded fresh each time
the app refreshes. Fields like ``Momentum`` and ``LatestTaskStatus`` change
over time, and a student who passes or drops simply *disappears* from the
export — so without recording snapshots there's no way to review a trend or
notice that someone left and needs a follow-up.

This module snapshots the dynamic fields into a small SQLite DB
(``config.HISTORY_DB``) on reload. SQLite (stdlib ``sqlite3``) so the data
reads straight into pandas (``pd.read_sql_query``) and supports cheap
timeline / "who-departed" queries; an Export-to-CSV escape hatch is provided.

Design notes:
- **Sampling = at most once per interval window.** Captures are bucketed into
  interval-aligned windows (``_bucket``): a 24 h interval gives one sample per
  calendar day; 6 h gives four. Re-running within the same window upserts
  (newest export wins) rather than duplicating, and never touches an earlier
  window's sample. The interval is a user setting
  (``Settings.history_capture_interval_hours``).
- **Departure** = a ``(student_id, course_code)`` present in the most recent
  *prior-day* collection but absent now. Comparison is always day-grained
  (prior calendar day, gap-aware) regardless of the sampling interval, so
  sub-day samples never generate departure noise.
- ``extra_json`` keeps every non-core CSV column so nothing is lost (the export
  is ~106 columns wide); core fields accept both API and display-name header
  spellings, so changing the export's columns doesn't drop data.

All writes are wrapped so a DB failure is non-fatal to the caller (the reload
path must never break because history couldn't be written).
"""
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from src import caseload_csv
from src.config import HISTORY_DB

_SCHEMA_VERSION = 4

# Ordinal Momentum scale as it appears in the WGU export. Unknown / blank
# values map to NULL rank (they still store the raw label).
_MOMENTUM_RANK = {"Low": 1, "Med Low": 2, "Med": 3, "Med High": 4, "High": 5}

# CSV header -> snapshot column. Everything NOT listed here is preserved in
# the per-row ``extra_json`` blob. Core fields also accept the display-name
# spelling (see _candidate_headers).
_CORE_HEADERS = {
    "StudentID": "student_id",
    "CourseCode": "course_code",
    "Name": "name",
    "StudentEmail": "student_email",
    "Momentum": "momentum",
    "LatestTaskStatus": "latest_task_status",
    "Task1": "task1",
    "Task2": "task2",
    "Task3": "task3",
    "LatestCourseNote": "latest_course_note",
    "CourseFollowupNote": "followup_note",
    "CourseFollowupDate": "followup_date",
}

# Column order for snapshot inserts.
_SNAP_COLS = [
    "collected_at", "collected_date", "student_id", "course_code", "name",
    "student_email", "momentum", "momentum_rank", "latest_task_status",
    "task1", "task2", "task3", "latest_course_note", "followup_note",
    "followup_date", "extra_json",
]

# Reverse of _CORE_HEADERS: snapshot column -> canonical CSV header. Used to
# rebuild a caseload-style row (CSV-header keys) from a stored snapshot.
_SNAP_TO_CSV = {snap_col: csv_header for csv_header, snap_col in _CORE_HEADERS.items()}

# If a fresh export has fewer than this fraction of the prior collection's
# rows, treat it as a truncated/filtered export and suppress departures.
_PARTIAL_EXPORT_FRACTION = 0.5


def momentum_rank(label: str) -> Optional[int]:
    """1..5 for the ordinal Momentum label, or None if unknown/blank."""
    return _MOMENTUM_RANK.get((label or "").strip())


def _bucket(now: datetime, interval_hours: int) -> str:
    """Interval-aligned window id for ``now``. A >=24 h interval buckets by
    calendar date (one sample/day, exactly the original behavior); a smaller
    interval splits the day into ``24 // interval`` fixed slots so windows
    never cross a sample. Same bucket => the sample is updated in place."""
    d = now.date().isoformat()
    if interval_hours >= 24 or interval_hours <= 0:
        return d
    return f"{d}#{now.hour // interval_hours}"


# ----------------------------------------------------------------------
# connection + schema (+ v1->v2 migration)
# ----------------------------------------------------------------------
def _connect(db_path=HISTORY_DB) -> sqlite3.Connection:
    """Open the history DB (creating/upgrading the schema as needed). Rows
    come back as ``sqlite3.Row`` so columns are addressable by name."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS collections (
    collected_at   TEXT PRIMARY KEY,
    collected_date TEXT NOT NULL,
    bucket         TEXT NOT NULL,
    csv_mtime      TEXT,
    row_count      INTEGER NOT NULL,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS ix_collections_date   ON collections(collected_date);
CREATE INDEX IF NOT EXISTS ix_collections_bucket ON collections(bucket);

CREATE TABLE IF NOT EXISTS snapshots (
    collected_at       TEXT NOT NULL,
    collected_date     TEXT NOT NULL,
    student_id         TEXT NOT NULL,
    course_code        TEXT NOT NULL,
    name               TEXT,
    student_email      TEXT,
    momentum           TEXT,
    momentum_rank      INTEGER,
    latest_task_status TEXT,
    task1 TEXT, task2 TEXT, task3 TEXT,
    latest_course_note TEXT,
    followup_note      TEXT,
    followup_date      TEXT,
    extra_json         TEXT,
    PRIMARY KEY (collected_at, student_id, course_code)
);
CREATE INDEX IF NOT EXISTS ix_snap_student_at ON snapshots(student_id, collected_at);
CREATE INDEX IF NOT EXISTS ix_snap_date       ON snapshots(collected_date);

-- Small key/value store for cross-cutting bookkeeping (e.g. when the passed
-- outcomes archive was last ingested, for the staleness reminder).
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Resolved *outcomes* for students who have left the live caseload because they
-- passed. The live export drops passers, so the daily snapshots never capture
-- the final result; the only source is WGU's "passed in the last 30 days"
-- caseload view, downloaded as CSV and ingested here. One row per
-- (student, course); re-ingesting a fresh archive upserts (first_seen_at is
-- preserved, everything else refreshed). ``ic_end_date`` is the extension
-- deadline (IC End Date) and takes precedence over ``term_end_date`` when
-- judging whether a pass was "in time".
CREATE TABLE IF NOT EXISTS outcomes (
    student_id    TEXT NOT NULL,
    course_code   TEXT NOT NULL,
    name          TEXT,
    student_email TEXT,
    momentum_at_outcome      TEXT,
    momentum_rank_at_outcome INTEGER,
    latest_task_status       TEXT,
    outcome       TEXT,                 -- 'passed' (only outcome this view carries)
    pass_date     TEXT,                 -- ActualEndDate
    ic_end_date   TEXT,                 -- Icenddate (extension deadline)
    term_end_date TEXT,                 -- TermEndDate
    course_start_date TEXT,
    term_start_date   TEXT,
    other_courses      TEXT,            -- raw OtherCourses list at outcome time
    other_course_count INTEGER,         -- distinct courses OTHER than this one
    entry_momentum       TEXT,          -- Momentum at/near course entry (frozen
    entry_momentum_rank  INTEGER,       --   from the snapshot nearest CourseStart)
    entry_captured       INTEGER,       -- 1 if a genuine at-entry reading exists
    first_seen_at TEXT,                 -- ingest ts we first recorded this outcome
    last_ingest_at TEXT,                -- ingest ts of the most recent refresh
    source_file   TEXT,
    extra_json    TEXT,
    PRIMARY KEY (student_id, course_code)
);
CREATE INDEX IF NOT EXISTS ix_outcomes_course ON outcomes(course_code);

-- Per-student contact NOTES scraped from the SF Notes History tab. One row per
-- SF note record (note_id = the note's Salesforce record id, parsed from its
-- anchor href). SF notes are editable but never deleted, so re-scraping UPSERTS
-- (first_seen_at preserved; body/type/direction refreshed). Captures the REAL
-- event time (WGUCreationDateTime__c) plus derived channel + direction, so
-- inbound texts and true contact timing become analyzable — the thing daily
-- snapshots can't hold (they keep only the latest note). See
-- data_analysis/FINDINGS.md §6. contact_id is always reliable; student_id is
-- best-effort (join contact_id -> snapshots.extra_json.contactID to backfill).
CREATE TABLE IF NOT EXISTS notes (
    note_id       TEXT PRIMARY KEY,
    student_id    TEXT,
    contact_id    TEXT,
    course_code   TEXT,
    type          TEXT,        -- SF Type__c, e.g. 'Instant Message (IM) / Text'
    channel       TEXT,        -- derived: text / email / note / chatter / other
    direction     TEXT,        -- derived: inbound / outbound / '' (unknown)
    created_at    TEXT,        -- WGUCreationDateTime__c (the real event time)
    author        TEXT,
    subject       TEXT,
    body          TEXT,
    url           TEXT,
    first_seen_at TEXT,        -- when we first scraped this note
    last_seen_at  TEXT         -- most recent scrape (notes can be edited)
);
CREATE INDEX IF NOT EXISTS ix_notes_student ON notes(student_id, created_at);
CREATE INDEX IF NOT EXISTS ix_notes_contact ON notes(contact_id, created_at);
CREATE INDEX IF NOT EXISTS ix_notes_channel ON notes(channel, direction);

-- Per-student user-managed flags. Only MANUAL overrides live here; auto-derived
-- values (contact preference from note history, chase-list membership from the
-- live criteria) are computed on the fly. contact_pref: 'text'/'email'/'call'
-- set by the instructor (e.g. "student wants texts only") — takes precedence
-- over the auto-inferred preference. pronouns: free-form manual label
-- (e.g. 'she/her') — the caseload feed carries no gender/pronoun data, so this
-- is instructor-entered and blank by default.
CREATE TABLE IF NOT EXISTS student_flags (
    student_id   TEXT PRIMARY KEY,
    contact_pref TEXT,
    pronouns     TEXT,
    updated_at   TEXT
);

-- Persistent chase-list worklist. at_risk_students() recomputes membership
-- live; this table remembers who's on it and SINCE WHEN (first_listed_at),
-- when they last still qualified (last_listed_at), when they dropped off
-- (resolved_at — attempted a task / outcome resolved), and the instructor's
-- manual status. status: '' active / 'contacted' / 'dismissed'. Managed by
-- sync_chase_list() + set_chase_status().
CREATE TABLE IF NOT EXISTS chase_list (
    student_id      TEXT NOT NULL,
    course_code     TEXT NOT NULL,
    first_listed_at TEXT,
    last_listed_at  TEXT,
    status          TEXT,
    status_at       TEXT,
    resolved_at     TEXT,
    PRIMARY KEY (student_id, course_code)
);
"""


def _init_schema(conn: sqlite3.Connection) -> None:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collections'"
    ).fetchone() is not None
    # Upgrade an EXISTING older schema first (so the DDL below, which indexes
    # the new 'bucket' column, doesn't run against a pre-bucket table). v2->v3
    # (meta, outcomes) and v3->v4 (student_flags, chase_list) only ADD tables,
    # which the IF NOT EXISTS DDL handles — no data migration needed, so only
    # the v1->v2 rebuild is gated here.
    if has_tables and v < 2:
        _migrate_v1_to_v2(conn)
    conn.executescript(_SCHEMA_DDL)  # create from scratch / fill in any gaps
    _ensure_outcome_columns(conn)    # additive columns on an existing outcomes
    _ensure_student_flag_columns(conn)  # additive columns on existing flags
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()


def _ensure_outcome_columns(conn: sqlite3.Connection) -> None:
    """Add later-introduced outcome columns to a pre-existing table (CREATE
    TABLE IF NOT EXISTS won't alter an existing one). Cheap + idempotent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(outcomes)")}
    add = {
        "other_courses": "TEXT", "other_course_count": "INTEGER",
        "entry_momentum": "TEXT", "entry_momentum_rank": "INTEGER",
        "entry_captured": "INTEGER",
    }
    for name, typ in add.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE outcomes ADD COLUMN {name} {typ}")


def _ensure_student_flag_columns(conn: sqlite3.Connection) -> None:
    """Add later-introduced student_flags columns (e.g. pronouns) to a
    pre-existing table. Cheap + idempotent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(student_flags)")}
    for name, typ in {"pronouns": "TEXT"}.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE student_flags ADD COLUMN {name} {typ}")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 keyed snapshots by (collected_date, …) — one sample/day. v2 keys by
    (collected_at, …) so sub-day sampling is possible, and collections gain a
    ``bucket`` column. Data is preserved (v1 was daily, so existing rows are
    unique under the new key)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(collections)")]
    if cols and "bucket" not in cols:
        conn.execute("ALTER TABLE collections ADD COLUMN bucket TEXT")
        conn.execute("UPDATE collections SET bucket = collected_date "
                     "WHERE bucket IS NULL")
    # v1 had a UNIQUE index on collected_date; drop it so >1 sample/day is OK.
    conn.execute("DROP INDEX IF EXISTS ix_collections_date")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_collections_date "
                 "ON collections(collected_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_collections_bucket "
                 "ON collections(bucket)")
    # Rebuild snapshots to change the PRIMARY KEY (SQLite can't ALTER a PK).
    snap_pk = conn.execute(
        "SELECT 1 FROM pragma_table_info('snapshots') "
        "WHERE name='collected_date' AND pk > 0"
    ).fetchone()
    if snap_pk:  # still the v1 per-day PK -> rebuild
        cols_csv = ", ".join(_SNAP_COLS)
        conn.executescript(
            "CREATE TABLE snapshots__v2 ("
            " collected_at TEXT NOT NULL, collected_date TEXT NOT NULL,"
            " student_id TEXT NOT NULL, course_code TEXT NOT NULL, name TEXT,"
            " student_email TEXT, momentum TEXT, momentum_rank INTEGER,"
            " latest_task_status TEXT, task1 TEXT, task2 TEXT, task3 TEXT,"
            " latest_course_note TEXT, followup_note TEXT, followup_date TEXT,"
            " extra_json TEXT,"
            " PRIMARY KEY (collected_at, student_id, course_code));"
            f"INSERT INTO snapshots__v2 ({cols_csv}) "
            f"SELECT {cols_csv} FROM snapshots;"
            "DROP TABLE snapshots;"
            "ALTER TABLE snapshots__v2 RENAME TO snapshots;"
            "CREATE INDEX IF NOT EXISTS ix_snap_student_at "
            "ON snapshots(student_id, collected_at);"
            "CREATE INDEX IF NOT EXISTS ix_snap_date ON snapshots(collected_date);"
        )


# ----------------------------------------------------------------------
# row mapping
# ----------------------------------------------------------------------
def _candidate_headers(csv_header: str) -> list[str]:
    """A core field can arrive under its API header ('Task1', 'StudentID') or
    the display-name spelling ('Task 1', 'Student ID') depending on the user's
    Caseload view config. Reuse caseload_csv's mapping to accept either, so
    changing the export's column NAMING doesn't drop core fields."""
    disp = caseload_csv.CSV_TO_DISPLAY.get(csv_header)
    cands = [csv_header]
    if disp and disp != csv_header:
        cands.append(disp)
    return cands


_CORE_CANDIDATES = {h: _candidate_headers(h) for h in _CORE_HEADERS}
_CORE_HEADER_ALIASES = {a for cands in _CORE_CANDIDATES.values() for a in cands}


def _get(row: dict, csv_header: str) -> str:
    """First non-empty value among a core field's accepted header spellings,
    stripped; '' if absent/blank."""
    for h in _CORE_CANDIDATES[csv_header]:
        val = row.get(h)
        if isinstance(val, str):
            val = val.strip()
        if val:
            return val
    return ""


def _row_to_record(row: dict) -> Optional[dict]:
    """Map one CSV row dict to a snapshot record (sans collected_at/date).
    Returns None for rows lacking a usable (StudentID, CourseCode) key.
    Accepts API and display-name header spellings; non-core columns -> extra_json."""
    rec = {dest: _get(row, src) for src, dest in _CORE_HEADERS.items()}
    if not rec["student_id"] or not rec["course_code"]:
        return None
    rec["momentum_rank"] = momentum_rank(rec.get("momentum", ""))
    extra = {k: v for k, v in row.items()
             if k and k not in _CORE_HEADER_ALIASES}
    rec["extra_json"] = json.dumps(extra, ensure_ascii=False)
    return rec


def _classify(latest_task_status: str) -> str:
    """A departed student whose last status was 'Passed' likely completed;
    anything else (incl. blank) is treated as needing follow-up."""
    return ("completed" if (latest_task_status or "").strip() == "Passed"
            else "followup")


def _departure_dict(r: sqlite3.Row) -> dict:
    return {
        "student_id": r["student_id"],
        "course_code": r["course_code"],
        "name": r["name"],
        "student_email": r["student_email"],
        "last_task_status": r["latest_task_status"],
        "last_seen_date": r["collected_date"],
        "momentum": r["momentum"],
        "followup_note": r["followup_note"],
        "followup_date": r["followup_date"],
        "classification": _classify(r["latest_task_status"]),
    }


_PRIOR_ROW_COLS = ("student_id, course_code, name, student_email, "
                   "latest_task_status, collected_date, momentum, "
                   "followup_note, followup_date")


# ----------------------------------------------------------------------
# capture
# ----------------------------------------------------------------------
def record_snapshot(rows, csv_mtime, *, interval_hours: int = 24,
                    db_path=HISTORY_DB, note: str = "",
                    now: Optional[datetime] = None) -> dict:
    """Snapshot ``rows`` (the freshly-loaded caseload) into the history DB.

    Sampling rule (bucketed by ``interval_hours``):
      - no sample in this window yet   -> capture (status 'captured')
      - sample exists, same csv mtime  -> skip    (status 'skipped_stale')
      - sample exists, mtime moved     -> replace this window in place ('updated')

    Computes departures vs the most recent *prior-day* collection (gap-aware,
    always day-grained) before writing. Returns a summary dict; never raises
    (errors come back as ``{"status": "error", "error": ...}``).

    ``now`` is a testability seam so tests can simulate distinct days/windows.
    """
    try:
        now = now or datetime.now()
        today = now.date().isoformat()
        collected_at = now.isoformat(timespec="seconds")
        bucket = _bucket(now, interval_hours)
        csv_mtime_iso = (csv_mtime.isoformat(timespec="seconds")
                         if isinstance(csv_mtime, datetime) else None)

        # Map rows up front so we can bail BEFORE touching the DB if the export
        # can't be keyed (e.g. StudentID column dropped from the view) — an
        # unkeyable empty collection would poison the next departure diff.
        records, incoming_keys = [], set()
        for row in rows:
            rec = _row_to_record(row)
            if rec is None:
                continue
            rec["collected_at"] = collected_at
            rec["collected_date"] = today
            records.append(rec)
            incoming_keys.add((rec["student_id"], rec["course_code"]))
        row_count = len(records)
        if rows and not records:
            return {
                "status": "skipped_no_keys", "row_count": 0,
                "departures": [], "departure_count": 0,
                "warning": ("history snapshot skipped: no rows had a "
                            "StudentID / CourseCode — check your export columns"),
            }

        conn = _connect(db_path)
        try:
            with conn:
                cur = conn.cursor()
                existing = cur.execute(
                    "SELECT collected_at, csv_mtime, row_count "
                    "FROM collections WHERE bucket = ?", (bucket,),
                ).fetchone()
                if existing is not None:
                    if (csv_mtime_iso is not None
                            and existing["csv_mtime"] == csv_mtime_iso):
                        return {
                            "status": "skipped_stale",
                            "collected_at": existing["collected_at"],
                            "row_count": existing["row_count"],
                            "departures": [], "departure_count": 0,
                        }
                    # Fresher export in the same window -> replace this sample.
                    cur.execute("DELETE FROM snapshots WHERE collected_at = ?",
                                (existing["collected_at"],))
                    cur.execute("DELETE FROM collections WHERE collected_at = ?",
                                (existing["collected_at"],))
                    status = "updated"
                else:
                    status = "captured"

                departures, prior_count = _departures_vs_prior_day(
                    cur, today, incoming_keys)
                partial = bool(prior_count
                               and row_count < _PARTIAL_EXPORT_FRACTION * prior_count)
                if partial:
                    departures = []  # truncated export — not real attrition

                # Carry forward the last-known name for any row whose export
                # dropped the Name column (a slimmed list view, or a grid-missed
                # row): writing a blank name would erase a known student's
                # identity from history and from the Departures/Archived views.
                name_cache: dict = {}
                for rec in records:
                    if (rec.get("name") or "").strip():
                        continue
                    key = (rec["student_id"], rec["course_code"])
                    if key not in name_cache:
                        prior = cur.execute(
                            "SELECT name FROM snapshots WHERE student_id = ? "
                            "AND course_code = ? AND name != '' "
                            "ORDER BY collected_at DESC LIMIT 1", key,
                        ).fetchone()
                        name_cache[key] = prior["name"] if prior else ""
                    if name_cache[key]:
                        rec["name"] = name_cache[key]

                cur.execute(
                    "INSERT INTO collections"
                    "(collected_at, collected_date, bucket, csv_mtime, "
                    "row_count, note) VALUES (?, ?, ?, ?, ?, ?)",
                    (collected_at, today, bucket, csv_mtime_iso, row_count, note),
                )
                placeholders = ", ".join(":" + c for c in _SNAP_COLS)
                cur.executemany(
                    f"INSERT OR REPLACE INTO snapshots "
                    f"({', '.join(_SNAP_COLS)}) VALUES ({placeholders})",
                    records,
                )
        finally:
            conn.close()

        return {
            "status": status,
            "collected_at": collected_at,
            "row_count": row_count,
            "departures": departures,
            "departure_count": len(departures),
            "partial_export": partial,
        }
    except Exception as e:  # never break the reload path
        return {"status": "error", "error": str(e)}


def backfill_missing_names(*, db_path=HISTORY_DB) -> int:
    """One-time cleanup: fill any blank ``name`` in ``snapshots`` from that same
    student's most recent non-blank name (e.g. rows captured while a slimmed list
    view dropped the Name column). Returns the number of rows updated. Safe to
    re-run — it only touches rows whose name is still blank."""
    conn = _connect(db_path)
    try:
        with conn:
            blanks = conn.execute(
                "SELECT DISTINCT student_id, course_code FROM snapshots "
                "WHERE name = ''"
            ).fetchall()
            updated = 0
            for b in blanks:
                key = (b["student_id"], b["course_code"])
                prior = conn.execute(
                    "SELECT name FROM snapshots WHERE student_id = ? "
                    "AND course_code = ? AND name != '' "
                    "ORDER BY collected_at DESC LIMIT 1", key,
                ).fetchone()
                if prior and prior["name"]:
                    cur = conn.execute(
                        "UPDATE snapshots SET name = ? WHERE student_id = ? "
                        "AND course_code = ? AND name = ''",
                        (prior["name"], key[0], key[1]),
                    )
                    updated += cur.rowcount
            return updated
    finally:
        conn.close()


# ----------------------------------------------------------------------
# contact notes (scraped SF Notes History) — Phase 1a: persist-on-view
# ----------------------------------------------------------------------
def note_id_from_url(url: str) -> str:
    """The Salesforce record id embedded in a note's anchor href, if any. Handles
    both the classic '.../a16S600000nIdA3IAK' tail and the Lightning
    '.../r/ShortText__c/a16S…AY/view' mid-path form by scanning every path/query
    segment for the one that looks like an SF id (15 or 18 alphanumerics). '' if
    none — the href is often empty here (the cell anchor sits in a shadow root)."""
    if not url:
        return ""
    for seg in str(url).replace("?", "/").replace("&", "/").split("/"):
        if len(seg) in (15, 18) and seg.isalnum():
            return seg
    return ""


def _synthetic_note_id(vals: dict) -> str:
    """Stable content-hash id for a note whose SF record id we couldn't parse
    (empty/opaque href). Keyed on the student + event time + subject + body head,
    which uniquely identifies a note within a student's thread. An EDIT to the
    body yields a new id (can't detect the edit) — acceptable; a parsed record id
    (when present) gives true edit-upsert instead."""
    import hashlib
    raw = "|".join((
        vals.get("contact_id", "") or vals.get("student_id", ""),
        vals.get("created_at", ""), vals.get("subject", ""),
        (vals.get("body", "") or "")[:80],
    ))
    return "syn:" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def classify_note_channel(note_type: str) -> str:
    """Map an SF Type__c to a coarse channel: text / email / call / chatter / note."""
    t = (note_type or "").lower()
    if "text" in t or "instant message" in t or "(im)" in t:
        return "text"
    if "email" in t:
        return "email"
    if "call" in t or "phone" in t:
        return "call"
    if "chatter" in t:
        return "chatter"
    return "note" if t else "other"


def classify_note_direction(note_type: str, body: str) -> str:
    """inbound / outbound / '' for one note.

    Reliable inbound signals: an 'Email from Student' type, or a text body whose
    first direction marker is 'Incoming:' (Mongoose writes Incoming:/Outgoing:).
    Everything email — 'to Student', welcome, Mass Email, cohort invites — defaults
    OUTBOUND (a genuine student email is always typed 'from Student', so an
    unmarked email is one we sent). Unprefixed texts / calls / admin notes stay ''
    (unknown): the multi-sender text-export format needs richer parsing (Phase 1c)."""
    t = (note_type or "").lower()
    if "from student" in t:
        return "inbound"
    b = (body or "").lower()
    i, o = b.find("incoming:"), b.find("outgoing:")
    if i != -1 and (o == -1 or i < o):
        return "inbound"
    if o != -1:
        return "outbound"
    if "email" in t or "welcome" in t:   # unmarked email = we sent it
        return "outbound"
    return ""


def reclassify_notes(*, db_path=HISTORY_DB) -> int:
    """Recompute channel + direction for every stored note from its (type, body)
    — cheap, idempotent, no re-scrape. Run after the classifiers change. Returns
    the number of rows whose channel or direction actually moved."""
    conn = _connect(db_path)
    try:
        changed = 0
        with conn:
            for r in conn.execute(
                    "SELECT note_id, type, body, channel, direction FROM notes"):
                ch = classify_note_channel(r["type"])
                di = classify_note_direction(r["type"], r["body"])
                if ch != r["channel"] or di != r["direction"]:
                    conn.execute(
                        "UPDATE notes SET channel = ?, direction = ? "
                        "WHERE note_id = ?", (ch, di, r["note_id"]))
                    changed += 1
        return changed
    finally:
        conn.close()


def persist_notes(notes, *, student_id: str = "", contact_id: str = "",
                  course_code: str = "", db_path=HISTORY_DB,
                  now: Optional[datetime] = None) -> dict:
    """Upsert scraped SF note-history rows into the ``notes`` table.

    ``notes`` is the list of dicts the browser scrape returns (keys: type,
    course, subject, text, author, date, url). Each row is keyed by its SF note
    record id (parsed from the anchor url); notes are editable-but-never-deleted,
    so a re-scrape UPSERTS — first_seen_at is preserved, the mutable fields and
    last_seen_at refresh. Rows without a usable note id are skipped (can't dedup).
    Never raises; returns {'inserted', 'updated', 'skipped'} (or an error dict)."""
    try:
        now_iso = (now or datetime.now()).isoformat(timespec="seconds")
        ins = upd = skip = 0
        conn = _connect(db_path)
        try:
            with conn:
                for nd in notes or []:
                    body = nd.get("text") or ""
                    ntype = nd.get("type") or ""
                    vals = {
                        "student_id": student_id or "",
                        "contact_id": contact_id or "",
                        "course_code": nd.get("course") or course_code or "",
                        "type": ntype,
                        "channel": classify_note_channel(ntype),
                        "direction": classify_note_direction(ntype, body),
                        "created_at": nd.get("date") or "",
                        "author": nd.get("author") or "",
                        "subject": nd.get("subject") or "",
                        "body": body,
                        "url": nd.get("url") or "",
                    }
                    # Prefer the real SF record id (true edit-upsert); fall back to
                    # a content hash when the href carries no id (common — the
                    # anchor is in a shadow root), so a note is never dropped.
                    nid = note_id_from_url(vals["url"]) or _synthetic_note_id(vals)
                    if not nid or not (vals["created_at"] or body):
                        skip += 1        # nothing to identify or store
                        continue
                    exists = conn.execute(
                        "SELECT 1 FROM notes WHERE note_id = ?", (nid,)).fetchone()
                    if exists:
                        # Don't blank a known student_id/contact_id if this scrape
                        # didn't carry one (COALESCE(NULLIF(...))).
                        conn.execute(
                            "UPDATE notes SET "
                            "student_id = COALESCE(NULLIF(?,''), student_id), "
                            "contact_id = COALESCE(NULLIF(?,''), contact_id), "
                            "course_code = ?, type = ?, channel = ?, direction = ?, "
                            "created_at = ?, author = ?, subject = ?, body = ?, "
                            "url = ?, last_seen_at = ? WHERE note_id = ?",
                            (vals["student_id"], vals["contact_id"],
                             vals["course_code"], vals["type"], vals["channel"],
                             vals["direction"], vals["created_at"], vals["author"],
                             vals["subject"], vals["body"], vals["url"],
                             now_iso, nid))
                        upd += 1
                    else:
                        conn.execute(
                            "INSERT INTO notes (note_id, student_id, contact_id, "
                            "course_code, type, channel, direction, created_at, "
                            "author, subject, body, url, first_seen_at, "
                            "last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (nid, vals["student_id"], vals["contact_id"],
                             vals["course_code"], vals["type"], vals["channel"],
                             vals["direction"], vals["created_at"], vals["author"],
                             vals["subject"], vals["body"], vals["url"],
                             now_iso, now_iso))
                        ins += 1
        finally:
            conn.close()
        return {"inserted": ins, "updated": upd, "skipped": skip}
    except Exception as e:  # never break the notes-view render
        return {"status": "error", "error": str(e)}


_CONTACT_PREFS = ("text", "email", "call")


def set_contact_preference(student_id: str, pref: str, *,
                           db_path=HISTORY_DB, now: Optional[datetime] = None):
    """Manually set a student's contact preference (instructor override, e.g.
    "wants texts only"). ``pref`` in {'text','email','call'}; '' clears the
    override (revert to auto-inferred). Takes precedence over the auto value."""
    pref = (pref or "").strip().lower()
    if pref and pref not in _CONTACT_PREFS:
        raise ValueError(f"pref must be one of {_CONTACT_PREFS} or ''")
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        with conn:
            if pref:
                conn.execute(
                    "INSERT INTO student_flags (student_id, contact_pref, "
                    "updated_at) VALUES (?, ?, ?) ON CONFLICT(student_id) DO "
                    "UPDATE SET contact_pref = excluded.contact_pref, "
                    "updated_at = excluded.updated_at", (student_id, pref, ts))
            else:
                conn.execute("UPDATE student_flags SET contact_pref = '', "
                             "updated_at = ? WHERE student_id = ?", (ts, student_id))
    finally:
        conn.close()


def _manual_contact_pref(student_id: str, *, db_path=HISTORY_DB) -> str:
    conn = _connect(db_path)
    try:
        r = conn.execute("SELECT contact_pref FROM student_flags WHERE "
                         "student_id = ?", (student_id,)).fetchone()
        return (r["contact_pref"] or "") if r else ""
    finally:
        conn.close()


def set_student_pronouns(student_id: str, pronouns: str, *,
                         db_path=HISTORY_DB, now: Optional[datetime] = None):
    """Manually set a student's pronouns (free-form, e.g. 'she/her'); '' clears
    them. The caseload feed carries no gender/pronoun data, so this is purely
    instructor-entered."""
    pronouns = (pronouns or "").strip()
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO student_flags (student_id, pronouns, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(student_id) DO UPDATE SET "
                "pronouns = excluded.pronouns, updated_at = excluded.updated_at",
                (student_id, pronouns, ts))
    finally:
        conn.close()


def student_pronouns(student_id: str, *, db_path=HISTORY_DB) -> str:
    """A student's manually-set pronouns, or '' if none."""
    conn = _connect(db_path)
    try:
        r = conn.execute("SELECT pronouns FROM student_flags WHERE "
                         "student_id = ?", (student_id,)).fetchone()
        return (r["pronouns"] or "") if r else ""
    finally:
        conn.close()


def all_student_pronouns(*, db_path=HISTORY_DB) -> dict:
    """{student_id -> pronouns} for every student with a non-blank manual value.
    One query so the caseload derived-columns pass can fill the column without a
    per-row lookup."""
    conn = _connect(db_path)
    try:
        return {r["student_id"]: r["pronouns"] for r in conn.execute(
            "SELECT student_id, pronouns FROM student_flags "
            "WHERE pronouns IS NOT NULL AND pronouns != ''")}
    finally:
        conn.close()


def contact_preference(student_id: str, *, db_path=HISTORY_DB,
                       _ledger=None) -> dict:
    """A student's contact preference: the channel they actually engage on.
    Manual instructor override wins; otherwise auto-inferred from the note
    history (the channel with the most INBOUND events — text/email/call — since
    an inbound is the student choosing that channel). Returns
    {pref: 'text'|'email'|'call'|'', source: 'manual'|'auto'|'none'}."""
    manual = _manual_contact_pref(student_id, db_path=db_path)
    if manual:
        return {"pref": manual, "source": "manual"}
    led = _ledger if _ledger is not None else engagement_ledger(
        student_id, db_path=db_path)
    # inbound counts per channel; calls count as engagement on the call channel.
    scores = {}
    for c, info in led.get("channels", {}).items():
        inbound = info.get("in", 0) + (info.get("out", 0) if c == "call" else 0)
        if inbound:
            scores[c] = inbound
    if not scores:
        return {"pref": "", "source": "none"}
    # most-engaged channel; tie-break by lower cost (text < email < call).
    best = max(scores, key=lambda c: (scores[c], -_CHANNEL_COST.get(c, 9)))
    return {"pref": best, "source": "auto"}


def notes_count(*, db_path=HISTORY_DB) -> int:
    """Total stored contact-note rows (Phase 1a accumulator size)."""
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()


def notes_last_stored(*, db_path=HISTORY_DB) -> dict:
    """{contact_id -> newest stored note created_at (ISO str)} — the high-water
    mark per student, used to change-gate the sweep (Phase 1b)."""
    conn = _connect(db_path)
    try:
        return {r["contact_id"]: r["newest"] for r in conn.execute(
            "SELECT contact_id, MAX(created_at) AS newest FROM notes "
            "WHERE contact_id != '' GROUP BY contact_id") if r["newest"]}
    finally:
        conn.close()


def notes_freshness(*, db_path=HISTORY_DB) -> dict:
    """Contact-note freshness for the Settings status line:
    ``{'count', 'last_swept', 'newest_note'}``. ``last_swept`` = when a sweep last
    captured anything (max last_seen_at); ``newest_note`` = the most recent note's
    authored date we hold (max created_at). Both ISO strings or None."""
    conn = _connect(db_path)
    try:
        r = conn.execute(
            "SELECT COUNT(*) c, MAX(last_seen_at) ls, MAX(created_at) nc "
            "FROM notes").fetchone()
        return {"count": r["c"] or 0, "last_swept": r["ls"] or None,
                "newest_note": r["nc"] or None}
    finally:
        conn.close()


def departed_students_for_sweep(*, db_path=HISTORY_DB) -> list:
    """Resolved (departed) students for a note sweep — one row per student_id,
    with their stored Contact id (from the contact_ids map) when known, else ''
    (the sweep then falls back to a Student-ID search). ``last_contact`` is left
    blank so the change-gate keeps only students we have NO notes for yet: a
    departed student's thread is static, so once captured it never needs
    re-scraping."""
    conn = _connect(db_path)
    try:
        # contact_ids is owned by src/mongoose_contacts (same DB); tolerate its
        # absence — a missing id just means the sweep searches by Student ID.
        try:
            cids = {r["student_id"]: r["contact_id"] for r in conn.execute(
                "SELECT student_id, contact_id FROM contact_ids "
                "WHERE contact_id != ''")}
        except sqlite3.OperationalError:
            cids = {}
        seen, out = set(), []
        for r in conn.execute(
                "SELECT student_id, name FROM outcomes ORDER BY last_ingest_at DESC"):
            sid = r["student_id"]
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append({"student_id": sid, "contact_id": cids.get(sid, ""),
                        "name": r["name"] or sid, "last_contact": ""})
        return out
    finally:
        conn.close()


def students_needing_note_sweep(students, *, db_path=HISTORY_DB,
                                _stored=None) -> list:
    """Change-gate a note sweep: from ``students`` (dicts with at least
    ``contact_id`` and ``last_contact`` — the newest caseload contact timestamp,
    ISO), keep only those whose last contact is NEWER than the newest note we've
    already stored for them (or who have no stored notes / no contact id). ISO
    timestamps compare correctly as strings. This makes a repeat sweep cheap —
    only students actually contacted since the last sweep are re-scraped.

    ``_stored`` (a pre-fetched notes_last_stored dict) is a seam for tests / to
    avoid re-querying when the caller already has it."""
    stored = notes_last_stored(db_path=db_path) if _stored is None else _stored
    # student_id-keyed high-water for rows lacking a contact id (departed
    # students often have no stored contact id — gate them by student_id so a
    # re-run skips ones we've already captured).
    conn = _connect(db_path)
    try:
        by_sid = {r["student_id"]: r["newest"] for r in conn.execute(
            "SELECT student_id, MAX(created_at) AS newest FROM notes "
            "WHERE student_id != '' GROUP BY student_id") if r["newest"]}
    finally:
        conn.close()
    out = []
    for s in students:
        cid = (s.get("contact_id") or "").strip()
        sid = (s.get("student_id") or "").strip()
        newest = stored.get(cid) if cid else by_sid.get(sid)
        last_contact = (s.get("last_contact") or "").strip()
        if newest is None or (last_contact and last_contact > newest):
            out.append(s)
    return out


# ----------------------------------------------------------------------
# engagement ledger — per-student channel strategy (what's tried / what works)
# ----------------------------------------------------------------------
# Contact channels by escalation COST (low → high): a boilerplate text is cheap,
# a personal email costs more, a cold call is the last resort. 'note'/'chatter'
# are internal, not outreach — excluded from the ledger.
_CHANNEL_COST = {"text": 1, "email": 2, "call": 3}
_CHANNEL_ORDER = ("text", "email", "call")


def engagement_ledger(student_id: str, *, db_path=HISTORY_DB,
                      now: Optional[datetime] = None) -> dict:
    """Per-student contact strategy from the note history: for each channel
    (text/email/call) how many outbound/inbound and when, whether the student
    RESPONDS on it, and a cost-aware suggestion for what to try next — prefer the
    cheapest channel they answer; if they've never answered, escalate to the next
    untried tier (don't cold-call until cheaper channels are exhausted). Returns
    {student_id, channels:{ch:{out,in,last_out,last_in,responds,response_rate}},
    responds_to:[…], tried:[…], last_inbound, suggested_next, reason}."""
    from collections import defaultdict
    today = (now or datetime.now()).date()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT channel, direction, created_at FROM notes "
            "WHERE student_id = ? AND created_at != ''", (student_id,)).fetchall()
    finally:
        conn.close()

    ch = defaultdict(lambda: {"out": 0, "in": 0, "last_out": "", "last_in": ""})
    for r in rows:
        c = r["channel"]
        if c not in _CHANNEL_COST:
            continue
        t = r["created_at"]
        if r["direction"] == "outbound":
            ch[c]["out"] += 1
            ch[c]["last_out"] = max(ch[c]["last_out"], t)
        elif r["direction"] == "inbound":
            ch[c]["in"] += 1
            ch[c]["last_in"] = max(ch[c]["last_in"], t)

    channels = {}
    for c, info in ch.items():
        channels[c] = dict(
            info, responds=info["in"] > 0,
            response_rate=(info["in"] / info["out"]) if info["out"] else None)
    responds_to = sorted((c for c in channels if channels[c]["responds"]),
                         key=lambda c: _CHANNEL_COST[c])
    tried = sorted((c for c in channels if channels[c]["out"] > 0),
                   key=lambda c: _CHANNEL_COST[c])
    last_inbound = max((channels[c]["last_in"] for c in channels
                        if channels[c]["last_in"]), default="")

    if responds_to:
        nxt = responds_to[0]                       # cheapest channel they answer
        reason = f"replies on {nxt} — use it"
    else:
        untried = [c for c in _CHANNEL_ORDER if c not in tried]
        if not tried:
            nxt, reason = "text", "no contact yet — start low-cost (text)"
        elif untried:
            nxt = untried[0]
            reason = (f"no reply to {'/'.join(tried)} — escalate to {nxt}")
        else:
            nxt = "call"
            reason = "tried every channel, no reply — call is the last resort"

    return {
        "student_id": student_id, "channels": channels,
        "responds_to": responds_to, "tried": tried,
        "last_inbound": last_inbound, "suggested_next": nxt, "reason": reason,
    }


def _departures_vs_prior_day(cur: sqlite3.Cursor, today: str,
                             incoming_keys: set):
    """(departures, prior_row_count) comparing the most recent collection from
    a calendar day BEFORE ``today`` against ``incoming_keys``. ([], 0) if the
    DB has no earlier-day collection yet."""
    prior = cur.execute(
        "SELECT collected_at, row_count FROM collections "
        "WHERE collected_date < ? ORDER BY collected_at DESC LIMIT 1",
        (today,),
    ).fetchone()
    if prior is None:
        return [], 0
    prior_rows = cur.execute(
        f"SELECT {_PRIOR_ROW_COLS} FROM snapshots WHERE collected_at = ?",
        (prior["collected_at"],),
    ).fetchall()
    deps = [_departure_dict(r) for r in prior_rows
            if (r["student_id"], r["course_code"]) not in incoming_keys]
    return deps, prior["row_count"]


# ----------------------------------------------------------------------
# queries
# ----------------------------------------------------------------------
def find_departures(*, db_path=HISTORY_DB) -> list[dict]:
    """Students present in the most recent prior-day collection but absent in
    the latest one, classified completed/followup. [] if there's no earlier
    day, or if the latest looks like a truncated export (partial-export guard)."""
    conn = _connect(db_path)
    try:
        latest = conn.execute(
            "SELECT collected_at, collected_date, row_count FROM collections "
            "ORDER BY collected_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return []
        prior = conn.execute(
            "SELECT collected_at, row_count FROM collections "
            "WHERE collected_date < ? ORDER BY collected_at DESC LIMIT 1",
            (latest["collected_date"],),
        ).fetchone()
        if prior is None:
            return []
        if prior["row_count"] and latest["row_count"] < (
                _PARTIAL_EXPORT_FRACTION * prior["row_count"]):
            return []
        prior_rows = conn.execute(
            f"SELECT {_PRIOR_ROW_COLS} FROM snapshots WHERE collected_at = ?",
            (prior["collected_at"],),
        ).fetchall()
        latest_keys = {
            (r["student_id"], r["course_code"]) for r in conn.execute(
                "SELECT student_id, course_code FROM snapshots "
                "WHERE collected_at = ?", (latest["collected_at"],),
            ).fetchall()
        }
        return [_departure_dict(r) for r in prior_rows
                if (r["student_id"], r["course_code"]) not in latest_keys]
    finally:
        conn.close()


def _snapshot_to_viewer_row(r: sqlite3.Row) -> dict:
    """Rebuild a caseload-style row (CSV-header keys, like app._caseload_rows)
    from one snapshots row: the ~100 non-core columns in extra_json, overlaid
    with the core fields under their canonical CSV headers."""
    try:
        extra = json.loads(r["extra_json"] or "{}")
    except Exception:
        extra = {}
    row = dict(extra) if isinstance(extra, dict) else {}
    for snap_col, csv_header in _SNAP_TO_CSV.items():
        row[csv_header] = r[snap_col] or ""
    return row


def archived_students(current_keys=None, *, db_path=HISTORY_DB) -> list[dict]:
    """Every student we've ever snapshotted who is no longer on the caseload —
    as viewer-ready rows (CSV-header keys), each rebuilt from that student's
    LAST-KNOWN snapshot. Enriched with the pass outcome (ArchivedOutcome /
    'Passed <date>') when the passed-archive has it, else inferred from the last
    task status. `current_keys` is a set of (student_id, course_code) to treat
    as still-on-caseload and exclude; when None the latest snapshot collection
    is used as 'current'. Newest departures first."""
    conn = _connect(db_path)
    try:
        if current_keys is None:
            latest = conn.execute(
                "SELECT collected_at FROM collections "
                "ORDER BY collected_at DESC LIMIT 1").fetchone()
            current = set()
            if latest is not None:
                current = {
                    (r["student_id"], r["course_code"]) for r in conn.execute(
                        "SELECT student_id, course_code FROM snapshots "
                        "WHERE collected_at = ?", (latest["collected_at"],))}
        else:
            current = {(str(a), str(b)) for (a, b) in current_keys}

        # Last-known snapshot per (student, course).
        rows = conn.execute(
            "SELECT s.* FROM snapshots s JOIN ("
            "  SELECT student_id, course_code, MAX(collected_at) AS mx "
            "  FROM snapshots GROUP BY student_id, course_code) L "
            "ON s.student_id = L.student_id AND s.course_code = L.course_code "
            "AND s.collected_at = L.mx").fetchall()
        outcomes = {
            (r["student_id"], r["course_code"]): r for r in conn.execute(
                "SELECT student_id, course_code, outcome, pass_date "
                "FROM outcomes")}

        result = []
        for r in rows:
            key = (r["student_id"], r["course_code"])
            if key in current:
                continue
            row = _snapshot_to_viewer_row(r)
            oc = outcomes.get(key)
            passed = bool(oc and (oc["outcome"] or "").strip() == "passed")
            row["ArchivedLastSeen"] = r["collected_date"] or ""
            if passed:
                row["ArchivedOutcome"] = (
                    "Passed " + (oc["pass_date"] or "")).strip()
            elif (r["latest_task_status"] or "").strip() == "Passed":
                row["ArchivedOutcome"] = "Passed"
            else:
                row["ArchivedOutcome"] = "Left"
            row["_archived"] = True
            result.append(row)
        result.sort(key=lambda d: d.get("ArchivedLastSeen", ""), reverse=True)
        return result
    finally:
        conn.close()


# ----------------------------------------------------------------------
# outcomes ingest (passed-in-last-30-days archive)
# ----------------------------------------------------------------------
def _val(row: dict, csv_header: str) -> str:
    """First non-empty value among a header's API + display-name spellings,
    stripped; '' if absent. Same robustness as ``_get`` but for any header
    (not just the precomputed core set)."""
    for h in _candidate_headers(csv_header):
        v = row.get(h)
        if isinstance(v, str):
            v = v.strip()
        if v:
            return v
    return ""


# Archive columns we pull into typed outcome fields; everything else is kept
# verbatim in extra_json (so the full ~106-column row is never lost).
_OUTCOME_SRC = [
    "StudentID", "CourseCode", "Name", "StudentEmail", "Momentum",
    "ActualEndDate", "Icenddate", "TermEndDate", "CourseStartDate",
    "TermStartDate", "LatestTaskStatus", "CourseStatus", "OtherCourses",
]

# The archive's CourseStatus column resolves each departed student. Normalize
# to 'passed' / 'not_passed'; an unrecognized value (e.g. a column-shifted row
# where a program name lands in this field) returns None and the row is skipped.
_OUTCOME_STATUS = {
    "passed": "passed",
    "not passed": "not_passed",
    "unenrolled": "not_passed",
    "term end": "not_passed",
    "withdrawn": "not_passed",
    "dropped": "not_passed",
    "failed": "not_passed",
}


def _norm_outcome(course_status: str) -> Optional[str]:
    return _OUTCOME_STATUS.get((course_status or "").strip().lower())


def count_other_courses(raw: str, course_code: str) -> int:
    """Distinct courses in an OtherCourses list OTHER than ``course_code``. 0 if
    blank or the list only echoes the current course. The OtherCourses field
    enumerates every course the student is enrolled in (including this one), so
    'other' = the rest. WGU advises students juggling multiple courses to finish
    the others first, so a non-pass here may be deliberate deprioritization
    rather than a Momentum miss — which is why we track it for calibration."""
    if not raw:
        return 0
    cur = (course_code or "").strip().upper()
    seen = set()
    for tok in str(raw).replace(";", ",").split(","):
        c = tok.strip().upper()
        if c and c != cur:
            seen.add(c)
    return len(seen)
_OUTCOME_ALIASES = {a for h in _OUTCOME_SRC for a in _candidate_headers(h)}

_OUTCOME_COLS = [
    "student_id", "course_code", "name", "student_email",
    "momentum_at_outcome", "momentum_rank_at_outcome", "latest_task_status",
    "outcome", "pass_date", "ic_end_date", "term_end_date",
    "course_start_date", "term_start_date", "other_courses",
    "other_course_count", "entry_momentum", "entry_momentum_rank",
    "entry_captured", "first_seen_at", "last_ingest_at",
    "source_file", "extra_json",
]

# A snapshot within this many days of CourseStart counts as a genuine
# at-enrollment Momentum reading (vs a mid-course, already-adjusted proxy).
_ENTRY_WINDOW_DAYS = 21


def _entry_reading_for(snap_index: dict, key, course_start, window=_ENTRY_WINDOW_DAYS):
    """(momentum, rank, captured) for the snapshot nearest ``course_start`` among
    this student's history. ``captured`` is True only when that nearest snapshot
    is within ``window`` days of the start — i.e. we actually observed them at
    enrollment, so the reading is a fair entry prediction rather than a
    mid-course (self-corrected) proxy. ('', None, False) if we have no usable
    snapshot for them."""
    seq = snap_index.get(key)
    if not seq or course_start is None:
        return ("", None, False)
    best, best_gap = None, None
    for d, mom, rank in seq:
        dt = _parse_date(d)
        if dt is None:
            continue
        gap = abs((dt - course_start).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = (mom, rank), gap
    if best is None:
        return ("", None, False)
    return (best[0] or "", best[1], best_gap <= window)

# UPSERT: refresh every field on conflict EXCEPT first_seen_at, so we keep the
# timestamp at which an outcome first entered our records.
_OUTCOME_UPSERT = (
    f"INSERT INTO outcomes ({', '.join(_OUTCOME_COLS)}) "
    f"VALUES ({', '.join(':' + c for c in _OUTCOME_COLS)}) "
    "ON CONFLICT(student_id, course_code) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in _OUTCOME_COLS
                if c not in ("student_id", "course_code", "first_seen_at"))
)


def ingest_outcomes_csv(path, *, db_path=HISTORY_DB,
                        now: Optional[datetime] = None) -> dict:
    """Ingest a WGU "passed in the last 30 days" archive CSV into ``outcomes``.

    Upserts one row per (student, course): a re-downloaded archive refreshes
    existing rows (preserving ``first_seen_at``) and adds newly-passed students.
    Because the archive is a rolling 30-day window, downloading at least every
    ~30 days guarantees no passer is missed.

    Returns a summary dict (``status`` 'ok' | 'empty' | 'error'); never raises.
    """
    try:
        now = now or datetime.now()
        ts = now.isoformat(timespec="seconds")
        try:
            archive_mtime = datetime.fromtimestamp(
                Path(path).stat().st_mtime).isoformat(timespec="seconds")
        except Exception:
            archive_mtime = ts
        records = []
        skipped = 0
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sid, cc = _val(row, "StudentID"), _val(row, "CourseCode")
                if not sid or not cc:
                    continue
                status = _norm_outcome(_val(row, "CourseStatus"))
                if status is None:
                    skipped += 1   # blank/unrecognized status (e.g. shifted row)
                    continue
                mom = _val(row, "Momentum")
                extra = {k: v for k, v in row.items()
                         if k and k not in _OUTCOME_ALIASES}
                records.append({
                    "student_id": sid, "course_code": cc,
                    "name": _val(row, "Name"),
                    "student_email": _val(row, "StudentEmail"),
                    "momentum_at_outcome": mom,
                    "momentum_rank_at_outcome": momentum_rank(mom),
                    "latest_task_status": _val(row, "LatestTaskStatus"),
                    "outcome": status,
                    "pass_date": _val(row, "ActualEndDate"),
                    "ic_end_date": _val(row, "Icenddate"),
                    "term_end_date": _val(row, "TermEndDate"),
                    "course_start_date": _val(row, "CourseStartDate"),
                    "term_start_date": _val(row, "TermStartDate"),
                    "other_courses": _val(row, "OtherCourses"),
                    "other_course_count": count_other_courses(
                        _val(row, "OtherCourses"), cc),
                    # Filled from the snapshot index inside the txn below.
                    "entry_momentum": "", "entry_momentum_rank": None,
                    "entry_captured": 0,
                    "first_seen_at": ts, "last_ingest_at": ts,
                    "source_file": Path(path).name,
                    "extra_json": json.dumps(extra, ensure_ascii=False),
                })
        if not records:
            return {"status": "empty", "ingested": 0, "new": 0, "updated": 0,
                    "skipped": skipped, "file": Path(path).name}
        n_passed = sum(1 for r in records if r["outcome"] == "passed")
        n_not_passed = len(records) - n_passed

        conn = _connect(db_path)
        try:
            with conn:
                # Freeze each student's ENTRY Momentum (reading nearest their
                # CourseStart) onto the outcome — the only fair basis for a
                # performance-vs-prediction assessment, captured durably now so
                # it survives even if snapshots are later pruned.
                snap_index: dict = {}
                for r in conn.execute(
                        "SELECT student_id, course_code, collected_date, "
                        "momentum, momentum_rank FROM snapshots "
                        "ORDER BY collected_date ASC"):
                    snap_index.setdefault(
                        (r["student_id"], r["course_code"]), []).append(
                            (r["collected_date"], r["momentum"],
                             r["momentum_rank"]))
                for rec in records:
                    mom, rank, captured = _entry_reading_for(
                        snap_index, (rec["student_id"], rec["course_code"]),
                        _parse_date(rec["course_start_date"]))
                    rec["entry_momentum"] = mom
                    rec["entry_momentum_rank"] = rank
                    rec["entry_captured"] = 1 if captured else 0

                existing = {(r["student_id"], r["course_code"]) for r in
                            conn.execute("SELECT student_id, course_code "
                                         "FROM outcomes")}
                new = sum(1 for r in records
                          if (r["student_id"], r["course_code"]) not in existing)
                conn.executemany(_OUTCOME_UPSERT, records)
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES('outcomes_last_ingest_at', ?)", (ts,))
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES('outcomes_last_ingest_file', ?)",
                             (Path(path).name,))
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES('outcomes_last_ingest_count', ?)",
                             (str(len(records)),))
                # The archive file's mtime = when the user downloaded it. That,
                # not our ingest run time, is what the staleness reminder keys
                # off — re-ingesting an old file shouldn't reset the clock.
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES('outcomes_archive_mtime', ?)",
                             (archive_mtime,))
                total = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        finally:
            conn.close()
        return {"status": "ok", "ingested": len(records), "new": new,
                "updated": len(records) - new, "total_outcomes": total,
                "passed": n_passed, "not_passed": n_not_passed,
                "skipped": skipped, "file": Path(path).name}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def last_outcomes_ingest(*, db_path=HISTORY_DB) -> Optional[datetime]:
    """Timestamp of the most recent outcomes-archive ingest, or None if never."""
    conn = _connect(db_path)
    try:
        v = _meta_get(conn, "outcomes_last_ingest_at")
    finally:
        conn.close()
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except Exception:
        return None


def outcomes_archive_mtime(*, db_path=HISTORY_DB) -> Optional[datetime]:
    """Download time (file mtime) of the most recently ingested archive, or
    None if never ingested. This is the age of the *data*, which is what the
    staleness reminder should reflect."""
    conn = _connect(db_path)
    try:
        v = (_meta_get(conn, "outcomes_archive_mtime")
             or _meta_get(conn, "outcomes_last_ingest_at"))
    finally:
        conn.close()
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except Exception:
        return None


def outcomes_stale_days(*, db_path=HISTORY_DB,
                        now: Optional[datetime] = None) -> Optional[int]:
    """Whole days since the most recently ingested archive was *downloaded*
    (file mtime), or None if never ingested. The caller decides what to do at
    the never/stale thresholds. Keyed off download time so re-ingesting an old
    file doesn't falsely reset the reminder."""
    mt = outcomes_archive_mtime(db_path=db_path)
    if mt is None:
        return None
    return ((now or datetime.now()) - mt).days


def outcomes_count(*, db_path=HISTORY_DB) -> int:
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    finally:
        conn.close()


def outcomes_entry_coverage(*, db_path=HISTORY_DB) -> dict:
    """How many resolved outcomes we can FAIRLY assess — i.e. for which we
    froze a genuine at-entry Momentum reading (``entry_captured``). The rest
    started before we were tracking them, so only their self-corrected later
    Momentum is known. Returns {total, captured}."""
    conn = _connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        captured = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE entry_captured = 1"
        ).fetchone()[0]
        # Drift: of students with both an entry and an exit reading, how many
        # had Momentum CHANGE — the evidence that exit Momentum can't fairly
        # measure performance against the prediction.
        both = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE entry_captured = 1 "
            "AND entry_momentum_rank IS NOT NULL "
            "AND momentum_rank_at_outcome IS NOT NULL").fetchone()[0]
        drifted = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE entry_captured = 1 "
            "AND entry_momentum_rank IS NOT NULL "
            "AND momentum_rank_at_outcome IS NOT NULL "
            "AND entry_momentum_rank != momentum_rank_at_outcome").fetchone()[0]
    finally:
        conn.close()
    return {"total": total, "captured": captured,
            "both": both, "drifted": drifted}


def all_outcomes(*, db_path=HISTORY_DB) -> list[dict]:
    """Every recorded outcome (for the calibration report). One row per
    (student, course)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM outcomes ORDER BY course_code, student_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _effective_end_from_extra(ej: dict):
    """Effective end date from a snapshot's extra_json: the IC (incomplete /
    extension) date when present, else the term end. Parsed date or None."""
    return _parse_date((ej.get("Icenddate") or "").strip()
                       or (ej.get("TermEndDate") or "").strip())


def term_end_outcomes(*, db_path=HISTORY_DB, date_from=None, date_to=None,
                      courses=None, depart_cutoff_days=14, term_grace_days=21,
                      now: Optional[datetime] = None) -> dict:
    """Course pass / non-pass resolution built from the SNAPSHOT history rather
    than the "passed in the last 30 days" archive.

    WHY snapshots, not the archive: the archive captures passers well but badly
    under-captures non-passers (they don't cleanly "resolve" — most get an IC
    extension, roll to a new term, or just term-end quietly and stay on the
    caseload), so an archive-only pass rate is inflated. The snapshot history
    keeps every student we ever saw, so it can classify the non-passers the
    archive drops.

    Each (student, course) is classified and DATED BY WHEN IT RESOLVED:

      passed            authoritative pass on record (outcomes); dated pass_date.
      notpass_term_end  reached their effective end (IC else term end) without a
                        pass on record; dated by that effective end.
      early_exit        fell off the caseload well before their term end
                        (>``term_grace_days`` early), or an archived non-pass
                        whose term end is still in the future; dated by last-seen.
                        AMBIGUOUS — this can be a true dropout, a reassignment to
                        another CI, or a pass we simply failed to capture. It is
                        reported SEPARATELY and kept OUT of the headline term-end
                        rate, because snapshots alone can't tell those apart and
                        folding them in would over-count non-passers.
      in_progress       still enrolled, term end not yet reached; excluded.

    ``date_from``/``date_to`` bound the window by RESOLUTION date. ``courses``
    (a set/list) restricts the cohort; None = all. The two tuning knobs let a
    caller widen/narrow what counts as "reached term end" vs "left early".

    Returns ``{asof, date_from, date_to, passed, notpass_term_end, early_exit,
    in_progress_excluded, term_end_rate, rate_incl_early, students:{...},
    courses:[…]}``. ``students`` holds per-category detail dicts (student_id,
    course_code, name, resolved_on, effective_end, last_seen, momentum,
    latest_task_status) — the caller owns any PII handling.
    """
    from datetime import timedelta
    lo = _parse_date(date_from) if date_from else None
    hi = _parse_date(date_to) if date_to else None
    sel = set(courses) if courses else None
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT MAX(collected_date) m FROM snapshots").fetchone()
        asof = _parse_date(row["m"]) if row and row["m"] else (
            (now or datetime.now()).date())
        oc = {}
        for r in conn.execute("SELECT student_id, course_code, outcome, "
                              "pass_date, name FROM outcomes"):
            oc[(r["student_id"], r["course_code"])] = dict(r)
        latest = {}
        for r in conn.execute(
                "SELECT s.student_id, s.course_code, s.name, s.momentum, "
                "s.latest_task_status, s.collected_date, s.extra_json "
                "FROM snapshots s JOIN (SELECT student_id, course_code, "
                "MAX(collected_date) md FROM snapshots GROUP BY student_id, "
                "course_code) m ON s.student_id = m.student_id AND "
                "s.course_code = m.course_code AND s.collected_date = m.md"):
            try:
                ej = json.loads(r["extra_json"] or "{}")
            except Exception:
                ej = {}
            latest[(r["student_id"], r["course_code"])] = {
                "name": r["name"], "momentum": r["momentum"],
                "latest_task_status": r["latest_task_status"],
                "last_seen": _parse_date(r["collected_date"]),
                "effective_end": _effective_end_from_extra(ej)}
    finally:
        conn.close()

    def _classify(key):
        o = oc.get(key)
        snap = latest.get(key, {})
        eff = snap.get("effective_end")
        ls = snap.get("last_seen")
        departed = ls is not None and (asof - ls).days > depart_cutoff_days
        if o and o["outcome"] == "passed":
            return "passed", _parse_date(o.get("pass_date"))
        if o and o["outcome"] == "not_passed":
            if eff and ls and ls < eff - timedelta(days=term_grace_days):
                return "early_exit", ls
            return "notpass_term_end", (eff if eff and eff <= asof else ls)
        if eff and eff <= asof:
            if ls is not None and ls < eff - timedelta(days=term_grace_days):
                return "early_exit", ls
            return "notpass_term_end", eff
        if departed:
            return "early_exit", ls
        return "in_progress", None

    def _detail(key, resolved_on):
        snap = latest.get(key, {})
        o = oc.get(key, {})
        return {
            "student_id": key[0], "course_code": key[1],
            "name": snap.get("name") or o.get("name") or "",
            "resolved_on": resolved_on.isoformat() if resolved_on else "",
            "effective_end": (snap.get("effective_end").isoformat()
                              if snap.get("effective_end") else ""),
            "last_seen": (snap.get("last_seen").isoformat()
                          if snap.get("last_seen") else ""),
            "momentum": snap.get("momentum") or "",
            "latest_task_status": snap.get("latest_task_status") or ""}

    buckets = {"passed": [], "notpass_term_end": [], "early_exit": []}
    in_progress = 0
    present_courses = set()
    for key in set(latest) | set(oc):
        if sel is not None and key[1] not in sel:
            continue
        cat, resolved_on = _classify(key)
        if cat == "in_progress":
            in_progress += 1
            continue
        if resolved_on is None:
            continue
        if (lo is not None and resolved_on < lo) or (
                hi is not None and resolved_on > hi):
            continue
        buckets[cat].append(_detail(key, resolved_on))
        present_courses.add(key[1])

    n_pass = len(buckets["passed"])
    n_nt = len(buckets["notpass_term_end"])
    n_ex = len(buckets["early_exit"])
    term_rate = (100.0 * n_pass / (n_pass + n_nt)) if (n_pass + n_nt) else None
    rate_incl = (100.0 * n_pass / (n_pass + n_nt + n_ex)) if (
        n_pass + n_nt + n_ex) else None
    for cat in buckets:
        buckets[cat].sort(key=lambda d: (d["course_code"], d["resolved_on"]))
    return {
        "asof": asof.isoformat(),
        "date_from": lo.isoformat() if lo else None,
        "date_to": hi.isoformat() if hi else None,
        "passed": n_pass, "notpass_term_end": n_nt, "early_exit": n_ex,
        "in_progress_excluded": in_progress,
        "term_end_rate": term_rate, "rate_incl_early": rate_incl,
        "students": buckets, "courses": sorted(present_courses)}


def _to_int(x):
    try:
        return int(float(x))
    except Exception:
        return None


# Enrolled at least this long WITHOUT attempting a task = a real stall worth
# chasing (FINDINGS §8/§15: fresh non-attempters self-start; the aged ones are
# the risk). Weeks, from `weeksincourse` (or course-start age).
CHASE_MIN_WEEKS = 6


def _has_attempted(latest_task_status: str, e: dict) -> bool:
    """Has the student engaged ANY task (submitted/passed/returned)? For C769 the
    causal risk is never-attempting (attempting ≈ passing, FINDINGS §12)."""
    if (latest_task_status or "").strip():
        return True
    if _ej_get(e, "LatestTaskStatus") or _ej_get(e, "LatestTaskDate"):
        return True
    if (_to_int(_ej_get(e, "LatestTaskAttempts")) or 0) > 0:
        return True
    for n in (1, 2, 3):
        if (e.get(f"Task{n}Status") or "").strip():
            return True
    return False


def at_risk_students(*, db_path=HISTORY_DB, now: Optional[datetime] = None,
                     min_weeks: int = CHASE_MIN_WEEKS) -> list[dict]:
    """The CHASE LIST — the reachable students who most need a nudge to attempt
    and aren't getting one (FINDINGS §12/§15). Gates on the current caseload:

      1. **course underway** — CourseStatus not 'Planned', start in the past;
      2. **never attempted a task** — the causal risk (attempt ≈ pass, §12);
      3. **enrolled ≥ ``min_weeks``** — a real stall, not a fresh student who'll
         self-start (§8/§15 time-weighting). Measured from **CourseStartDate**
         (time in THIS registration), NOT the ``weeksincourse`` field, which
         counts tenure across prior terms and wrongly flags freshly term-rolled
         students as long stalls;
      4. **not already resolved** (passed / not-passed in outcomes).

    Each row is an actionable worklist entry (see _actionable_fields) plus
    ``weeks_enrolled`` / ``days_into_course``. Sorted by urgency: soonest
    deadline, then longest-enrolled, then longest since contact (most-neglected).
    NOTE: superseded in the UI by momentum_risk_students(); retained as the
    never-attempted stall query."""
    today = (now or datetime.now()).date()
    conn = _connect(db_path)
    try:
        latest = conn.execute("SELECT collected_at FROM collections "
                              "ORDER BY collected_at DESC LIMIT 1").fetchone()
        if latest is None:
            return []
        rows = conn.execute("SELECT * FROM snapshots WHERE collected_at = ?",
                            (latest["collected_at"],)).fetchall()
        resolved = {(r["student_id"], r["course_code"]) for r in
                    conn.execute("SELECT student_id, course_code FROM outcomes")}
    finally:
        conn.close()

    out = []
    for r in rows:
        key = (r["student_id"], r["course_code"])
        if key in resolved:
            continue
        try:
            e = json.loads(r["extra_json"] or "{}")
        except Exception:
            e = {}
        status = _ej_get(e, "CourseStatus")
        start = _parse_date(_ej_get(e, "CourseStartDate"))
        days_into = (today - start).days if start else None
        # gate 1: underway
        if status == "Planned" or days_into is None or days_into <= 0:
            continue
        # gate 2: never attempted a task
        if _has_attempted(r["latest_task_status"], e):
            continue
        # gate 3: enrolled long enough (in THIS registration) to be a real stall.
        # Use CourseStartDate, not weeksincourse (which counts prior terms).
        weeks = days_into // 7
        if weeks < min_weeks:
            continue

        row = _actionable_fields(r, e, db_path=db_path)
        row["weeks_enrolled"] = weeks
        row["days_into_course"] = days_into
        out.append(row)
    out.sort(key=lambda a: (
        a["term_days_left"] if a["term_days_left"] is not None else 1 << 30,
        -(a["weeks_enrolled"] or 0), -(a["days_since_contact"] or 0)))
    return out


# ----------------------------------------------------------------------
# chase-list worklist (persistent membership + instructor status)
# ----------------------------------------------------------------------
# at_risk_students() recomputes the chase list live on every call. The worklist
# layer persists it: who is on it and SINCE WHEN (first_listed_at), when they
# last still qualified (last_listed_at), when they dropped off (resolved_at —
# they attempted a task or the outcome resolved), and the instructor's manual
# status (contacted / dismissed). That turns a volatile view into a durable
# list you can work down over days.
_CHASE_STATUSES = {"contacted", "dismissed"}


def sync_chase_list(live: list[dict], *, db_path=HISTORY_DB,
                    now: Optional[datetime] = None) -> list[dict]:
    """Reconcile a freshly-computed worklist (``live`` — the rows from
    momentum_risk_students / at_risk_students) against the persistent
    ``chase_list`` ledger, then return those rows enriched IN PLACE with the
    persisted fields (``first_listed_at``, ``days_on_list``, ``chase_status``,
    ``status_at``).

    Reconciliation in one pass:
      * a student newly on the list is INSERTed (first_listed_at = now);
      * one still on the list has last_listed_at bumped and any resolved_at
        cleared (they are back / still here) — first_listed_at and the manual
        status are preserved (a dismissed student STAYS dismissed until you
        clear it);
      * one previously listed but no longer present gets resolved_at = now
        (they left the worklist — passed / departed / no longer scored).
    """
    ts = now or datetime.now()
    ts_iso = ts.isoformat(timespec="seconds")
    live_keys = {(r["student_id"], r["course_code"]) for r in live}

    conn = _connect(db_path)
    try:
        existing = {(r["student_id"], r["course_code"]): r for r in
                    conn.execute("SELECT * FROM chase_list").fetchall()}
        with conn:
            for r in live:
                key = (r["student_id"], r["course_code"])
                if key in existing:
                    conn.execute(
                        "UPDATE chase_list SET last_listed_at = ?, "
                        "resolved_at = NULL WHERE student_id = ? AND "
                        "course_code = ?", (ts_iso, key[0], key[1]))
                else:
                    conn.execute(
                        "INSERT INTO chase_list (student_id, course_code, "
                        "first_listed_at, last_listed_at, status, status_at, "
                        "resolved_at) VALUES (?, ?, ?, ?, '', NULL, NULL)",
                        (key[0], key[1], ts_iso, ts_iso))
            for key, row in existing.items():
                if key not in live_keys and row["resolved_at"] is None:
                    conn.execute(
                        "UPDATE chase_list SET resolved_at = ? WHERE "
                        "student_id = ? AND course_code = ?",
                        (ts_iso, key[0], key[1]))
        persisted = {(r["student_id"], r["course_code"]): r for r in
                     conn.execute("SELECT * FROM chase_list").fetchall()}
    finally:
        conn.close()

    today = ts.date()
    for r in live:
        p = persisted.get((r["student_id"], r["course_code"]))
        first = (p["first_listed_at"] if p else None) or ts_iso
        try:
            days_on = (today - datetime.fromisoformat(first).date()).days
        except Exception:
            days_on = 0
        r["first_listed_at"] = first
        r["days_on_list"] = days_on
        r["chase_status"] = (p["status"] or "") if p else ""
        r["status_at"] = (p["status_at"] or "") if p else ""
    return live


def set_chase_status(student_id: str, course_code: str, status: str, *,
                     db_path=HISTORY_DB, now: Optional[datetime] = None):
    """Set the instructor's manual status on a chase-list row. ``status`` in
    {'contacted','dismissed'}; '' clears it (back to active). Upserts so it
    works even if a sync has not recorded the row yet."""
    status = (status or "").strip().lower()
    if status and status not in _CHASE_STATUSES:
        raise ValueError(f"status must be one of {_CHASE_STATUSES} or ''")
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO chase_list (student_id, course_code, "
                "first_listed_at, last_listed_at, status, status_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(student_id, course_code) "
                "DO UPDATE SET status = excluded.status, "
                "status_at = excluded.status_at",
                (student_id, course_code, ts, ts, status, ts))
    finally:
        conn.close()


def chase_worklist(*, window_weeks: Optional[int] = None, db_path=HISTORY_DB,
                   now: Optional[datetime] = None,
                   include_dismissed: bool = False) -> list[dict]:
    """The momentum-risk targeting list as a persistent worklist: computes the
    ranked rows (momentum_risk_students over ``window_weeks``), reconciles
    membership/status, then returns them — hiding dismissed students unless
    ``include_dismissed``. Dismissed rows keep chase_status='dismissed' so the
    caller can grey them."""
    rows = momentum_risk_students(window_weeks=window_weeks, db_path=db_path,
                                  now=now)
    rows = sync_chase_list(rows, db_path=db_path, now=now)
    if not include_dismissed:
        rows = [r for r in rows if r.get("chase_status") != "dismissed"]
    return rows


def _actionable_fields(r, e, *, db_path=HISTORY_DB, led=None) -> dict:
    """The common actionable-worklist fields for a caseload snapshot row: how to
    reach them (reachable channels + opt-in), the ledger-backed suggested channel
    and contact preference (manual override else the channel they engage on),
    whether they've ever replied, and the urgency/neglect metrics. Shared by
    at_risk_students() and momentum_risk_students()."""
    sid = r["student_id"]
    led = led if led is not None else engagement_ledger(sid, db_path=db_path)
    pref = contact_preference(sid, db_path=db_path, _ledger=led)
    opted_in = _ej_get(e, "TextingPreference") == "Opted In"
    has_email = bool(_ej_get(e, "StudentEmail"))
    reachable = [c for c, ok in (("text", opted_in), ("email", has_email)) if ok]
    ever_responded = any(
        (info.get("in", 0) or (info.get("out", 0) if c == "call" else 0))
        for c, info in led.get("channels", {}).items())
    # what to lead with: their preference; else text if opted-in (§15 — disengaged
    # respond 68–80% to text); else email.
    suggested = pref["pref"] or ("text" if opted_in else "email")
    return {
        "name": r["name"] or "",
        "student_id": sid,
        "course_code": r["course_code"],
        "momentum": r["momentum"] or "",
        "term_days_left": _to_int(_ej_get(e, "TermDaysLeft")),
        "days_since_contact": _to_int(_ej_get(e, "DaysSinceLastCourseContact")),
        "other_courses": count_other_courses(
            _ej_get(e, "OtherCourses"), r["course_code"]),
        "reachable": "+".join(reachable) if reachable else "—",
        "opted_in_text": opted_in,
        "contact_pref": pref["pref"],
        "contact_pref_source": pref["source"],
        "suggested_channel": suggested,
        "ever_responded": ever_responded,
        "ic_end": _ej_get(e, "Icenddate"),
    }


# §17 (FINDINGS): the two INDEPENDENTLY-VALIDATED risk signals that momentum-risk
# — a passer-biased FLOOR — structurally can't rank, because a student who never
# submits or never replies looks "safe" on momentum:
#   * a STALLED never-attempter (§12 attempt≈pass) — but only once enrolled long
#     enough to have started (a fresh enrollee is benign; §12 time-weighting), and
#   * a GONE-SILENT student (§9 non-response separates outcomes) — contacted, yet
#     never replied on any channel.
# On the current caseload 16% of students are momentum-"safe" (<8%) but a real
# signal risk, and all were reachable. So these are promoted to a ranking TIER,
# not just displayed as flags.
_SIGNAL_STALL_DAYS = 42          # 6 weeks enrolled: the "should have engaged by now"
                                 # gate that keeps FRESH students (who simply
                                 # haven't attempted / replied YET) out of both
                                 # signals — the §12 time-weighting principle.
# never-attempted-stalled is floored to the warm boundary so a momentum-SAFE
# non-attempter still surfaces above the safe crowd (§12: attempting ≈ passing,
# at any momentum).
_SIGNAL_PRIORITY_FLOOR = 0.08
# §18: silence only PREDICTS not-passing when momentum is ALREADY risky —
# momentum-safe+silent students still pass ~98%, but risky+silent not-pass ~53%
# (vs 10% responsive). So silence is a risk AMPLIFIER, not a standalone flag: it
# only fires above this momentum-risk floor, and there it ranks the student at
# the very top (53% > the ~29% momentum-risk ceiling).
_SIGNAL_MODERATE_RISK = 0.08
_SILENT_AMPLIFY_FLOOR = 0.30


def _signal_flags(row: dict) -> dict:
    """Compute the validated-signal flags + a chase `priority` from a row's
    existing fields (never_attempted, days_into_course, days_since_contact,
    ever_responded, risk). Both signals need ≥6 weeks enrolled (a fresh student
    who just hasn't engaged yet isn't flagged, §12). ``never_attempted`` stands
    alone at any momentum (§12); ``silent`` only bites when momentum is already
    risky and then amplifies to the top (§18). Merged onto the row."""
    risk = float(row.get("risk") or 0.0)
    enrolled = (row.get("days_into_course") or 0) >= _SIGNAL_STALL_DAYS
    # never attempted despite ≥6 weeks in — the C769 risk (§12), momentum-agnostic.
    stalled = enrolled and bool(row.get("never_attempted"))
    # contacted, ≥6wk in, never replied on any channel — but only a signal where
    # momentum is ALREADY risky (§18); safe+silent students pass ~98%.
    silent = (enrolled and row.get("days_since_contact") is not None
              and not row.get("ever_responded")
              and risk >= _SIGNAL_MODERATE_RISK)
    parts = []
    if stalled:
        parts.append("stalled")
    if silent:
        parts.append("silent")
    priority = risk
    if stalled:
        priority = max(priority, _SIGNAL_PRIORITY_FLOOR)
    if silent:
        priority = max(priority, _SILENT_AMPLIFY_FLOOR)
    return {
        "never_attempted_stalled": stalled,
        "gone_silent": silent,
        "signal_flag": "+".join(parts),      # "", "stalled", "silent", "stalled+silent"
        "signal_flagged": bool(parts),
        "priority": priority,
    }


# ----------------------------------------------------------------------
# momentum-trajectory risk model (not-pass probability from momentum history)
# ----------------------------------------------------------------------
# FINDINGS: not-passers averaged momentum rank 2.0 and spent 69% of their time at
# Low/Med-Low, vs passers at 4.1 / 17% — the *trajectory* separates far harder
# than a single (noisy, ~daily-jittering) reading. We bin a student's average
# momentum rank and map it to an empirical not-pass rate calibrated on resolved
# outcomes. Absolute values are a FLOOR (WGU's export under-captures non-passers)
# — trust the ordering. never-attempted is kept as a SEPARATE flag, not folded in
# (it's a different axis, and 'resolved + never-attempted → not-passed' is nearly
# tautological, so baking it into the score would distort it).
_RISK_BUCKETS = [(1.5, "1.0–1.5"), (2.5, "1.5–2.5"), (3.5, "2.5–3.5"),
                 (4.5, "3.5–4.5"), (99.0, "4.5–5.0")]
# Representative avg-rank at each bucket's centre — the anchor points the
# continuous risk curve is interpolated between (so risk varies smoothly with
# avg momentum rank instead of snapping to 4–5 discrete bucket rates, which left
# gaps that made band filters like "20–30%" match nobody).
_RISK_BUCKET_CENTERS = (1.25, 2.0, 3.0, 4.0, 4.75)


def _avg_rank_bucket_idx(avg: float) -> int:
    for i, (hi, _lab) in enumerate(_RISK_BUCKETS):
        if avg < hi:
            return i
    return len(_RISK_BUCKETS) - 1


def _interp_risk(avg: float, rates: list) -> float:
    """Continuous not-pass risk for a student's average momentum rank: piecewise-
    linear interpolation between the per-bucket calibrated ``rates`` anchored at
    _RISK_BUCKET_CENTERS, clamped past the end anchors. Gives a smooth spread
    (so risk bands are populated) while staying pinned to the calibrated rates."""
    centers = _RISK_BUCKET_CENTERS
    if avg <= centers[0]:
        return rates[0]
    if avg >= centers[-1]:
        return rates[-1]
    for i in range(len(centers) - 1):
        a, b = centers[i], centers[i + 1]
        if a <= avg <= b:
            f = (avg - a) / (b - a) if b > a else 0.0
            return rates[i] + f * (rates[i + 1] - rates[i])
    return rates[-1]


def _isotonic_noninc(counts: list[tuple]) -> list[float]:
    """Pool-adjacent-violators (PAVA): given (notpass, total) per bucket ordered
    by ASCENDING momentum rank, return a per-bucket rate that is NON-INCREASING
    (higher momentum ⇒ lower risk), pooling adjacent violators weighted by sample
    size. Smooths the small-n wobble in the raw bucket rates into a monotonic fit."""
    blocks = [{"np": np_, "tot": tot, "idx": [i]}
              for i, (np_, tot) in enumerate(counts)]
    i = 0
    while i < len(blocks) - 1:
        ri = blocks[i]["np"] / blocks[i]["tot"] if blocks[i]["tot"] else 0.0
        rj = blocks[i + 1]["np"] / blocks[i + 1]["tot"] if blocks[i + 1]["tot"] else 0.0
        if ri < rj - 1e-9:          # violation: rate should be non-increasing
            blocks[i]["np"] += blocks[i + 1]["np"]
            blocks[i]["tot"] += blocks[i + 1]["tot"]
            blocks[i]["idx"] += blocks[i + 1]["idx"]
            del blocks[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    out = [0.0] * len(counts)
    for b in blocks:
        rate = b["np"] / b["tot"] if b["tot"] else 0.0
        for idx in b["idx"]:
            out[idx] = rate
    return out


def momentum_risk_calibration(*, db_path=HISTORY_DB) -> dict:
    """Empirical not-pass probability by average-momentum-rank bucket, calibrated
    on resolved outcomes and smoothed monotonic (PAVA). Uses each resolved
    student's FULL momentum history (the most stable estimate). Returns
    ``{'buckets': [label…], 'prob': [rate…], 'counts': [(notpass, total)…]}``
    aligned to _RISK_BUCKETS. Absolute rates are a floor (see module note)."""
    from collections import defaultdict
    conn = _connect(db_path)
    try:
        series = defaultdict(list)
        for s in conn.execute("SELECT student_id, course_code, momentum_rank "
                              "FROM snapshots WHERE momentum_rank IS NOT NULL"):
            series[(s["student_id"], s["course_code"])].append(s["momentum_rank"])
        counts = [[0, 0] for _ in _RISK_BUCKETS]
        for o in conn.execute("SELECT student_id, course_code, outcome "
                              "FROM outcomes"):
            h = series.get((o["student_id"], o["course_code"]))
            if not h:
                continue
            bi = _avg_rank_bucket_idx(sum(h) / len(h))
            counts[bi][1] += 1
            if o["outcome"] == "not_passed":
                counts[bi][0] += 1
    finally:
        conn.close()
    prob = _isotonic_noninc([(c[0], c[1]) for c in counts])
    return {"buckets": [b[1] for b in _RISK_BUCKETS], "prob": prob,
            "counts": [tuple(c) for c in counts]}


def momentum_risk_students(*, window_weeks: Optional[int] = None,
                           db_path=HISTORY_DB,
                           now: Optional[datetime] = None) -> list[dict]:
    """The MOMENTUM-RISK targeting list: in-progress caseload students ranked by
    modeled not-pass probability from their momentum TRAJECTORY.

    ``window_weeks`` limits the trajectory to the last N weeks (None = all
    history — most stable; shorter windows react faster to a recent slide). The
    per-row ``risk`` is the calibrated not-pass probability for the student's
    windowed average momentum rank. Each row also carries ``avg_momentum_rank``,
    ``readings`` (how many snapshots backed it), ``trend`` (recent-half minus
    earlier-half mean; >0 = recovering, so you can deprioritise climbers), a
    ``never_attempted`` flag (a SEPARATE acute axis, not in the score), and the
    actionable reach/suggested-channel/contact-pref fields. Sorted by risk desc,
    then lower avg rank, then most-neglected."""
    from collections import defaultdict
    from datetime import timedelta
    today = (now or datetime.now()).date()
    calib = momentum_risk_calibration(db_path=db_path)
    conn = _connect(db_path)
    try:
        latest = conn.execute("SELECT collected_at FROM collections "
                              "ORDER BY collected_at DESC LIMIT 1").fetchone()
        if latest is None:
            return []
        rows = conn.execute("SELECT * FROM snapshots WHERE collected_at = ?",
                            (latest["collected_at"],)).fetchall()
        resolved = {(r["student_id"], r["course_code"]) for r in
                    conn.execute("SELECT student_id, course_code FROM outcomes")}
        cut = None
        if window_weeks:
            cut = (today - timedelta(days=7 * window_weeks)).isoformat()
        q = ("SELECT student_id, course_code, momentum_rank FROM snapshots "
             "WHERE momentum_rank IS NOT NULL")
        params: tuple = ()
        if cut:
            q += " AND collected_date >= ?"
            params = (cut,)
        q += " ORDER BY collected_date"
        traj = defaultdict(list)
        for s in conn.execute(q, params):
            traj[(s["student_id"], s["course_code"])].append(s["momentum_rank"])
    finally:
        conn.close()

    out = []
    for r in rows:
        key = (r["student_id"], r["course_code"])
        if key in resolved:
            continue
        try:
            e = json.loads(r["extra_json"] or "{}")
        except Exception:
            e = {}
        status = _ej_get(e, "CourseStatus")
        start = _parse_date(_ej_get(e, "CourseStartDate"))
        days_into = (today - start).days if start else None
        if status == "Planned" or days_into is None or days_into <= 0:
            continue                              # not underway
        h = traj.get(key) or ([r["momentum_rank"]] if r["momentum_rank"] else [])
        if not h:
            continue                              # no momentum reading to score
        avg = sum(h) / len(h)
        risk = _interp_risk(avg, calib["prob"])
        half = len(h) // 2 or 1
        trend = (sum(h[half:]) / max(1, len(h) - half)) - (sum(h[:half]) / half)
        row = _actionable_fields(r, e, db_path=db_path)
        row.update({
            "risk": risk,
            "avg_momentum_rank": round(avg, 2),
            "readings": len(h),
            "trend": round(trend, 2),
            "never_attempted": not _has_attempted(r["latest_task_status"], e),
            "days_into_course": days_into,
        })
        # §17 validated-signal tier: floors gone-silent / stalled-never-attempted
        # students so the momentum FLOOR can't bury them (adds `priority`).
        row.update(_signal_flags(row))
        out.append(row)
    # Rank by chase priority (momentum risk, raised to a floor for signal-flagged
    # students) first, then the real momentum risk, then avg rank, then neglect —
    # so genuine high-risk stays on top and momentum-safe-but-flagged surfaces
    # above the safe crowd instead of at the very bottom.
    out.sort(key=lambda a: (-a["priority"], -a["risk"], a["avg_momentum_rank"],
                            -(a["days_since_contact"] or 0)))
    return out


# ----------------------------------------------------------------------
# momentum calibration (entry prediction vs actual outcome)
# ----------------------------------------------------------------------
def _parse_date(s):
    """Lenient date parse for the assorted CSV date spellings; None on failure."""
    if not s:
        return None
    s = str(s).strip()
    for cand in (s, s[:10]):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(cand, fmt).date()
            except Exception:
                pass
    return None


def _ej_get(ej: dict, header: str) -> str:
    """Pull a non-core field from a snapshot's extra_json, tolerating API vs
    display-name header spellings."""
    for h in _candidate_headers(header):
        v = ej.get(h)
        if isinstance(v, str):
            v = v.strip()
        if v:
            return v
    return ""


# Predicted pass-probability range (%) for each Momentum band, per WGU's model.
_MOMENTUM_BANDS = [
    (5, "High",     "80-100"),
    (4, "Med High", "60-80"),
    (3, "Med",      "40-60"),
    (2, "Med Low",  "20-40"),
    (1, "Low",      "0-20"),
]


def course_codes(*, db_path=HISTORY_DB) -> list[str]:
    """Distinct course codes seen across snapshots + outcomes, sorted — the
    options for the calibration course picker."""
    conn = _connect(db_path)
    try:
        s = set()
        for q in ("SELECT DISTINCT course_code FROM snapshots",
                  "SELECT DISTINCT course_code FROM outcomes"):
            for r in conn.execute(q):
                if r[0]:
                    s.add(r[0])
    finally:
        conn.close()
    return sorted(s)


def momentum_calibration(*, db_path=HISTORY_DB, eligible_from="2026-06-10",
                         course_load="all", course=None,
                         date_from=None, date_to=None,
                         now: Optional[datetime] = None) -> dict:
    """Compare each student's ENTRY-time Momentum band to their actual outcome.

    Entry momentum = the snapshot reading nearest the student's CourseStartDate
    (for a course that started after we began collecting, that's the genuine
    at-enrollment reading; ``eligible_from`` restricts the cohort to those).

    Outcome per (student, course):
      - **passed (in time)**  — in the outcomes archive AND pass_date is on/before
        the deadline (IC End Date if present, else Term End Date),
      - **passed (late)**     — passed but after that deadline,
      - **missed deadline**   — not passed and the deadline is already in the past,
      - **in progress**       — not passed and the deadline hasn't arrived (or is
        unknown); not yet a resolved outcome.

    The calibration pass rate per band = passed-in-time / resolved (resolved =
    passed-in-time + passed-late + missed); in-progress is excluded from the
    rate. Returns a dict with an ordered ``bands`` table plus cohort metadata.
    ``eligible_from`` of an early date (e.g. '1900-01-01') includes everyone,
    using each student's earliest snapshot as a (mid-course, imperfect) proxy.
    """
    from collections import defaultdict
    today = (now or datetime.now()).date()
    elig = _parse_date(eligible_from)
    # Optional resolution-date window. When active, ONLY students whose outcome
    # resolved in the window are counted (in-progress + out-of-window resolved
    # are excluded) — i.e. "of students who finished in this period, …".
    df = _parse_date(date_from) if date_from else None
    dto = _parse_date(date_to) if date_to else None
    date_active = df is not None or dto is not None

    def _in_window(o):
        d = _resolution_date(o)
        return (d is not None and (df is None or d >= df)
                and (dto is None or d <= dto))

    conn = _connect(db_path)
    try:
        snaps = conn.execute(
            "SELECT student_id, course_code, collected_date, momentum, "
            "momentum_rank, extra_json FROM snapshots ORDER BY collected_at ASC"
        ).fetchall()
        outcomes = {(r["student_id"], r["course_code"]): dict(r)
                    for r in conn.execute("SELECT * FROM outcomes")}
        latest = conn.execute("SELECT collected_at FROM collections "
                              "ORDER BY collected_at DESC LIMIT 1").fetchone()
        latest_keys = set()
        if latest is not None:
            latest_keys = {(r["student_id"], r["course_code"]) for r in
                           conn.execute("SELECT student_id, course_code FROM "
                                        "snapshots WHERE collected_at = ?",
                                        (latest["collected_at"],))}
    finally:
        conn.close()

    per = defaultdict(list)
    for r in snaps:
        per[(r["student_id"], r["course_code"])].append(r)

    # Tally counters per band rank, plus cohort-level bookkeeping. The archive's
    # CourseStatus gives an authoritative negative class ("not_passed"); a
    # departed student not yet in any archive stays "in_progress" until the next
    # archive resolves them. "missed" = still on caseload but past the deadline.
    cells = {rank: {"passed_in_time": 0, "passed_late": 0, "not_passed": 0,
                    "missed": 0, "in_progress": 0}
             for rank, _, _ in _MOMENTUM_BANDS}
    eligible = no_entry_band = not_started = 0
    skipped_no_start = skipped_pre_window = 0
    rows_detail = []  # per-student, for CSV export
    _RANK_LABEL = {rank: label for rank, label, _ in _MOMENTUM_BANDS}

    keys = set(per) | set(outcomes)
    for key in keys:
        if course and key[1] != course:
            continue
        rows = per.get(key, [])
        oc = outcomes.get(key)
        if date_active and (oc is None or not _in_window(oc)):
            continue       # date window = resolved-in-window students only
        latest_ej = json.loads(rows[-1]["extra_json"] or "{}") if rows else {}

        # Course start (eligibility). Prefer a snapshot's value; fall back to the
        # outcome row for a passer we never snapshotted.
        cs_raw = _ej_get(latest_ej, "CourseStartDate") or (
            oc.get("course_start_date") if oc else "")
        cs = _parse_date(cs_raw)
        if cs is None:
            skipped_no_start += 1
            continue
        if elig is not None and cs < elig:
            skipped_pre_window += 1
            continue
        # Course-load filter: students juggling other courses are often told to
        # finish those first, so their outcome here isn't a clean test of the
        # prediction. 'single' = only this course, 'multi' = 1+ others.
        oc_raw = ((oc.get("other_courses") if oc else "")
                  or _ej_get(latest_ej, "OtherCourses"))
        other_count = count_other_courses(oc_raw, key[1])
        if course_load == "single" and other_count != 0:
            continue
        if course_load == "multi" and other_count < 1:
            continue
        eligible += 1

        # Entry momentum = reading nearest CourseStartDate (earliest if no dates).
        entry_rank = None
        if rows:
            dated = [(r, _parse_date(r["collected_date"])) for r in rows]
            dated = [(r, d) for r, d in dated if d is not None]
            if dated:
                best = min(dated, key=lambda rd: abs((rd[1] - cs).days))[0]
                entry_rank = best["momentum_rank"]
        if entry_rank is None:
            no_entry_band += 1
            continue

        # Deadline = IC End Date (extension) if present, else Term End Date.
        ic = (oc.get("ic_end_date") if oc else "") or _ej_get(latest_ej, "Icenddate")
        te = (oc.get("term_end_date") if oc else "") or _ej_get(latest_ej, "TermEndDate")
        deadline = _parse_date(ic) or _parse_date(te)

        if oc is not None:
            if (oc.get("outcome") or "") == "not_passed":
                outcome_label = "not_passed"    # authoritative, from CourseStatus
            else:
                pd = _parse_date(oc.get("pass_date"))
                if deadline and pd and pd > deadline:
                    outcome_label = "passed_late"
                else:
                    outcome_label = "passed_in_time"
        elif deadline and deadline < today and key in latest_keys:
            outcome_label = "missed"            # still here but past the deadline
        else:
            # Not yet resolved — still on caseload pre-deadline, or departed but
            # not yet in an archive (the next archive download will resolve it).
            outcome_label = "in_progress"
            if cs > today:
                not_started += 1
        cells[entry_rank][outcome_label] += 1
        rows_detail.append({
            "student_id": key[0], "course_code": key[1],
            "name": (_ej_get(latest_ej, "Name")
                     or (oc.get("name") if oc else "") or ""),
            "entry_band": _RANK_LABEL.get(entry_rank, ""),
            "entry_rank": entry_rank,
            "outcome": outcome_label,
            "course_start": cs.isoformat() if cs else "",
            "deadline": deadline.isoformat() if deadline else "",
            "pass_date": (oc.get("pass_date") if oc else "") or "",
            "other_course_count": other_count,
            "other_courses": oc_raw,
        })

    bands = []
    for rank, label, rng in _MOMENTUM_BANDS:
        c = cells[rank]
        passed = c["passed_in_time"] + c["passed_late"]
        resolved = passed + c["not_passed"] + c["missed"]
        total = resolved + c["in_progress"]
        rate = (c["passed_in_time"] / resolved) if resolved else None
        bands.append({
            "rank": rank, "label": label, "predicted_range": rng,
            "passed_in_time": c["passed_in_time"],
            "passed_late": c["passed_late"], "not_passed": c["not_passed"],
            "missed": c["missed"],
            "in_progress": c["in_progress"], "resolved": resolved,
            "total": total,
            "pass_in_time_rate": rate,
        })

    return {
        "eligible_from": eligible_from,
        "course_load": course_load,
        "course": course,
        "as_of": today.isoformat(),
        "bands": bands,
        "eligible_total": eligible,
        "not_started": not_started,
        "no_entry_band": no_entry_band,
        "skipped_no_start": skipped_no_start,
        "skipped_pre_window": skipped_pre_window,
        "rows": rows_detail,
    }


def momentum_calibration_at_exit(*, db_path=HISTORY_DB, course_load="all",
                                 course=None, date_from=None, date_to=None,
                                 now: Optional[datetime] = None) -> dict:
    """Calibration of Momentum AS RECORDED IN THE ARCHIVE (at course exit) vs.
    the actual outcome — i.e. how well the *final* Momentum reading matched the
    result. Unlike the entry view this needs no snapshot history, so every
    resolved outcome counts (high volume immediately). Directly tests whether
    Momentum self-corrects toward the outcome by the end of the course.

    Same band-table shape as ``momentum_calibration`` (missed/in_progress are
    always 0 here — every archive row is a resolved outcome)."""
    today = (now or datetime.now()).date()
    conn = _connect(db_path)
    try:
        outs = [dict(r) for r in conn.execute("SELECT * FROM outcomes")]
    finally:
        conn.close()

    cells = {rank: {"passed_in_time": 0, "passed_late": 0, "not_passed": 0}
             for rank, _, _ in _MOMENTUM_BANDS}
    df = _parse_date(date_from) if date_from else None
    dto = _parse_date(date_to) if date_to else None
    no_band = 0
    for oc in outs:
        if course and (oc.get("course_code") or "") != course:
            continue
        if df or dto:
            d = _resolution_date(oc)
            if d is None or (df and d < df) or (dto and d > dto):
                continue
        cnt = oc.get("other_course_count")
        if cnt is None:
            cnt = count_other_courses(oc.get("other_courses") or "",
                                      oc.get("course_code"))
        if course_load == "single" and cnt:
            continue
        if course_load == "multi" and not cnt:
            continue
        rank = oc.get("momentum_rank_at_outcome")
        if rank not in cells:
            no_band += 1
            continue
        if (oc.get("outcome") or "") == "not_passed":
            cells[rank]["not_passed"] += 1
        else:
            deadline = (_parse_date(oc.get("ic_end_date"))
                        or _parse_date(oc.get("term_end_date")))
            pd = _parse_date(oc.get("pass_date"))
            if deadline and pd and pd > deadline:
                cells[rank]["passed_late"] += 1
            else:
                cells[rank]["passed_in_time"] += 1

    bands = []
    for rank, label, rng in _MOMENTUM_BANDS:
        c = cells[rank]
        resolved = c["passed_in_time"] + c["passed_late"] + c["not_passed"]
        rate = (c["passed_in_time"] / resolved) if resolved else None
        bands.append({
            "rank": rank, "label": label, "predicted_range": rng,
            "passed_in_time": c["passed_in_time"],
            "passed_late": c["passed_late"], "not_passed": c["not_passed"],
            "missed": 0, "in_progress": 0, "resolved": resolved,
            "total": resolved, "pass_in_time_rate": rate,
        })
    return {
        "eligible_from": None, "course_load": course_load,
        "as_of": today.isoformat(), "bands": bands,
        "eligible_total": sum(b["resolved"] for b in bands) + no_band,
        "not_started": 0,
        "no_entry_band": no_band, "skipped_no_start": 0,
        "skipped_pre_window": 0, "rows": [],
    }


def _resolution_date(o):
    """The date an outcome resolved: pass_date for a pass, else the Term End
    (or IC End) deadline for a non-pass. None if unparseable. Shared by the
    over-time chart and the date-range filters."""
    if (o.get("outcome") or "") == "passed":
        return _parse_date(o.get("pass_date"))
    return (_parse_date(o.get("term_end_date"))
            or _parse_date(o.get("ic_end_date")))


def outcomes_over_time(*, db_path=HISTORY_DB, weeks_back=16,
                       date_from=None, date_to=None,
                       now: Optional[datetime] = None) -> dict:
    """Resolved outcomes bucketed by ISO week (Monday) for a completions /
    pass-rate-over-time chart. Resolution date = pass_date for a pass, else the
    Term End (or IC End) deadline for a non-pass. Only PAST resolutions count —
    a non-pass whose deadline is still in the future hasn't resolved yet, so
    it's excluded until its term ends. The window is ``[date_from, date_to]``
    when given (else the last ``weeks_back`` weeks up to today). Each week
    carries passed / not_passed / total / rate."""
    from datetime import timedelta
    today = (now or datetime.now()).date()
    lower = _parse_date(date_from) or (today - timedelta(weeks=weeks_back))
    upper = _parse_date(date_to) or today
    upper = min(upper, today)
    buckets = {}
    for o in all_outcomes(db_path=db_path):
        d = _resolution_date(o)
        if d is None or d < lower or d > upper:
            continue
        wk = d - timedelta(days=d.weekday())
        b = buckets.setdefault(wk, {"passed": 0, "not_passed": 0})
        b["passed" if o["outcome"] == "passed" else "not_passed"] += 1
    weeks = []
    for w in sorted(buckets):
        p, np_ = buckets[w]["passed"], buckets[w]["not_passed"]
        t = p + np_
        weeks.append({"week": w.isoformat(), "passed": p, "not_passed": np_,
                      "total": t, "rate": (p / t if t else None)})
    return {"weeks": weeks}


def momentum_band_bounds(rank):
    """(low, high) predicted pass-probability bounds (0..1) for a Momentum rank —
    the band's published range (Low 0-20 → (.0, .2) … High 80-100 → (.8, 1.0)).
    (None, None) for an unknown/blank rank."""
    for r, _label, rng in _MOMENTUM_BANDS:
        if r == rank:
            lo, hi = (int(v) for v in rng.split("-"))
            return lo / 100.0, hi / 100.0
    return None, None


def momentum_band_rate(rank) -> Optional[float]:
    """WGU's PREDICTED pass probability (0..1) for a Momentum rank — the MIDPOINT
    of that band's published range (Low 0-20 → .10 … High 80-100 → .90). The
    indicator's own prediction, NOT a trained model. None for unknown/blank."""
    lo, hi = momentum_band_bounds(rank)
    return None if lo is None else (lo + hi) / 2.0


def completion_by_month(*, by="start", basis="entry", courses=None,
                        db_path=HISTORY_DB, date_from=None, date_to=None,
                        now: Optional[datetime] = None) -> dict:
    """Course completion by month — ACTUAL pass rate vs the WGU Momentum
    indicator's PREDICTED pass rate (the midpoint of each student's Momentum
    band; the indicator's own prediction, not a trained model).

    ``by="start"`` buckets by course-START month (a cohort); ``"resolution"``
    buckets by the resolution month (pass date / term end). ``basis="entry"``
    reads each student's entry-time Momentum band; ``"exit"`` reads their band at
    the outcome (the value at the time they passed/resolved) — the same choice as
    the calibration view.

    Per month: ``{month, total, passed, resolved, actual_rate, predicted_rate,
    predicted_low, predicted_high}`` where actual_rate = passed ÷ resolved,
    predicted_rate = mean Momentum-band midpoint, and predicted_low/high = mean of
    the band LOW/HIGH bounds over students that month with a reading (the band
    drawn as a translucent range). Rates 0..1 or None. ``courses`` restricts."""
    today = (now or datetime.now()).date()
    lower = _parse_date(date_from)
    upper = min(_parse_date(date_to) or today, today)
    rank_key = ("entry_momentum_rank" if basis == "entry"
                else "momentum_rank_at_outcome")
    buckets = {}
    for o in all_outcomes(db_path=db_path):
        if courses is not None and o["course_code"] not in courses:
            continue
        rd = _resolution_date(o)
        resolved = rd is not None and rd <= today
        d = (rd if resolved else None) if by == "resolution" \
            else _parse_date(o.get("course_start_date"))
        if d is None or (lower and d < lower) or d > upper:
            continue
        month = d.replace(day=1)
        b = buckets.setdefault(month, {"total": 0, "passed": 0, "resolved": 0,
                                       "lo_sum": 0.0, "hi_sum": 0.0, "pred_n": 0})
        b["total"] += 1
        if o.get("outcome") == "passed":
            b["passed"] += 1
        if resolved:
            b["resolved"] += 1
        lo, hi = momentum_band_bounds(o.get(rank_key))
        if lo is not None:
            b["lo_sum"] += lo
            b["hi_sum"] += hi
            b["pred_n"] += 1
    months = []
    for m in sorted(buckets):
        b = buckets[m]
        n = b["pred_n"]
        lo = (b["lo_sum"] / n) if n else None
        hi = (b["hi_sum"] / n) if n else None
        months.append({
            "month": m.isoformat()[:7],
            "total": b["total"], "passed": b["passed"],
            "resolved": b["resolved"],
            "actual_rate": (b["passed"] / b["resolved"]) if b["resolved"]
            else None,
            "predicted_low": lo, "predicted_high": hi,
            "predicted_rate": ((lo + hi) / 2.0) if lo is not None else None,
        })
    return {"months": months}


# Contacts-line metric options for the throughput view (label handled in the UI).
_THROUGHPUT_CONTACTS = ("sent", "unique", "all")


def _month_label(mk: str) -> str:
    """'2026-06' -> 'Jun' (with a 'Jan'26 year tag when the year turns, so
    cross-year axes stay unambiguous). Falls back to the raw key on any parse
    failure."""
    try:
        dt = datetime.strptime(mk, "%Y-%m")
        return dt.strftime("%b") if dt.month != 1 else dt.strftime("%b'%y")
    except Exception:
        return mk


def monthly_throughput(*, db_path=HISTORY_DB, courses=None, contacts="sent",
                       date_from=None, date_to=None, include_last30=True,
                       now: Optional[datetime] = None) -> dict:
    """Caseload THROUGHPUT by month: how many DISTINCT students were assigned to
    the caseload each month (from the daily snapshots), stacked by course, with a
    contacts line.

    The live caseload export only shows who is enrolled TODAY, so a snapshot of it
    understates the real volume handled — students pass or depart and are replaced.
    Summing the unique students seen in each month's snapshots reveals the actual
    throughput over time (the "we've served far more than the current headcount"
    story).

    Unique student assignments: a student counts in month M for course C if any
    snapshot in M carries (student_id, C). The per-month TOTAL is the sum over
    courses, so a student enrolled in two courses that month counts once per
    course — i.e. student-course assignments, matching the stacked bars.

    Contacts (a secondary line) come from the notes history, per month:
      - "sent"   : outbound outreach events (text / email / call we sent),
      - "unique" : distinct students we reached (any outbound outreach),
      - "all"    : every logged note (incl. inbound replies + admin notes).

    ``courses`` (a set) restricts both series; None = all courses. When a strict
    subset is given, contacts are filtered by the note's (best-effort)
    ``course_code`` and course-less notes are excluded; with None, every note
    counts. ``date_from``/``date_to`` bound the calendar months shown.

    ``include_last30`` appends a synthetic trailing bucket (``month='last30'``)
    for the rolling last 30 days, so recent volume shows even mid-month; it is NOT
    included in ``avg_load`` (which averages the complete calendar months shown).

    Returns ``{months:[{month,label,by_course:{code:n},total,contacts}],
    courses:[…], avg_load: float|None, months_count: int}``.
    """
    from collections import defaultdict
    from datetime import timedelta
    today = (now or datetime.now()).date()
    lower = _parse_date(date_from) if date_from else None
    upper = min(_parse_date(date_to) or today, today)
    d30 = today - timedelta(days=30)
    sel = set(courses) if courses is not None else None

    def _in_window(d):
        return d is not None and (lower is None or d >= lower) and d <= upper

    mcs = defaultdict(lambda: defaultdict(set))   # month -> course -> {student}
    last30_cs = defaultdict(set)                   # course -> {student}
    mc = defaultdict(int)                          # month -> contacts count
    m_uniq = defaultdict(set)                      # month -> {student} (unique)
    last30_contacts = 0
    last30_uniq: set = set()
    conn = _connect(db_path)
    try:
        for r in conn.execute(
                "SELECT DISTINCT collected_date, student_id, course_code "
                "FROM snapshots"):
            cc = r["course_code"]
            if not cc or (sel is not None and cc not in sel):
                continue
            d = _parse_date(r["collected_date"])
            if d is None:
                continue
            if _in_window(d):
                mcs[d.isoformat()[:7]][cc].add(r["student_id"])
            if include_last30 and d30 <= d <= today:
                last30_cs[cc].add(r["student_id"])

        for r in conn.execute(
                "SELECT created_at, student_id, course_code, channel, direction "
                "FROM notes"):
            cc = r["course_code"] or ""
            if sel is not None and (not cc or cc not in sel):
                continue
            is_outreach = (r["direction"] == "outbound"
                           and r["channel"] in ("text", "email", "call"))
            if contacts in ("sent", "unique") and not is_outreach:
                continue
            d = _parse_date(r["created_at"])
            if d is None:
                continue
            sid = r["student_id"] or ""
            if _in_window(d):
                if contacts == "unique":
                    if sid:
                        m_uniq[d.isoformat()[:7]].add(sid)
                else:
                    mc[d.isoformat()[:7]] += 1
            if include_last30 and d30 <= d <= today:
                if contacts == "unique":
                    if sid:
                        last30_uniq.add(sid)
                else:
                    last30_contacts += 1
    finally:
        conn.close()

    months = []
    for mk in sorted(mcs):
        by_course = {c: len(s) for c, s in mcs[mk].items()}
        con = len(m_uniq[mk]) if contacts == "unique" else mc.get(mk, 0)
        months.append({"month": mk, "label": _month_label(mk),
                       "by_course": by_course,
                       "total": sum(by_course.values()), "contacts": con})
    avg = (sum(m["total"] for m in months) / len(months)) if months else None
    months_count = len(months)
    present_courses = sorted({c for m in months for c in m["by_course"]})
    if include_last30:
        by_course = {c: len(s) for c, s in last30_cs.items()}
        con = len(last30_uniq) if contacts == "unique" else last30_contacts
        months.append({"month": "last30", "label": "Last 30d",
                       "by_course": by_course,
                       "total": sum(by_course.values()), "contacts": con})
    return {"months": months, "courses": present_courses,
            "avg_load": avg, "months_count": months_count}


def momentum_drift(*, db_path=HISTORY_DB, course_load="all",
                   date_from=None, date_to=None) -> dict:
    """How each entry-band's students MOVED by exit, for the resolved students
    where we have both a frozen entry reading and an exit reading. Per entry
    band: counts of improved / same / declined (exit rank higher / equal /
    lower than entry). This is the evidence that exit Momentum self-corrects —
    and so can't fairly score the entry prediction. ``course_load`` filters the
    same way as the calibration views."""
    conn = _connect(db_path)
    try:
        outs = [dict(r) for r in conn.execute(
            "SELECT * FROM outcomes WHERE entry_captured = 1 "
            "AND entry_momentum_rank IS NOT NULL "
            "AND momentum_rank_at_outcome IS NOT NULL")]
    finally:
        conn.close()

    df = _parse_date(date_from) if date_from else None
    dto = _parse_date(date_to) if date_to else None
    rows = []
    for o in outs:
        cnt = o.get("other_course_count")
        if cnt is None:
            cnt = count_other_courses(o.get("other_courses") or "",
                                      o.get("course_code"))
        if course_load == "single" and cnt:
            continue
        if course_load == "multi" and not cnt:
            continue
        if df or dto:
            d = _resolution_date(o)
            if d is None or (df and d < df) or (dto and d > dto):
                continue
        rows.append(o)

    bands = []
    for rank, label, _ in _MOMENTUM_BANDS:    # High → Low
        grp = [o for o in rows if o["entry_momentum_rank"] == rank]
        imp = sum(1 for o in grp if o["momentum_rank_at_outcome"] > rank)
        same = sum(1 for o in grp if o["momentum_rank_at_outcome"] == rank)
        dec = sum(1 for o in grp if o["momentum_rank_at_outcome"] < rank)
        bands.append({"rank": rank, "label": label, "improved": imp,
                      "same": same, "declined": dec, "total": len(grp)})
    return {"bands": bands, "total": len(rows), "course_load": course_load}


def export_calibration_csv(dest_path, *, db_path=HISTORY_DB,
                           eligible_from="2026-06-10", course_load="all",
                           now: Optional[datetime] = None) -> int:
    """Write the per-student calibration detail (entry band + outcome) to CSV.
    Returns the row count. Raises on write error (caller reports)."""
    data = momentum_calibration(db_path=db_path, eligible_from=eligible_from,
                                course_load=course_load, now=now)
    cols = ["student_id", "course_code", "name", "entry_band", "entry_rank",
            "outcome", "course_start", "deadline", "pass_date",
            "other_course_count", "other_courses"]
    rows = data["rows"]
    with open(dest_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def student_timeline(student_id: str, *, db_path=HISTORY_DB) -> list[dict]:
    """Chronological snapshots for one student (for the deferred timeline UI)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT collected_date, collected_at, course_code, momentum, "
            "momentum_rank, latest_task_status, task1, task2, task3, "
            "followup_note FROM snapshots WHERE student_id = ? "
            "ORDER BY collected_at ASC, course_code ASC",
            (student_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def task_stall_days(*, db_path=HISTORY_DB,
                    now: Optional[datetime] = None) -> dict:
    """Whole days that each student's task status has been unchanged, keyed
    ``{(student_id, course_code): days}``.

    Derived from the snapshot history: for each student+course, take the most
    recent contiguous run of an identical ``latest_task_status`` and measure
    from the first date of that run to today. So a student whose status last
    changed 17 days ago reads 17; one who changed today reads 0. Keys with no
    snapshots are omitted. Day-grained (matches the snapshot cadence).
    """
    from itertools import groupby
    today = (now or datetime.now()).date()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT student_id, course_code, collected_date, latest_task_status "
            "FROM snapshots "
            "ORDER BY student_id, course_code, collected_at ASC"
        ).fetchall()
    finally:
        conn.close()
    out: dict = {}
    keyf = lambda r: (r["student_id"], r["course_code"])
    for key, grp in groupby(rows, key=keyf):
        seq = list(grp)
        latest = (seq[-1]["latest_task_status"] or "")
        run_start = seq[-1]["collected_date"]
        for r in reversed(seq):  # walk back while the status is unchanged
            if (r["latest_task_status"] or "") == latest:
                run_start = r["collected_date"]
            else:
                break
        try:
            d = datetime.strptime(run_start[:10], "%Y-%m-%d").date()
            out[key] = (today - d).days
        except Exception:
            pass
    return out


def export_to_csv(dest_path, *, db_path=HISTORY_DB) -> int:
    """Dump the whole snapshots table to a CSV at ``dest_path``. Returns the
    number of data rows written. Raises on a write error (caller reports it)."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM snapshots "
            "ORDER BY collected_at, student_id, course_code"
        )
        header = [d[0] for d in cur.description]
        n = 0
        with open(dest_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in cur:
                w.writerow([r[c] for c in header])
                n += 1
        return n
    finally:
        conn.close()
