"""EMA Score Report URL helpers.

WGU's EMA Score Report links follow a fixed pattern keyed by student, course,
and task id. These pure functions parse an existing link (to learn a course's
courseId/taskId once) and rebuild the link for any student in that course — see
the "EMA Score Report links" feature. No GUI or network dependency.
"""
import re
from typing import Optional

_EMA_URL_RE = re.compile(
    r"tasks\.wgu\.edu/student/(\d+)/course/(\d+)/task/(\d+)/score-report",
    re.I)


def parse_ema_url(url: str) -> Optional[dict]:
    """Pull the ids out of an EMA Score Report URL, e.g.
    https://tasks.wgu.edu/student/009930908/course/33860018/task/4521/score-report
    -> {student_id, course_id, task_id}. None if it doesn't match."""
    m = _EMA_URL_RE.search(url or "")
    if not m:
        return None
    return {"student_id": m.group(1), "course_id": m.group(2),
            "task_id": m.group(3)}


def build_ema_url(student_id: str, course_id: str, task_id: str) -> str:
    """Rebuild a score-report URL from the three ids."""
    return (f"https://tasks.wgu.edu/student/{student_id}"
            f"/course/{course_id}/task/{task_id}/score-report")
