"""C949 cohort summary from Learner360 academic activity (read-only).

Aggregates only — prints counts/percentages, never student values. Reads
history.db through a read-only URI. Usage:
    python -m scripts.c949_summary [COURSE]

OA "completed" activity events include failed attempts, so the number of
OA events in the course = OA sittings used. Pre-assessment sittings likewise.
A "scout" took the pre-assessment within 3 days of entering the course with
no course-material activity before it.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK_TYPES = {"Course of Study Link", "Learning Resource", "panopto",
              "wgulibrary", "course.start.learning",
              "Complete Course Activity Material", "Complete Course Topic Materials"}


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def main(course: str = "C949") -> None:
    db = (ROOT / "history.db").resolve().as_posix()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cohort = {r["student_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM l360_cohort WHERE course = ?", (course,))}
    ev = defaultdict(list)
    for r in conn.execute(
            "SELECT student_id, activity_date, category, type, context, "
            "course_code, occurrences FROM l360_activity WHERE course_code = ?",
            (course,)):
        sid = (r["student_id"] or "").lstrip("0")
        if sid in cohort:
            ev[sid].append(r)
    any_act = {(r[0] or "").lstrip("0") for r in conn.execute(
        "SELECT DISTINCT student_id FROM l360_activity")}
    conn.close()
    today = date.today()
    d14 = today - timedelta(days=14)

    stu = {}
    for sid, k in cohort.items():
        rows = ev.get(sid, [])
        oa = sorted(_d(r["activity_date"]) for r in rows
                    if r["type"] == "Objective Assessment" and _d(r["activity_date"]))
        pre = []
        for r in rows:
            if r["type"] == "Objective Preassessment" and _d(r["activity_date"]):
                pre += [_d(r["activity_date"])] * int(r["occurrences"] or 1)
        pre.sort()
        enter = [_d(r["activity_date"]) for r in rows
                 if r["type"] in ("Term Enrollment", "Activate Course Resources")]
        enter = min([d for d in enter if d], default=None)
        work = sorted(_d(r["activity_date"]) for r in rows
                      if r["type"] in WORK_TYPES and _d(r["activity_date"])
                      and "provisioned" not in (r["context"] or ""))
        scout = bool(pre and enter and (pre[0] - enter).days <= 3
                     and not any(w < pre[0] for w in work))
        last_oa = oa[-1] if oa else None
        stu[sid] = {
            "program": k["program"] or "(blank)", "momentum": k["momentum"] or "(blank)",
            "owner": k["owner"] or "(blank)",
            "has_activity": sid in any_act,
            "oa": len(oa), "pre": len(pre), "scout": scout,
            "pre_before_first_oa": bool(pre and oa and pre[0] < oa[0]),
            "pre_since_last_oa": sum(1 for p in pre if last_oa and p > last_oa),
            "days_since_last_oa": (today - last_oa).days if last_oa else None,
            "work14": any(w >= d14 for w in work) or any(
                d and d >= d14 for d in pre + oa),
        }

    n = len(stu)
    got = sum(1 for s in stu.values() if s["has_activity"])
    print(f"{course} cohort: {n} students; activity data for {got}.\n")

    def bucket(x):
        return "4+" if x >= 4 else str(x)

    print("OA sittings used (activity 'Objective Assessment' events in the course):")
    dist = Counter(bucket(s["oa"]) for s in stu.values() if s["has_activity"])
    for b in ("0", "1", "2", "3", "4+"):
        print(f"  {b:>3}: {dist.get(b, 0):4d}  ({dist.get(b, 0) / max(1, got):.0%})")

    three = [s for s in stu.values() if s["oa"] >= 3]
    print(f"\nAt 3+ OA sittings: {len(three)}")
    if three:
        print(f"  with a pre-A since their last OA: "
              f"{sum(1 for s in three if s['pre_since_last_oa']) }")
        print(f"  active in the course in the last 14 days: "
              f"{sum(1 for s in three if s['work14'])}")
        dl = sorted(s["days_since_last_oa"] for s in three
                    if s["days_since_last_oa"] is not None)
        if dl:
            print(f"  days since last OA: median {dl[len(dl) // 2]}, "
                  f"<=30 days {sum(1 for x in dl if x <= 30)}")

    def by(key, title):
        print(f"\nBy {title} (students with activity data):")
        groups = defaultdict(list)
        for s in stu.values():
            if s["has_activity"]:
                groups[s[key]].append(s)
        print(f"  {'':22} {'n':>4} {'any OA':>7} {'2+ OA':>6} {'3+ OA':>6} "
              f"{'took preA':>9} {'scouts':>6}")
        for g, ss in sorted(groups.items(), key=lambda x: -len(x[1])):
            m = len(ss)
            print(f"  {str(g)[:22]:22} {m:4d} "
                  f"{sum(1 for s in ss if s['oa'] >= 1) / m:7.0%} "
                  f"{sum(1 for s in ss if s['oa'] >= 2) / m:6.0%} "
                  f"{sum(1 for s in ss if s['oa'] >= 3) / m:6.0%} "
                  f"{sum(1 for s in ss if s['pre']) / m:9.0%} "
                  f"{sum(1 for s in ss if s['scout']):6d}")

    by("program", "program")
    by("momentum", "momentum level")
    by("owner", "assigned CI")

    pre_takers = [s for s in stu.values() if s["pre"]]
    print(f"\nPre-assessment: {len(pre_takers)} took it "
          f"({len(pre_takers) / max(1, got):.0%}); scouts (within 3 days of "
          f"entering, no course material first): "
          f"{sum(1 for s in pre_takers if s['scout'])}")
    if pre_takers:
        ps = sorted(s["pre"] for s in pre_takers)
        print(f"  sittings per taker: median {ps[len(ps) // 2]}, max {ps[-1]}")
    oa_takers = [s for s in stu.values() if s["oa"]]
    if oa_takers:
        print(f"  OA takers who took a pre-A first: "
              f"{sum(1 for s in oa_takers if s['pre_before_first_oa'])} of "
              f"{len(oa_takers)}")


if __name__ == "__main__":
    main(sys.argv[1].upper() if len(sys.argv) > 1 else "C949")
