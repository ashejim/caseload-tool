"""The note-log record shared by the browser worker (which produces one per
filed note) and the App (which shows them in the log tabs + writes note_log.csv,
the persistent feed for downstream tools like the texting app).
"""
from dataclasses import dataclass
from datetime import datetime


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
