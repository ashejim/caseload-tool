"""C949 cohort analysis — what distinguishes students who needed a 2nd+ OA
sitting? Read-only (history.db via a mode=ro URI); prints AGGREGATES only.

Data: the saved Learner360 cohort (l360_cohort: program, momentum, CI, term
end) + 12 months of Learner360 academic activity for those students across
ALL their courses (l360_activity).

Outcome: among students whose FIRST C949 OA sitting was >= 30 days ago,
"repeat" = 2+ C949 OA sittings. (OA 'completed' events include failed
sittings; the activity window is 12 months, so older sittings are missed.)

Usage: python -m scripts.c949_analysis [COURSE]   (report also saved to
data_analysis/<course>_analysis_<date>.md — git-ignored)
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = {"Course of Study Link", "Learning Resource", "panopto", "wgulibrary",
        "course.start.learning", "Complete Course Activity Material",
        "Complete Course Topic Materials"}
DONE_RE = re.compile(r"^(.*?) completed for ([A-Z]{1,4}\d{2,4})\b")
OUT: list = []


def say(s: str = "") -> None:
    OUT.append(s)
    print(s)


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def pct(a, b):
    return f"{a / b:.0%}" if b else "—"


def rate_line(label, group):
    n = len(group)
    r = sum(1 for s in group if s["repeat"])
    return f"  {label:42} n={n:4d}  repeat {r:3d} ({pct(r, n)})"


def main(course: str = "C949") -> None:
    db = (ROOT / "history.db").resolve().as_posix()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cohort = {r["student_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM l360_cohort WHERE course = ?", (course,))}
    ev = defaultdict(list)
    titles = {}
    for r in conn.execute(
            "SELECT student_id, activity_date, category, type, context, "
            "course_code, occurrences FROM l360_activity"):
        sid = (r["student_id"] or "").lstrip("0")
        if sid not in cohort:
            continue
        d = _d(r["activity_date"])
        if not d:
            continue
        ev[sid].append((d, r["category"], r["type"], r["context"] or "",
                        r["course_code"] or "", int(r["occurrences"] or 1)))
        m = DONE_RE.match(r["context"] or "")
        if m and r["type"] in ("Objective Assessment", "Performance Assessment"):
            titles.setdefault(m.group(2), m.group(1))
    conn.close()
    today = date.today()

    stu = {}
    for sid, k in cohort.items():
        rows = sorted(ev.get(sid, []))
        mine = [r for r in rows if r[4] == course]
        oa = [r[0] for r in mine if r[2] == "Objective Assessment"]
        pre = [r[0] for r in mine if r[2] == "Objective Preassessment"
               for _ in range(r[5])]
        enter = min((r[0] for r in mine if r[2] in ("Term Enrollment",
                                                     "Activate Course Resources")),
                    default=None)
        work = [r[0] for r in mine if r[2] in WORK
                and "provisioned" not in r[3]]
        first_oa = oa[0] if oa else None
        cut = first_oa or today
        # Other courses finished BEFORE the first C949 OA (PA "completed" =
        # passed; OA "completed" = a sitting, may be a fail).
        pa_done = {r[4] for r in rows if r[4] and r[4] != course
                   and r[2] == "Performance Assessment" and r[0] < cut}
        oa_taken = {r[4] for r in rows if r[4] and r[4] != course
                    and r[2] == "Objective Assessment" and r[0] < cut}
        fac = sum(r[5] for r in rows if r[2] == "Academic Discussion with Faculty"
                  and r[0] < cut and (r[4] in ("", course)))
        cohort_ev = sum(r[5] for r in rows if r[2] == "Cohort Event Attendance"
                        and r[0] < cut and course in r[3])
        cpt = any(r[2] == "Course Planning Tool" and course in r[3] for r in rows)
        stu[sid] = {
            "program": k["program"] or "(blank)",
            "momentum": k["momentum"] or "(blank)",
            "has_act": bool(rows),
            "oa_n": len(oa), "first_oa": first_oa,
            "eligible": bool(first_oa and (today - first_oa).days >= 30),
            "repeat": len(oa) >= 2,
            "pre_before": sum(1 for p in pre if first_oa and p < first_oa),
            "pre_total": len(pre),
            "scout": bool(pre and enter and (pre[0] - enter).days <= 3
                          and not any(w < pre[0] for w in work)),
            "days_to_oa": (first_oa - enter).days if first_oa and enter else None,
            "work_days_before": len({w for w in work if first_oa and w < first_oa}),
            "work_days_30": len({w for w in work if first_oa
                                 and first_oa - timedelta(days=30) <= w < first_oa}),
            "fac_before": fac, "cohort_before": cohort_ev, "cpt": cpt,
            "prior_pa": pa_done, "prior_oa": oa_taken,
            "term_end": _d(k["term_end"]),
        }

    allw = [s for s in stu.values() if s["has_act"]]
    el = [s for s in allw if s["eligible"]]
    say(f"# {course} cohort analysis — {today.isoformat()}")
    say()
    say(f"Cohort: {len(stu)} students (WST - CS Apps, currently enrolled); "
        f"activity data for {len(allw)}.")
    say(f"Sat the OA at least once (12 mo): "
        f"{sum(1 for s in allw if s['oa_n'])}; analysed (first OA >= 30 days "
        f"ago): {len(el)}, of whom {sum(s['repeat'] for s in el)} "
        f"({pct(sum(s['repeat'] for s in el), len(el))}) needed a 2nd+ sitting.")
    say("All rates below = share of the analysed OA takers who needed 2+ "
        "sittings.")

    def split(title, key, buckets):
        say()
        say(f"## {title}")
        for label, fn in buckets:
            g = [s for s in el if fn(s[key])]
            if g:
                say(rate_line(label, g))

    split("Pre-assessment sittings BEFORE the first OA", "pre_before", [
        ("none", lambda x: x == 0), ("1", lambda x: x == 1),
        ("2", lambda x: x == 2), ("3-4", lambda x: 3 <= x <= 4),
        ("5+", lambda x: x >= 5)])
    split("Days from entering the course to the first OA", "days_to_oa", [
        ("< 14 days", lambda x: x is not None and x < 14),
        ("14-29", lambda x: x is not None and 14 <= x < 30),
        ("30-59", lambda x: x is not None and 30 <= x < 60),
        ("60-119", lambda x: x is not None and 60 <= x < 120),
        ("120+", lambda x: x is not None and x >= 120),
        ("unknown (entered > 12 mo ago)", lambda x: x is None)])
    split("Days with C949 course-material activity before the first OA",
          "work_days_before", [
              ("0", lambda x: x == 0), ("1-3", lambda x: 1 <= x <= 3),
              ("4-10", lambda x: 4 <= x <= 10), ("11-25", lambda x: 11 <= x <= 25),
              ("26+", lambda x: x >= 26)])
    split("…of which in the 30 days before the first OA", "work_days_30", [
        ("0", lambda x: x == 0), ("1-3", lambda x: 1 <= x <= 3),
        ("4-10", lambda x: 4 <= x <= 10), ("11+", lambda x: x >= 11)])
    split("Scout (pre-A within 3 days of entering, before any material)",
          "scout", [("scout", lambda x: x), ("not a scout", lambda x: not x)])
    split("Faculty discussions (C949 or untagged) before the first OA",
          "fac_before", [("none", lambda x: x == 0), ("1-2", lambda x: 1 <= x <= 2),
                         ("3+", lambda x: x >= 3)])
    split("C949 cohort-session attendance before the first OA",
          "cohort_before", [("none", lambda x: x == 0), ("1+", lambda x: x >= 1)])
    split("Used the Course Planning Tool for C949", "cpt",
          [("yes", lambda x: x), ("no", lambda x: not x)])

    say()
    say("## Program")
    for p, n in Counter(s["program"] for s in el).most_common():
        say(rate_line(p, [s for s in el if s["program"] == p]))
    say()
    say("## Momentum level (current)")
    for p, n in Counter(s["momentum"] for s in el).most_common():
        say(rate_line(p, [s for s in el if s["momentum"] == p]))

    # Prior courses: finished before the first C949 OA, enough students in
    # both groups to compare.
    say()
    say("## Other courses finished before the first C949 OA (12-month window)")
    say("  (PA 'completed' = passed; OA = sat. Courses with >= 12 students "
        "each way; sorted by gap.)")
    rows_out = []
    for kind, key in (("PA passed", "prior_pa"), ("OA sat", "prior_oa")):
        cnt = Counter(c for s in el for c in s[key])
        for c, n in cnt.items():
            yes = [s for s in el if c in s[key]]
            no = [s for s in el if c not in s[key]]
            if len(yes) < 12 or len(no) < 12:
                continue
            ry = sum(s["repeat"] for s in yes) / len(yes)
            rn = sum(s["repeat"] for s in no) / len(no)
            rows_out.append((ry - rn, c, kind, len(yes), ry, len(no), rn))
    for gap, c, kind, ny, ry, nn, rn in sorted(rows_out):
        say(f"  {c:5} {titles.get(c, '')[:34]:34} {kind:9} with n={ny:3d} "
            f"{ry:4.0%}  without n={nn:3d} {rn:4.0%}  gap {gap:+.0%}")

    # Who is at risk NOW (counts only).
    say()
    say("## Now")
    at3 = [s for s in allw if s["oa_n"] >= 3]
    at2 = [s for s in allw if s["oa_n"] == 2]
    soon = today + timedelta(days=56)
    say(f"  3+ sittings: {len(at3)} (term ends within 8 weeks: "
        f"{sum(1 for s in at3 if s['term_end'] and s['term_end'] <= soon)})")
    say(f"  exactly 2 sittings: {len(at2)} (term ends within 8 weeks: "
        f"{sum(1 for s in at2 if s['term_end'] and s['term_end'] <= soon)})")
    never = [s for s in allw if not s["oa_n"]]
    say(f"  no OA yet: {len(never)}; of those, took the pre-A: "
        f"{sum(1 for s in never if s['pre_total'])}; term ends within 8 weeks: "
        f"{sum(1 for s in never if s['term_end'] and s['term_end'] <= soon)}")

    out = ROOT / "data_analysis" / f"{course.lower()}_analysis_{today:%Y%m%d}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(OUT) + "\n", encoding="utf-8")
    print(f"\n(saved {out})")


if __name__ == "__main__":
    main(sys.argv[1].upper() if len(sys.argv) > 1 else "C949")
