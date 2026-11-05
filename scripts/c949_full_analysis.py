"""C949 full analysis from the course-assessment import (read-only).

Aggregates only — never student values or pre-assessment item TEXT (items
are referred to by code). Inputs: course_assess (OA/pre-A attempts, coaching
reports, engagement, prior/transferred courses), preA_response, l360_cohort.

Important framing: the cohort is students CURRENTLY in the course, so every
recorded OA sitting is a fail (passers leave). The outcome is therefore the
OA SCORE (how close to the cut), not pass/fail. C949 has two OA forms (A/B):
attempt 3 repeats the first form, attempt 4 the second (inferred by order —
the feeds carry no form id).

Usage: python -m scripts.c949_full_analysis   (report also saved to
data_analysis/c949_full_analysis_<date>.md — git-ignored)
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics as stx
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COURSE = "C949"
OUT: list = []


def say(s: str = "") -> None:
    OUT.append(s)
    print(s)


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def d_iso(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def d_us(s):
    try:
        return datetime.strptime(str(s)[:10], "%m/%d/%Y").date()
    except ValueError:
        return None


def corr(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 8:
        return None, len(pts)
    a, b = zip(*pts)
    try:
        return stx.correlation(a, b), len(pts)
    except stx.StatisticsError:
        return None, len(pts)


def summ(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return "n=0"
    return (f"n={len(v):3d} median {stx.median(v):5.1f} mean {stx.fmean(v):5.1f}")


def main() -> None:
    db = (ROOT / "history.db").resolve().as_posix()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cohort = {r["student_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM l360_cohort WHERE course = ?", (COURSE,))}
    S = {}
    for r in conn.execute("SELECT * FROM course_assess WHERE course = ?",
                          (COURSE,)):
        sid = r["student_id"]
        a = json.loads(r["assessments_json"] or "{}") or {}
        oa = a.get("oa") or {}
        fails = sorted((t for t in oa.get("attempts") or []
                        if t.get("status") == "Not Passed"
                        and fnum(t.get("score")) is not None),
                       key=lambda t: (t.get("n") or 0, t.get("result") or ""))
        coach = json.loads(r["coaching_json"] or "[]") or []
        coach = sorted(coach, key=lambda t: d_us(t.get("date")) or date.min)
        S[sid] = {
            "program": (cohort.get(sid) or {}).get("program") or "(blank)",
            "momentum": (cohort.get(sid) or {}).get("momentum") or "(blank)",
            "scores": [fnum(t["score"]) for t in fails],
            "dates": [d_iso(t.get("result")) for t in fails],
            "approvals": Counter(t.get("approval") for t in oa.get("attempts") or []
                                 if not t.get("status")),
            "coach": coach,
            "eng": json.loads(r["engagement_json"] or "{}") or {},
            "hist": json.loads(r["plan_history_json"] or "{}") or {},
            "pre": defaultdict(lambda: {"date": None, "passed": None,
                                        "items": {}, "comp": defaultdict(list)}),
        }
    cut = 74.0
    for r in conn.execute("SELECT * FROM preA_response WHERE course = ?",
                          (COURSE,)):
        s = S.get(r["student_id"])
        if not s:
            continue
        p = s["pre"][r["attempt_n"]]
        p["date"] = d_us(r["attempt_date"])
        p["passed"] = bool(r["passed"])
        p["items"][r["item_code"]] = r["correct"]
        p["comp"][r["competency"]].append(r["correct"])
    conn.close()
    today = date.today()

    say(f"# {COURSE} full analysis — {today.isoformat()}")
    say()
    say(f"Students: {len(S)} (currently enrolled; passers have left the "
        "cohort, so every OA sitting below is a FAIL). OA cut score: "
        f"{cut:.0f}%. Outcome measure: OA score.")

    # A. Failed sittings + approvals
    say()
    say("## A. Failed OA sittings per student")
    nf = Counter(min(len(s["scores"]), 5) for s in S.values())
    for k in range(6):
        say(f"  {k if k < 5 else '5+'} fails: {nf.get(k, 0)}")
    at3 = [s for s in S.values() if len(s["scores"]) >= 3]
    say(f"  3+ fails: {len(at3)}. Their open approval records: "
        f"{dict(sum((s['approvals'] for s in at3), Counter()))}")

    # B. Scores by attempt
    say()
    say("## B. OA score by attempt (all failed)")
    for i in range(5):
        v = [s["scores"][i] for s in S.values() if len(s["scores"]) > i]
        if v:
            near = sum(1 for x in v if x >= cut - 5)
            say(f"  attempt {i + 1}: {summ(v)}  within 5 pts of cut: "
                f"{near} ({near / len(v):.0%})")
    for a_, b_, label in ((0, 1, "1→2 (other form)"), (1, 2, "2→3"),
                          (0, 2, "1→3 (SAME form)"), (1, 3, "2→4 (SAME form)")):
        d = [s["scores"][b_] - s["scores"][a_] for s in S.values()
             if len(s["scores"]) > b_]
        if d:
            say(f"  change {label}: {summ(d)}  improved: "
                f"{sum(1 for x in d if x > 0)}/{len(d)}")

    # C. Competencies on failed attempts. NOTE: the coaching report's
    # "assessmentPercentage" is each competency's WEIGHT on the exam (sums to
    # 100), not the student's score — the student result is the RATING.
    say()
    say("## C. Competency ratings on failed OA attempts (coaching reports)")
    titles, weight = {}, {}
    rate = defaultdict(lambda: defaultdict(Counter))
    ok_n = Counter()
    for s in S.values():
        for i, t in enumerate(s["coach"]):
            k = 0
            for c in t.get("competencies") or []:
                titles.setdefault(c["number"], c.get("title") or "")
                weight.setdefault(c["number"], c.get("pct"))
                rate[c["number"]][min(i, 2)][c.get("rating") or "?"] += 1
                k += c.get("rating") in ("COMPETENT", "EXEMPLARY")
            ok_n[k] += 1
    for num in sorted(rate):
        say(f"  {num} {titles.get(num, '')} (exam weight {weight.get(num)}%):")
        for i, cn in sorted(rate[num].items()):
            n = sum(cn.values())
            ok = cn.get("COMPETENT", 0) + cn.get("EXEMPLARY", 0)
            say(f"    attempt {i + 1 if i < 2 else '3+'}: competent {ok}/{n} "
                f"({ok / n:.0%}), approaching {cn.get('APPROACHING_COMPETENCY', 0)}, "
                f"unsatisfactory {cn.get('UNSATISFACTORY', 0)}")
    say(f"  competencies rated competent per failed attempt: "
        f"{dict(sorted(ok_n.items()))}")
    still = Counter()
    for s in at3:
        if s["coach"]:
            for c in s["coach"][-1].get("competencies") or []:
                if c.get("rating") not in ("COMPETENT", "EXEMPLARY"):
                    still[c["number"]] += 1
    say(f"  3+ fail group — NOT yet competent on their latest attempt: "
        f"{dict(still)} (of {sum(1 for s in at3 if s['coach'])} with reports)")
    comp_by_att = rate

    # D. Topics missed + repeat misses
    say()
    say("## D. Topics flagged on failed attempts (coaching 'missed' topic codes)")
    topic_n = Counter()
    for s in S.values():
        for t in s["coach"]:
            topic_n.update({m["item"] for m in t.get("missed") or []})
    att_total = sum(len(s["coach"]) for s in S.values())
    for code, n in topic_n.most_common(16):
        say(f"  {code:16} flagged on {n:3d} of {att_total} attempts "
            f"({n / max(1, att_total):.0%})")
    same = other = same_n = other_n = 0
    for s in S.values():
        sets = [{m["item"] for m in t.get("missed") or []} for t in s["coach"]]
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                if not sets[i]:
                    continue
                again = len(sets[i] & sets[j]) / len(sets[i])
                if (j - i) % 2 == 0:
                    same += again
                    same_n += 1
                else:
                    other += again
                    other_n += 1
    if same_n or other_n:
        say(f"  topics flagged AGAIN on a later attempt: same form "
            f"{same / max(1, same_n):.0%} (pairs={same_n}), other form "
            f"{other / max(1, other_n):.0%} (pairs={other_n})")

    # E. Time on the OA
    say()
    say("## E. Time spent on the OA vs score")
    mins, sc = [], []
    for s in S.values():
        for t, x in zip(s["coach"], s["scores"]):
            mins.append(t.get("minutes"))
            sc.append(x)
    r, n = corr(mins, sc)
    say(f"  minutes vs score: r={r:.2f} (n={n})" if r is not None else
        "  not enough data")
    say(f"  minutes on failed attempts: {summ(mins)}")

    # F. Pre-assessment vs OA
    say()
    say("## F. Pre-assessment vs the first OA score")

    def pre_pct(p):
        v = list(p["items"].values())
        return 100 * sum(v) / len(v) if v else None

    first_pre, first_oa, last_pre_before, n_pre_before, ever = [], [], [], [], []
    for s in S.values():
        if not s["pre"]:
            continue
        atts = sorted(s["pre"].items())
        ever.append(pre_pct(atts[0][1]))
        if not s["scores"]:
            continue
        d0 = s["dates"][0]
        before = [p for _, p in atts if p["date"] and d0 and p["date"] <= d0]
        first_pre.append(pre_pct(atts[0][1]))
        first_oa.append(s["scores"][0])
        last_pre_before.append(pre_pct(before[-1]) if before else None)
        n_pre_before.append(len(before))
    say(f"  first pre-A sitting % correct (all takers): {summ(ever)}")
    for label, xs in (("FIRST pre-A % → first OA score", first_pre),
                      ("LAST pre-A % before the OA → first OA score",
                       last_pre_before),
                      ("# pre-A sittings before the OA → first OA score",
                       n_pre_before)):
        r, n = corr(xs, first_oa)
        say(f"  {label}: r={r:.2f} (n={n})" if r is not None
            else f"  {label}: not enough data")
    hi = [(p, o) for p, o in zip(last_pre_before, first_oa) if p is not None]
    if hi:
        good = [o for p, o in hi if p >= 74]
        say(f"  scored >=74% on their last pre-A, then failed the OA: "
            f"{len(good)} of {len(hi)}; their first OA score {summ(good)}")
    # Competency-level: first pre-A vs first OA competency
    for num in sorted(comp_by_att):
        xs, ys = [], []
        for s in S.values():
            if s["pre"] and s["coach"]:
                p0 = sorted(s["pre"].items())[0][1]["comp"].get(num)
                c0 = next((100.0 * (c.get("rating") in ("COMPETENT", "EXEMPLARY"))
                           for c in s["coach"][0].get("competencies") or []
                           if c["number"] == num), None)
                if p0 and c0 is not None:
                    xs.append(100 * sum(p0) / len(p0))
                    ys.append(c0)
        r, n = corr(xs, ys)
        if r is not None:
            say(f"  {num}: first pre-A % on this competency vs rated competent "
                f"on the first OA: r={r:.2f} (n={n}); pre-A mean "
                f"{stx.fmean(xs):.0f}%, competent on OA {stx.fmean(ys):.0f}%")

    # G. Items on the FIRST pre-A sitting
    say()
    say("## G. Pre-A items on the FIRST sitting (by item code; no item text)")
    item_c = defaultdict(list)
    item_oa = defaultdict(list)
    for s in S.values():
        if not s["pre"]:
            continue
        p0 = sorted(s["pre"].items())[0][1]
        for code, ok in p0["items"].items():
            item_c[code].append(ok)
            if s["scores"]:
                item_oa[code].append((ok, s["scores"][0]))
    hard = sorted(((stx.fmean(v), k, len(v)) for k, v in item_c.items()
                   if len(v) >= 30))
    say("  hardest items (first sitting % correct):")
    for m, k, n in hard[:8]:
        say(f"    {k:40} {m:4.0%} (n={n})")
    pred = []
    for k, v in item_oa.items():
        if len(v) < 30:
            continue
        r, n = corr([float(a) for a, _ in v], [b for _, b in v])
        if r is not None:
            pred.append((r, k, n))
    say("  items whose first-sitting result best tracks the first OA score:")
    for r, k, n in sorted(pred, reverse=True)[:8]:
        say(f"    {k:40} r={r:.2f} (n={n})")

    # H. Engagement + timing
    say()
    say("## H. Course engagement and time to first OA vs first OA score")
    ev, sc, days = [], [], []
    for s in S.values():
        if not s["scores"]:
            continue
        ev.append(s["eng"].get("engaged_views"))
        sc.append(s["scores"][0])
        st = d_iso(s["eng"].get("start_date"))
        days.append((s["dates"][0] - st).days if st and s["dates"][0] else None)
    r, n = corr(ev, sc)
    say(f"  engaged course views vs first OA score: r={r:.2f} (n={n})"
        if r is not None else "  engaged views: not enough data")
    r, n = corr(days, sc)
    say(f"  days from course start to first OA vs score: r={r:.2f} (n={n})"
        if r is not None else "  timing: not enough data")
    for lo, hi_, lab in ((0, 30, "<30 days"), (30, 60, "30-59"), (60, 120, "60-119"),
                         (120, 10_000, "120+")):
        v = [x for x, d in zip(sc, days) if d is not None and lo <= d < hi_]
        if v:
            say(f"    {lab:9} first OA score {summ(v)}")

    # I. Program + prior/transfer courses
    say()
    say("## I. Program and prior / transferred courses vs first OA score")
    byp = defaultdict(list)
    for s in S.values():
        if s["scores"]:
            byp[s["program"]].append(s["scores"][0])
    for p, v in sorted(byp.items(), key=lambda x: -len(x[1])):
        say(f"  {p:10} {summ(v)}")
    have_hist = [s for s in S.values() if s["hist"] and s["scores"]]
    say(f"  students with OA scores AND plan history: {len(have_hist)}")
    cnt = Counter()
    for s in have_hist:
        codes = {c["code"] for c in s["hist"].get("completed") or []} | set(
            s["hist"].get("transfer") or [])
        s["_prior"] = codes
        cnt.update(codes)
    rows = []
    for code, n in cnt.items():
        yes = [s["scores"][0] for s in have_hist if code in s["_prior"]]
        no = [s["scores"][0] for s in have_hist if code not in s["_prior"]]
        if len(yes) >= 12 and len(no) >= 12:
            rows.append((stx.fmean(yes) - stx.fmean(no), code, len(yes),
                         stx.fmean(yes), len(no), stx.fmean(no)))
    say("  courses completed or transferred before (n>=12 each way), "
        "first OA score with vs without:")
    for gap, code, ny, my, nn, mn in sorted(rows, reverse=True)[:12]:
        say(f"    {code:6} with n={ny:3d} {my:5.1f}  without n={nn:3d} "
            f"{mn:5.1f}  diff {gap:+.1f}")
    if len(rows) > 12:
        say("    …lowest:")
        for gap, code, ny, my, nn, mn in sorted(rows)[:5]:
            say(f"    {code:6} with n={ny:3d} {my:5.1f}  without n={nn:3d} "
                f"{mn:5.1f}  diff {gap:+.1f}")
    tr = [len(s["hist"].get("transfer") or []) for s in have_hist]
    r, n = corr(tr, [s["scores"][0] for s in have_hist])
    if r is not None:
        say(f"  number of transferred courses vs first OA score: r={r:.2f} (n={n})")

    out = ROOT / "data_analysis" / f"c949_full_analysis_{today:%Y%m%d}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(OUT) + "\n", encoding="utf-8")
    print(f"\n(saved {out})")


if __name__ == "__main__":
    main()
