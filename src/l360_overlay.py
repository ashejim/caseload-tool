"""CaseloadNotes overlay for Learner360 — data side (Stage 2, option A).

The app's own browser (Edge/Chromium, driven by browser_worker) shows
Learner360; an injected script (OVERLAY_JS in l360_overlay_js) decorates the
caseload list with CaseloadNotes badges. This module builds the per-student
SNAPSHOT the worker pushes into the page (push model — see the design doc
docs_private/LEARNER360_STAGE2_DESIGN.md, "Bridge mechanics").

Pure: no browser, no DB. Read-only by design (P0 shows data; changes nothing).
"""
from __future__ import annotations

import re
from typing import Optional

# Task status labels as the caseload rows carry them (TASK_STATE_LABELS).
_TASK_KIND = {"passed": "pass", "returned": "return", "submitted": "pending",
              "in process": "pending", "pending": "pending"}


def _num(v) -> Optional[float]:
    try:
        return float(v) if str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _nsid(s) -> str:
    return str(s or "").strip().lstrip("0")


def _next_step(row: dict, steps: list) -> str:
    """Label of the first 'due' success-path step for this row's course (else
    the first 'blocked'); '' when none / no path. ``steps`` = [(col, label)]."""
    for want in ("due", "blocked"):
        for col, label in steps:
            if row.get(col) == want:
                return label + (" (blocked)" if want == "blocked" else "")
    return ""


# Caseload-browser (Any-Course scan) fields offered to the panel as "CB:<f>"
# — link / tooltip / styling / bookkeeping fields are skipped.
_CB_SKIP = re.compile(r"(Url|hoverText|cssStyle|Yesterday)$|^(uniqueKey|"
                      r"LoggedInUser|CaseloadTotalRows|contactterm|"
                      r"StudentAcademicCourseId|contactID|PIDM|MentorId|"
                      r"CourseMentorID|CourseMentorUserID|Birthday)$")


def _human(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))
    return re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s).strip()


def roster_fields(roster_rows, student_ids=None) -> dict:
    """{nsid: {course: {field: value}}} from the caseload-browser scan rows,
    for ``student_ids`` (all when None), useful fields only."""
    want = ({_nsid(s) for s in student_ids} - {""}) if student_ids is not None \
        else None
    out: dict = {}
    for r in roster_rows or []:
        sid = _nsid(r.get("StudentID"))
        course = str(r.get("CourseCode") or "").strip().upper()
        if not sid or not course or (want is not None and sid not in want):
            continue
        out.setdefault(sid, {})[course] = {
            k: v for k, v in r.items()
            if k and not _CB_SKIP.search(k) and str(v or "").strip()}
    return out


def _disp(key: str) -> str:
    from src import caseload_csv
    if str(key).startswith("CB:"):
        return "CB · " + _human(key[3:])
    return caseload_csv.display_for_column(key)


def field_catalog(rows: list, extras: Optional[dict] = None) -> list:
    """[[key, display name]] the panel's ⚙ Fields picker offers: every
    caseload column (internal, per-task facet and success-path helpers
    excluded) + every caseload-browser field seen ("CB:<field>"), sorted by
    display name."""
    from src import caseload_filter
    keys = []
    if rows:
        keys = [k for k in rows[0].keys()
                if k and not str(k).startswith("_") and not str(k).startswith("sp:")
                and not caseload_filter.is_task_facet_col(k)]
    cb = set()
    for per in ((extras or {}).get("roster") or {}).values():
        for flds in per.values():
            cb.update(flds)
    keys += ["CB:" + f for f in sorted(cb)]
    return sorted(([k, _disp(k)] for k in keys), key=lambda kd: kd[1].lower())


# Extra COLUMNS for Learner360's caseload list (⚙ Columns): these built-ins
# (from the assessment summary) + any field_catalog key. Values go to the
# page as [text, kind, tooltip]; kind ∈ pass / fail / mid / "" colours it.
LIST_BUILTINS = [["@pron", "Pronouns"], ["@ea", "EA (open Essential Action)"],
                 ["@oa", "OA attempts"], ["@oa1", "OA 1"], ["@oa2", "OA 2"],
                 ["@oa3", "OA 3"], ["@oa4", "OA 4"],
                 ["@weak", "Weakest competency"], ["@pre", "Pre-A"]]
LIST_MAX = 12
# Learner360's OWN columns can be part of the layout as "L360:<field>" (the
# field = its cdk-column-<field> class, e.g. preferredName). The page moves a
# copy of that column into place (header label + sorting from Learner360).
NATIVE_RE = re.compile(r"^L360:[A-Za-z][A-Za-z0-9_]{0,40}$")
# The recommended layout (new installs; ⚙ Columns ▸ Reset to recommended):
# after Name — preferred name, pronouns, ID, active course, last ACI contact,
# pre-assessment, OA attempts. Learner360's other columns are hidden.
DEFAULT_LIST_COLS = ["L360:preferredName", "@pron", "L360:studentId",
                     "L360:courses", "MyCourseContact", "@pre", "@oa"]
# Caseload columns that only fill in with a background feature (→ the hover
# on an empty cell names it; src/features.py keys).
_FEATURE_COLS = {
    "activity": {"DaysSinceCourseActivity", "CourseActivityDays14",
                 "DaysSinceAnyActivity", "PASubmissions", "PAReturns",
                 "PAPasses", "OAAttempts", "PreAssessments", "ApprovalRequests",
                 "FacultyDiscussions30", "Signals", "SigNotOpened",
                 "SigWorkingNoSubmit", "SigActiveElsewhere", "SigFinishedOther",
                 "SigFinishedDays"},
    "plans": {"TermCoursesLeft", "OtherCoursesLeft", "LastCourseInTerm",
              "NextPlannedCourse", "TermCUsLeft", "PlanWeeksLeft",
              "DegreePctComplete", "ProgramMentorEmail"},
}
_FEATURE_TITLE = {"course_scan": "Course scan", "course_watch": "Course watch",
                  "activity": "Academic activity", "plans": "Degree plans"}


def _turn_on(feat: str) -> str:
    return (f"turn on “{_FEATURE_TITLE[feat]}” in ❔ Help ▸ Background "
            "features…")


def why_empty(key: str, a: Optional[dict], course: str,
              feats: dict) -> str:
    """Hover text for an EMPTY list cell of one of my students: why there's
    nothing, and what would fill it. ``feats`` = {course_scan, course_watch,
    activity, plans: bool, types: {course: {oa, pa}}}."""
    if key == "@ea":
        return "No open Essential Action for this student."
    if key == "@pron":
        return ("Pronouns not set — click to set them (saved in CaseloadNotes; "
                "the Caseload Tool shows them too).")
    if key in ("@oa", "@oa1", "@oa2", "@oa3", "@oa4", "@pre", "@weak"):
        ty = (feats.get("types") or {}).get(course) or {}
        if ty.get("oa") is False:
            return f"{course} has no objective assessment (it's performance-assessed)."
        if key == "@weak":
            return ("No coaching report stored — " + _turn_on("course_watch")
                    if not feats.get("course_watch") else
                    "No coaching report yet (no OA attempt with a report).")
        if not a:
            if not (feats.get("course_scan") or feats.get("course_watch")):
                return (f"No assessment data for {course} yet — "
                        + _turn_on("course_scan") + " (attempts) or Course "
                        "watch (scores).")
            return (f"No {'pre-assessment' if key == '@pre' else 'OA'} "
                    f"attempts recorded for {course}.")
        if key.startswith("@oa") and key[3:].isdigit():
            return f"No attempt {key[3:]} yet."
        return (f"No {'pre-assessment' if key == '@pre' else 'OA'} attempts "
                f"recorded for {course}.")
    if key.startswith("CB:"):
        return ("Caseload-browser fields come from the course scan — "
                + _turn_on("course_scan") if not feats.get("course_scan")
                else "Blank in the course scan for this student.")
    for feat, keys in _FEATURE_COLS.items():
        if key in keys and not feats.get(feat):
            return "Filled by a background import — " + _turn_on(feat)
    return "Blank in your caseload data for this student."

_RATING_RANK = {"unsat": 1, "appro": 2, "compe": 3, "exemp": 4}


def list_catalog(rows: list, extras: Optional[dict] = None) -> list:
    """[[key, label]] the list's ⚙ Columns picker offers (built-ins first)."""
    return [list(b) for b in LIST_BUILTINS] + field_catalog(rows, extras)


def _sit_tip(t: dict) -> str:
    return (f"OA {t.get('n')} · {t.get('date') or '?'}"
            + (f" · score {t['score']}" if t.get("score") is not None else "")
            + (" · passed" if t.get("passed") else ""))


def _ema_url(full_sid: str, ema: dict) -> str:
    from src.ema_links import build_ema_url
    if not (full_sid and ema.get("course_id") and ema.get("task_id")):
        return ""
    return build_ema_url(full_sid, str(ema["course_id"]), str(ema["task_id"]))


def safe_url(u) -> str:
    """``u`` if it's an https link on a wgu.edu host (EMA / coaching / course
    pages), else "" — only these become clickable in Learner360."""
    from urllib.parse import urlsplit
    try:
        sp = urlsplit(str(u or ""))
    except ValueError:
        return ""
    host = (sp.hostname or "").lower()
    return str(u) if sp.scheme == "https" and (
        host == "wgu.edu" or host.endswith(".wgu.edu")) else ""


def _assess_url(a: Optional[dict], key: str) -> str:
    """Where clicking an assessment cell goes: the coaching report (OA cells)
    or the pre-assessment report (Pre-A), else the course page."""
    links = (a or {}).get("links") or {}
    want = "preA" if key == "@pre" else "coaching"
    return safe_url(links.get(want)) or safe_url(links.get("cos"))


def _list_value(key: str, row: Optional[dict], roster: dict, sid: str,
                course: str, a: Optional[dict]):
    """[text, kind, tooltip(, url)] for one list column, or None when empty.
    Assessment cells carry a 4th element — the report they open."""
    v = _list_value_raw(key, row, roster, sid, course, a)
    if v and key in ("@oa", "@oa1", "@oa2", "@oa3", "@oa4", "@weak", "@pre"):
        url = _assess_url(a, key)
        if url:
            links = (a or {}).get("links") or {}
            what = ("pre-assessment report" if key == "@pre" and links.get("preA")
                    else "coaching report" if key != "@pre" and links.get("coaching")
                    else "course page")
            tip = (v[2] + "\n" if v[2] else "") + f"Click to open the {what}."
            v = [v[0], v[1], tip, url]
    return v


def _list_value_raw(key: str, row: Optional[dict], roster: dict, sid: str,
                    course: str, a: Optional[dict]):
    """[text, kind, tooltip] for one list column, or None when empty."""
    if key == "@pron":
        v = str((row or {}).get("Pronouns") or "").strip()
        return [v, "", "Click to change"] if v else None
    if key == "@ea":
        v = str((row or {}).get("EssentialAction") or "").strip()
        # 4th element "app:ea" = the page asks the app to open the EA
        # dashboard (approve / reject), not a URL.
        return [v[:40], "mid", f"Open Essential Action: {v}\nClick to open the "
                "EA dashboard (approve / reject).", "app:ea"] if v else None
    if NATIVE_RE.match(key):
        return None                          # the page mirrors Learner360's own
    if not key.startswith("@"):
        v = _field_value(key, row, roster, sid, course)
        return [v[:80], "", v if len(v) > 80 else ""] if v else None
    a = a or {}
    oa = a.get("oa") or {}
    sits = oa.get("sittings") or []
    cut = a.get("cut")
    if key == "@oa":
        if not (oa.get("used") or oa.get("passed")):
            return None
        tip = "\n".join(_sit_tip(t) for t in sits) + (f"\ncut {cut}" if cut else "")
        nx = oa.get("next") or {}
        if nx.get("scheduled"):
            tip += f"\nnext scheduled {nx['scheduled']}"
        if oa.get("passed"):
            return ["✓ passed", "pass", tip.strip()]
        last = next((t["score"] for t in reversed(sits)
                     if t.get("score") is not None), None)
        txt = f"{oa['used']}/{oa.get('max') or 4}" + (f" · {last}" if last is not None else "")
        return [txt, "fail" if oa["used"] >= 3 else "mid", tip.strip()]
    if key.startswith("@oa") and key[3:].isdigit():
        n = int(key[3:])
        t = sits[n - 1] if 0 < n <= len(sits) else None
        if not t:
            return None
        sc = t.get("score")
        ok = t.get("passed") or (sc is not None and cut and sc >= cut)
        txt = (str(sc) if sc is not None else "") + (" ✓" if ok else
                                                      (" ✗" if sc is None else ""))
        return [txt.strip(), "pass" if ok else "fail", _sit_tip(t)
                + ("" if sc is not None else
                   "\nScore: needs Course watch (❔ Help ▸ Background features…)")]
    if key == "@weak":
        att = [t for t in a.get("attempts") or [] if t.get("comps")]
        if not att:
            return None
        worst = None
        for x in att[-1]["comps"]:
            r = str(x.get("rating") or "")
            rank = (x["pct"] if x.get("pct") is not None
                    else 25 * _RATING_RANK.get(r[:5].lower(), 0))
            if worst is None or rank < worst[0]:
                worst = (rank, x)
        x = worst[1]
        title = str(x.get("title") or x.get("num") or "")
        val = f"{x['pct']}%" if x.get("pct") is not None else str(x.get("rating") or "")
        kind = "pass" if (x.get("pct") or 0) >= (cut or 101) or \
            str(x.get("rating") or "").lower()[:5] in ("compe", "exemp") else "fail"
        return [f"{title[:28]} · {val}", kind,
                f"Weakest on OA {att[-1].get('n')}: {title} · {val}"]
    if key == "@pre":
        pre = a.get("pre") or {}
        if not pre.get("sittings"):
            return None
        p = pre.get("last_pct")
        txt = (f"{p}%" if p is not None else "taken") + f" ({pre['sittings']})"
        need = "" if p is not None else \
            " · % needs Course watch (❔ Help ▸ Background features…)"
        kind = "" if p is None or not cut else ("pass" if p >= cut else "fail")
        return [txt, kind, f"Pre-assessment: {pre['sittings']} sitting(s)"
                + (f", first {pre['first_pct']}%" if pre.get("first_pct") is not None else "")
                + (f", last {p}%" if p is not None else "")
                + (f" · since last OA: {pre['since_last_oa']}" if pre.get("since_last_oa") else "")
                + need]
    return None


def _field_value(key: str, row: Optional[dict], roster: dict, sid: str,
                 course: str) -> str:
    if str(key).startswith("CB:"):
        return str(((roster.get(sid) or {}).get(course) or {}).get(key[3:]) or "").strip()
    return str((row or {}).get(key) or "").strip()


def build_snapshot(rows: list, step_cols: Optional[dict] = None,
                   version: int = 0, extras: Optional[dict] = None,
                   fields: Optional[list] = None,
                   list_cols: Optional[list] = None,
                   list_sets: Optional[dict] = None,
                   list_set: str = "", list_hide_native: bool = False,
                   features: Optional[dict] = None,
                   ema_map: Optional[dict] = None) -> dict:
    """{"v": version, "students": {student id without leading zeros:
    [entry per course]}, "uuid_map", "notes", "momi"} from the caseload rows
    (after derived columns, task status and EAs are applied). ``step_cols`` =
    {course: [(sp column, step label)]} in path order. ``extras`` =
    history.overlay_extras(...) for the student panel (uuid → student map,
    recent notes, MOMI trend); omitted → empty."""
    step_cols = step_cols or {}
    roster = (extras or {}).get("roster") or {}
    out: dict = {}
    src_row: dict = {}            # (sid, course) → caseload row, for list columns
    for r in rows or []:
        sid = _nsid(r.get("StudentID") or r.get("Student ID"))
        if not sid:
            continue
        course = str(r.get("CourseCode") or "").strip().upper()
        tasks = []
        for n in range(1, 16):
            status = str(r.get(f"Task{n}Status") or "").strip()
            date = str(r.get(f"Task{n}Date") or "").strip()
            if not status and not date:
                if n > 3:
                    continue
            kind = _TASK_KIND.get(status.lower(), "none" if not date
                                  else "pending")
            if n > 3 and kind == "none":
                continue
            ema = ((ema_map or {}).get(course) or {}).get(str(n)) or {}
            full_sid = str(r.get("StudentID") or r.get("Student ID") or "").strip()
            tasks.append({"n": n, "status": status, "kind": kind,
                          "date": date,
                          "count": str(r.get(f"Task{n}Count") or ""),
                          # EMA Score Report (same per-course map as the
                          # Caseload Tool's task badges); "" = not learned
                          # yet → clicking asks the app to learn it.
                          "url": safe_url(_ema_url(full_sid, ema))})
        risk = _num(r.get("MomentumRisk"))
        src_row.setdefault((sid, course), r)
        chosen =[[_disp(k), v] for k in fields or []
                  if (v := _field_value(k, r, roster, sid, course))]
        out.setdefault(sid, []).append({
            "fields": chosen,
            "course": course,
            "name": str(r.get("Name") or ""),
            "tasks": tasks,
            "risk": risk,
            "momentum": str(r.get("Momentum") or ""),
            "next_step": _next_step(r, step_cols.get(course, [])),
            "days_contact": _num(r.get("DaysSinceLastContact")),
            "days_action": _num(r.get("DaysSinceLastAction")),
            "last_action": str(r.get("LastActionType") or ""),
            "ea": str(r.get("EssentialAction") or ""),
            "contact_pref": str(r.get("ContactPref") or ""),
            "pronouns": str(r.get("Pronouns") or ""),
            "followup_date": str(r.get("CourseFollowupDate") or ""),
            "followup_note": str(r.get("CourseFollowupNote") or "")[:200],
            "days_course_activity": _num(r.get("DaysSinceCourseActivity")),
            "signals": str(r.get("Signals", "") or ""),
            "term_left": str(r.get("TermCoursesLeft", "") or ""),
            "term_cus_left": _num(r.get("TermCUsLeft")),
            "next_course": str(r.get("NextPlannedCourse", "") or ""),
            "degree_pct": _num(r.get("DegreePctComplete")),
            "pm_email": str(r.get("ProgramMentorEmail", "") or ""),
            "pa_returns": _num(r.get("PAReturns")),
            "pa_passes": _num(r.get("PAPasses")),
            "steps": [{"label": lab, "status": str(r.get(col) or "")}
                      for col, lab in step_cols.get(course, [])
                      if str(r.get(col) or "") not in ("", "off-path")],
            "term_end": str(r.get("EffectiveEndDate")
                            or r.get("TermEndDate") or ""),
        })
    ex = extras or {}
    # OA / pre-assessment summaries (src/assess_summary) on caseload entries,
    # and entries for COURSE-COHORT students (e.g. C949 on my team, not my
    # caseload) so their Learner360 page gets a panel too.
    assess = ex.get("assess") or {}
    for sid, ents in out.items():
        for e in ents:
            a = (assess.get(sid) or {}).get(e["course"])
            if a:
                e["assess"] = a
    for sid, cl in (ex.get("cohort") or {}).items():
        have = {e["course"] for e in out.get(sid, [])}
        for c in cl:
            if c["course"] in have:
                continue
            e = {"course": c["course"], "name": "", "tasks": [], "risk": None,
                 "momentum": c.get("momentum") or "", "next_step": "",
                 "steps": [], "term_end": c.get("term_end") or "",
                 "cohort": c,
                 "fields": [[_disp(k), v] for k in fields or []
                            if (v := _field_value(k, None, roster, sid,
                                                  c["course"]))]}
            a = (assess.get(sid) or {}).get(c["course"])
            if a:
                e["assess"] = a
            out.setdefault(sid, []).append(e)
    # Extra list columns: only non-empty values travel to the page.
    cat = list_catalog(rows, extras)
    label = {k: d for k, d in cat}
    ok = lambda k: isinstance(k, str) and (k in label or NATIVE_RE.match(k))  # noqa: E731
    # Learner360's own columns get their label from the page's header.
    cols = [[k, label.get(k, "")] for k in list_cols or [] if ok(k)][:LIST_MAX]
    feats = dict(features or {})
    feats.setdefault("types", (extras or {}).get("types") or {})
    if cols:
        for sid, ents in out.items():
            for e in ents:
                vals, why = {}, {}
                for k, _d in cols:
                    if NATIVE_RE.match(k):
                        continue
                    v = _list_value(k, src_row.get((sid, e["course"])), roster,
                                    sid, e["course"], e.get("assess"))
                    if v:
                        vals[k] = v
                    else:
                        why[k] = why_empty(k, e.get("assess"), e["course"], feats)
                e["cols"] = vals
                e["why"] = why
    return {"v": version, "students": out,
            "field_catalog": cat[len(LIST_BUILTINS):],
            "list_catalog": cat,
            "list_cols": cols,
            # Named column sets (known keys only) + the active one.
            "list_sets": {n: [k for k in ks if ok(k)][:LIST_MAX]
                          for n, ks in (list_sets or {}).items()},
            "list_hide_native": bool(list_hide_native),
            "list_default": list(DEFAULT_LIST_COLS),
            "list_set": list_set if list_set in (list_sets or {}) else "",
            "panel_fields": list(fields or []),
            "uuid_map": dict(ex.get("uuid_map") or {}),
            "notes": dict(ex.get("notes") or {}),
            "momi": dict(ex.get("momi") or {})}
