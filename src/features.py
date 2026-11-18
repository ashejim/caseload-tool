"""Background features a user can switch on/off in one place (the first-run
"Choose features" step, and ❔ Help ▸ Background features…): what each adds,
roughly how long it takes, and which settings it maps to.

Pure data + helpers (no Tk) so the mapping is testable. Run times are honest
estimates: "measured" ones come from real runs; the rest are from batch sizes.
"""
from __future__ import annotations

from typing import Optional

# Interval used when a periodic job is switched ON here (days, or hours for
# approval_check_hours).
_ON_DAYS = {"l360_auto_momi_days": 7, "l360_auto_notes_days": 7,
            "l360_auto_activity_days": 7, "l360_auto_plan_days": 7,
            "l360_auto_roster_days": 1, "approval_check_hours": 3}

FEATURES = [
    {"key": "l360", "title": "Learner360 extension",
     "adds": "CaseloadNotes columns, badges and the student panel inside "
             "Learner360; opens Learner360 at startup.",
     "time": "a few seconds at startup",
     "default": True,
     "settings": ["l360_overlay_enabled", "l360_open_on_startup"]},
    {"key": "roster", "title": "Team roster (Learner360)",
     "adds": "Links Learner360's learners to your students — the student panel "
             "needs it. Also keeps course-instructor names current.",
     "time": "about 30 seconds, once a day (background)",
     "default": True,
     "settings": ["l360_auto_roster_days"]},
    {"key": "course_scan", "title": "Course scan (Salesforce)",
     "adds": "OA attempts (pass / fail + dates), whether a pre-assessment was "
             "taken, and the “CB ·” caseload-browser fields, for your courses.",
     "time": "1–2 minutes, once a day — drives the Salesforce browser",
     "default": True,
     "settings": ["course_scan_auto"]},
    {"key": "approval", "title": "OA approval check",
     "adds": "Shows “OA approval requested” in the EA column when a student "
             "who failed an OA has asked to retake — even when the Essential "
             "Action goes to another CI. Re-checks only students with a "
             "failed, not-yet-passed OA (one quick request each).",
     "time": "under a minute, every 3 hours (background)",
     "default": True,
     "settings": ["approval_check_hours"]},
    {"key": "course_watch", "title": "Course watch (assessment details)",
     "adds": "OA scores, pre-assessment %, weakest competency and the "
             "competency grid; keeps passers for pass-rate reports.",
     "time": "10–25 minutes per course, once a day (background)",
     "default": False,
     "settings": ["course_watch_enabled"]},
    {"key": "momi", "title": "MOMI history (Learner360)",
     "adds": "Weekly MOMI trend in the student panel + change alerts.",
     "time": "about 3–5 minutes, once a week (background)",
     "default": False,
     "settings": ["l360_auto_momi_days"]},
    {"key": "notes", "title": "Learner360 notes",
     "adds": "Students' full contact history (incl. other faculty) in the "
             "student panel and reports.",
     "time": "about 5–15 minutes, once a week (background)",
     "default": False,
     "settings": ["l360_auto_notes_days"]},
    {"key": "activity", "title": "Academic activity (Learner360)",
     "adds": "Engagement columns: days since course activity, signals "
             "(not opened, working but no submission…), PA returns / passes.",
     "time": "about 3–5 minutes, once a week (background)",
     "default": False,
     "settings": ["l360_auto_activity_days"]},
    {"key": "plans", "title": "Degree plans (Learner360)",
     "adds": "Courses left this term, next planned course, degree % complete, "
             "program mentor.",
     "time": "about 4 minutes, once a week (measured: 281 learners)",
     "default": False,
     "settings": ["l360_auto_plan_days"]},
]

BY_KEY = {f["key"]: f for f in FEATURES}


def is_on(settings, key: str) -> bool:
    """Whether ``key`` is on in ``settings`` (all of its settings truthy)."""
    return all(bool(getattr(settings, s, False)) for s in BY_KEY[key]["settings"])


def apply(settings, choices: dict) -> list:
    """Write {key: on} into ``settings``; → [feature titles that changed]."""
    changed = []
    for key, on in choices.items():
        f = BY_KEY.get(key)
        if not f or is_on(settings, key) == bool(on):
            continue
        for s in f["settings"]:
            if s in _ON_DAYS:
                setattr(settings, s, _ON_DAYS[s] if on else 0)
            else:
                setattr(settings, s, bool(on))
        changed.append(f["title"])
    return changed


def defaults() -> dict:
    return {f["key"]: f["default"] for f in FEATURES}


def why_off(settings, key: str) -> Optional[str]:
    """'turn on <feature>' hint for an empty value, or None when it's on."""
    if is_on(settings, key):
        return None
    return (f"Needs “{BY_KEY[key]['title']}” — turn it on in ❔ Help ▸ "
            "Background features…")
