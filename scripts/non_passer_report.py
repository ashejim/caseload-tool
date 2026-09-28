"""Snapshot-based pass / non-pass report for a date window.

WHY THIS EXISTS: the "passed in the last 30 days" archive captures passers well
but badly under-captures non-passers, so an archive-only pass rate is inflated
(e.g. it read ~94% when WGU's official figure was 91.7%). Non-passers don't
cleanly "resolve" into that archive — most take an IC extension, roll to a new
term, or just term-end quietly and stay on the caseload. The local SNAPSHOT
history keeps every student we ever saw, so it can classify the non-passers the
archive drops. Built on ``history.term_end_outcomes`` (see its docstring).

Each (student, course) is dated by WHEN IT RESOLVED and classified:
  passed            — pass on record; dated pass_date.
  notpass_term_end  — reached their effective end (IC else term end) with no pass
                      on record; dated by that effective end. This is the
                      defensible non-pass population — it reproduces WGU's rate.
  early_exit        — fell off the caseload well before their term end (or an
                      archived non-pass with a still-future term end). AMBIGUOUS:
                      a true dropout, a reassignment to another CI, or a pass we
                      failed to capture. Reported SEPARATELY and kept OUT of the
                      headline rate — folding it in over-counts non-passers.

The headline **term-end rate** = passed / (passed + notpass_term_end).

Output (to ``data_analysis/`` — gitignored; contains student PII in PLAINTEXT,
FERPA — store securely):
  - ``nonpass_term_end_<from>_<to>.csv``   — the term-end non-passers.
  - ``nonpass_early_exit_<from>_<to>.csv``  — the ambiguous early exits, to
    review (confirm dropout vs reassignment vs uncaptured pass).

Read-only against history.db (safe while the app is open).

Run:  .venv\\Scripts\\python.exe scripts\\non_passer_report.py --from 2026-07-03 --to 2026-08-31
      (add --courses C769,C964 to restrict; omit dates for all-time)
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import PROJECT_ROOT  # noqa: E402
from src import history  # noqa: E402

_DETAIL_COLS = ["student_id", "course_code", "name", "resolved_on",
                "effective_end", "last_seen", "momentum", "latest_task_status"]


def _pct(v):
    return f"{v:.1f}%" if v is not None else "-"


def _write_detail(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_DETAIL_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in _DETAIL_COLS})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="date_from", default=None,
                    help="window start (YYYY-MM-DD, by resolution date)")
    ap.add_argument("--to", dest="date_to", default=None,
                    help="window end (YYYY-MM-DD, by resolution date)")
    ap.add_argument("--courses", default=None,
                    help="comma-separated course codes to restrict to")
    args = ap.parse_args(argv)
    courses = ([c.strip() for c in args.courses.split(",") if c.strip()]
               if args.courses else None)

    r = history.term_end_outcomes(
        date_from=args.date_from, date_to=args.date_to, courses=courses)

    span = (f"{r['date_from'] or 'start'} .. {r['date_to'] or 'today'}")
    print(f"\nSnapshot-based pass/non-pass - resolved in {span}"
          f"  (data as of {r['asof']})")
    if courses:
        print(f"  courses: {', '.join(courses)}")
    print("-" * 64)
    print(f"  passed .............. {r['passed']}")
    print(f"  not passed @ term end {r['notpass_term_end']}")
    print(f"  TERM-END PASS RATE .. {_pct(r['term_end_rate'])}"
          f"   ({r['passed']}/{r['passed'] + r['notpass_term_end']})")
    print("-" * 64)
    print(f"  early exits (ambiguous, NOT in the rate above): {r['early_exit']}")
    print(f"    -> rate if all early exits were non-passes: "
          f"{_pct(r['rate_incl_early'])}  (a FLOOR - over-counts)")
    print(f"  still in progress (excluded): {r['in_progress_excluded']}")
    print("-" * 64)
    print("  Note: 'early exits' left the caseload before their term end and "
          "can't be\n  told apart (dropout vs reassignment vs uncaptured pass) "
          "from snapshots\n  alone - review the detail CSV before counting them.")

    out_dir = Path(PROJECT_ROOT) / "data_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{r['date_from'] or 'all'}_{r['date_to'] or 'now'}".replace("-", "")
    nt_path = out_dir / f"nonpass_term_end_{tag}.csv"
    ex_path = out_dir / f"nonpass_early_exit_{tag}.csv"
    _write_detail(r["students"]["notpass_term_end"], nt_path)
    _write_detail(r["students"]["early_exit"], ex_path)
    print(f"\n  detail (PII) -> {nt_path.name}, {ex_path.name}  (in data_analysis/)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
