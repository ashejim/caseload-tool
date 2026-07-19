"""The note-log record shared by the browser worker (which produces one per
filed note) and the App (which shows them in the log tabs + writes note_log.csv,
the persistent feed for downstream tools like the texting app).
"""
from dataclasses import dataclass
from datetime import datetime


def _default_names_match(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def resolve_student_id(rows, name, course, *, norm_course=lambda c: c,
                       names_match=_default_names_match) -> str:
    """Best-effort StudentID for a note-log entry whose worker-side id came back
    empty — the deep-link fire path opens a record by Contact id and so has no
    on-page Caseload table to scrape the id from.

    Match the student `name` against the loaded caseload `rows` (each a dict with
    ``Name`` / ``StudentID`` / ``CourseCode``), preferring a same-`course` row so
    a student enrolled in several courses still resolves. Returns ``''`` unless
    exactly one StudentID matches — never guess, because a note filed against the
    wrong student can't be deleted. `norm_course` / `names_match` are injected so
    the caller can reuse the app's real course-normalizer and loose name match.
    """
    name = (name or "").strip()
    if not name:
        return ""
    want = norm_course((course or "").strip())
    matches = []  # (student_id, normalized course)
    for r in rows or []:
        rn = str(r.get("Name", "") or "").strip()
        sid = str(r.get("StudentID", "") or "").strip()
        if not rn or not sid or not names_match(rn, name):
            continue
        matches.append((sid, norm_course(str(r.get("CourseCode", "") or "").strip())))
    if not matches:
        return ""
    same_course = {sid for sid, rc in matches if want and rc == want}
    candidates = same_course or {sid for sid, _ in matches}
    return next(iter(candidates)) if len(candidates) == 1 else ""


@dataclass
class NoteLogEntry:
    """One filed note. Used both for the in-session tabs and the
    persistent CSV that feeds downstream tools (e.g. the texting app).

    `submitted` is False when any note in the scenario opted out of
    auto-submit (the form was filled but the user is reviewing it).
    `student_id`, `student_email`, `pm_name`, `pm_email` come from the
    Caseload table row when available; the 'Email Student' link has
    the PM as primary (so `pm_email`) and the student as CC.
    """
    timestamp: datetime
    scenario: str
    course_code: str
    student: str
    student_id: str = ""
    student_email: str = ""
    pm_name: str = ""
    pm_email: str = ""
    submitted: bool = True

    @property
    def tab_key(self) -> str:
        return f"{self.course_code} {self.scenario}"

    @property
    def display(self) -> str:
        flag = "" if self.submitted else "  (not submitted)"
        id_suffix = f"  [{self.student_id}]" if self.student_id else ""
        return f"{self.timestamp:%H:%M:%S}  {self.student}{id_suffix}{flag}"
