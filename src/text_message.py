"""Compose + schedule student text messages through Mongoose ("Cadence").

Texting goes through the Mongoose web app (sms.mongooseresearch.com), a
separate site the app drives in its own browser context. Templates and
variables are handled HERE (not Mongoose's own templates) — we render the
final plain-text body and inject it into Mongoose's compose box.

This module is the browser-free core: template rendering, timezone grouping,
and the student-local -> team-timezone schedule math. All of it is unit-
testable without Playwright. The Playwright driver that fills the Mongoose
compose modal builds on the DOM map captured in temp/text_probe.html (see the
`mongoose_texting_dom` memory) and is added separately.

Scheduling model (see `texting_milestone_scope` memory): one Mongoose compose
sends to all its recipients at ONE absolute time, entered in the TEAM's tz. To
reach each student at a good *local* hour we batch by timezone: one scheduled
compose per zone, each at the target local hour converted to the team's tz.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from src import email_template

# WGU caseload exports a bare timezone abbreviation (EST/CST/...). Map to IANA
# zones so DST is handled correctly. NOTE: scripts/launcher.py has its own copy
# (`_TZ_ABBR_TO_IANA`) used by `student_local_time`; consolidate to this one
# when convenient.
TZ_ABBR_TO_IANA = {
    "EST": "America/New_York", "EDT": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "AKST": "America/Anchorage", "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",                      # Hawaii — no DST
    "ChS": "Pacific/Guam", "ChST": "Pacific/Guam",  # Chamorro — no DST
}

# The Mongoose compose textarea caps the body (maxlength="306" in the DOM).
MAX_SMS_LEN = 306

# Default local hour to target when scheduling (10:00 AM in the student's tz).
# Overridable per fire; surfaced as a setting in the UI.
DEFAULT_TARGET_HOUR = 10
DEFAULT_TARGET_MINUTE = 0

# Mongoose only allows scheduling inside the team's working window
# (the DOM said "8:00 AM and 12:00 AM EDT"). We clamp the team-tz hour into
# [EARLIEST_TEAM_HOUR, 24).
EARLIEST_TEAM_HOUR = 8


def render_message(template_text: str, variables: dict) -> str:
    """Render {{var}} placeholders into a plain-text SMS body.

    Reuses the email template engine's plain-text path (no HTML escaping),
    so texting shares the exact variable set as email
    (first_name/preferred_name/course_code/...). Unknown placeholders are left
    in place so a typo is visible rather than silently dropped."""
    return email_template.render_plain(template_text or "", variables or {})


def over_length(body: str) -> int:
    """Characters by which `body` exceeds the Mongoose limit (0 if within)."""
    return max(0, len(body or "") - MAX_SMS_LEN)


def group_by_timezone(
    students: list[dict], *, tz_key: str = "timezone",
) -> dict[str, list[dict]]:
    """Group student rows by their timezone abbreviation.

    Returns {tz_abbr: [student, ...]}. Rows with a blank/missing tz are grouped
    under "" so the caller can surface them (we can't time-shift an unknown
    zone). Preserves input order within each group."""
    groups: dict[str, list[dict]] = {}
    for s in students:
        tz = (s.get(tz_key) or "").strip()
        groups.setdefault(tz, []).append(s)
    return groups


@dataclass
class ScheduleSlot:
    """A computed Mongoose schedule slot, expressed in the TEAM's timezone
    (the tz Mongoose's scheduler enters times in). The h12/minute/ampm/date_str
    fields map straight onto the compose-modal Date + Time controls."""
    team_dt: datetime          # tz-aware datetime in the team's tz
    date_str: str              # MM/DD/YYYY (Datepicker input)
    hour12: int                # 1-12 (vc-time-select-hours)
    minute: int                # 0-59 (vc-time-select-minutes)
    ampm: str                  # "AM" / "PM"
    student_local_str: str     # e.g. "10:00 AM MDT" — for display/confirmation
    clamped: bool = False      # True if we bumped into the allowed window


def compute_schedule_slot(
    student_tz_abbr: str,
    team_iana: str,
    *,
    target_hour: int = DEFAULT_TARGET_HOUR,
    target_minute: int = DEFAULT_TARGET_MINUTE,
    now: Optional[datetime] = None,
) -> Optional[ScheduleSlot]:
    """When to schedule so the student receives the text at
    target_hour:target_minute in THEIR local timezone.

    Returns a ScheduleSlot in the TEAM's tz (what Mongoose's scheduler expects),
    or None if the student's tz abbreviation is unknown.

    Picks the next occurrence of the target local time that is still in the
    future (today if it hasn't passed in the student's tz, else tomorrow), then
    converts that absolute instant to the team tz. Defensively clamps the
    team-tz time up to EARLIEST_TEAM_HOUR if it would land before the allowed
    window (shouldn't happen for an Eastern team, but matters if the team is
    further west)."""
    iana = TZ_ABBR_TO_IANA.get((student_tz_abbr or "").strip())
    if not iana:
        return None
    student_tz = ZoneInfo(iana)
    team_tz = ZoneInfo(team_iana)

    if now is None:
        now = datetime.now(team_tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=team_tz)

    now_student = now.astimezone(student_tz)
    target = now_student.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0,
    )
    if target <= now_student:
        target = target + timedelta(days=1)

    team_dt = target.astimezone(team_tz)
    clamped = False
    if team_dt.hour < EARLIEST_TEAM_HOUR:
        team_dt = team_dt.replace(
            hour=EARLIEST_TEAM_HOUR, minute=0, second=0, microsecond=0,
        )
        clamped = True

    return ScheduleSlot(
        team_dt=team_dt,
        date_str=team_dt.strftime("%m/%d/%Y"),
        hour12=int(team_dt.strftime("%I")),
        minute=team_dt.minute,
        ampm=team_dt.strftime("%p"),
        student_local_str=target.strftime("%I:%M %p %Z").lstrip("0"),
        clamped=clamped,
    )


def team_iana_from_abbr(tz_abbr: str) -> Optional[str]:
    """Resolve the team's tz abbreviation (read from Mongoose's
    `.timezone-label`, e.g. 'EDT') to an IANA zone for the schedule math."""
    return TZ_ABBR_TO_IANA.get((tz_abbr or "").strip())
