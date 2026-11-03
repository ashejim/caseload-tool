"""Contextual actions: which actions fit the students you've SELECTED.

Replaces the "every action, always" panel list. For a selection of caseload
rows, each action is classified by:

  - its COURSES, inferred from (in order of authority) the success-path steps
    it completes, Course Code conditions in its filters, its notes' course
    override, and finally a course code in its name ("welcome-C769"). No
    course found = a GENERAL action (fits any course).
  - an action fits the selection only if it fits EVERY selected student's
    course (select C769 + C964 → only actions valid for both);
  - its CONDITIONS, evaluated per selected student with exactly the batch
    engine's rules. A batch action's filters are its usual TARGETING — soft:
    the action is still offered (with "2 of 3 meet its usual conditions") and
    the user chooses who to include. Fire filters are a hard safety gate: an
    action whose fire filters no selected student meets is not offered;
  - SUGGESTED when it completes a selected student's next due success-path
    step (listed first).

Pure (no Tk, no browser, no DB): the caseload viewer, the Learner360 panel and
the standalone page all use it.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from src import caseload_filter

_COURSE_RE = re.compile(r"\b([A-Z]\d{3})\b")


def _norm(c) -> str:
    return str(c or "").strip().upper()


def action_courses(sc, success_paths: Optional[dict] = None) -> set:
    """Courses an action is for; empty set = general (any course)."""
    out = set()
    for course, path in (success_paths or {}).items():
        names = {sc.name} | set(getattr(sc, "aliases", []) or [])
        if any((st.action or "").strip() in names for st in path.steps):
            out.add(_norm(course))
    filters = list(getattr(sc, "fire_filters", []) or [])
    if getattr(sc, "batch", None) is not None:
        filters += list(sc.batch.filters or [])
    for f in filters:
        col = str(f.get("column") or "").replace(" ", "").lower()
        if col in ("coursecode", "course"):
            op = str(f.get("op") or "").lower()
            if op in ("is", "equals", "is any of", "in", "="):
                vals = re.split(r"[,;]", str(f.get("value") or ""))
                out |= {_norm(v) for v in vals if _norm(v)}
    for n in getattr(sc, "notes", []) or []:
        if getattr(n, "course_code_override", ""):
            out.add(_norm(n.course_code_override))
    if not out:
        out |= set(_COURSE_RE.findall(sc.name.upper()))
    return out


def gate_filters(sc) -> list:
    """The conditions a selected student must meet for this action."""
    if getattr(sc, "batch", None) is not None:
        return list(sc.batch.filters or [])
    return list(getattr(sc, "fire_filters", []) or [])


def gate_rows(filters: list, rows: list) -> list:
    """Rows meeting ``filters`` — the batch engine's exact path: display
    columns → CSV headers, 'Task N' routed to its facet, then AND."""
    if not filters:
        return list(rows)
    headers = list(rows[0].keys()) if rows else []
    ev = [caseload_filter.rewrite_task_filter(
        caseload_filter.resolve_filter_columns(f, headers)) for f in filters]
    return caseload_filter.apply_filters(ev, rows)


def applicable_actions(scenarios, rows: list, *, success_paths=None,
                       next_action_for: Optional[Callable] = None) -> list:
    """Actions that fit the selected ``rows``, best first.

    ``next_action_for(row)`` → the action name completing that student's next
    due success-path step ('' if none) — supplied by the caller (it knows the
    computed step statuses). Returns [{scenario, name, courses (sorted list,
    empty = general), matched, total, suggested (how many selected students
    have it as their next step), is_batch}] — sorted suggested-first, then
    full matches, then course-specific before general, then name."""
    rows = [r for r in rows or [] if r]
    if not rows:
        return []
    sel_courses = {_norm(r.get("CourseCode")) for r in rows} - {""}
    nexts = [(next_action_for(r) if next_action_for else "") for r in rows]
    out = []
    for sc in scenarios:
        if getattr(sc, "record_only", False) and not sc.name:
            continue
        courses = action_courses(sc, success_paths)
        if courses and not sel_courses <= courses:
            continue                      # not valid for every selected course
        gate = gate_filters(sc)
        try:
            ok = gate_rows(gate, rows) if gate else rows
        except Exception:
            ok = rows                     # unknown column etc. → don't hide it
        is_batch = getattr(sc, "batch", None) is not None
        if gate and not ok and not is_batch:
            continue                      # hard fire-filter gate: nobody passes
        names = {sc.name} | set(getattr(sc, "aliases", []) or [])
        out.append({
            "scenario": sc, "name": sc.name, "courses": sorted(courses),
            "matched": len(ok), "total": len(rows),
            "suggested": sum(1 for n in nexts if n and n in names),
            "is_batch": is_batch,
            "conditions": gate,
        })
    out.sort(key=lambda a: (-a["suggested"], not a["courses"],
                            -(a["matched"] / max(1, a["total"])),
                            a["name"].lower()))
    return out


def describe_conditions(filters: list) -> str:
    """Short human summary of an action's conditions for a prompt."""
    parts = []
    for f in filters or []:
        col = str(f.get("column") or "")
        if col.startswith("sp:"):
            col = "success-path step " + col.split(":")[-1]
        v = str(f.get("value") or "")
        parts.append(f"{col} {f.get('op', '')} {v}".strip())
    return "; ".join(parts)
