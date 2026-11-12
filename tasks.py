"""Pure helpers for task status, deadline classification and dashboard metrics."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional

DATE_LABELS = {
    "explicit": "Confirmed",
    "inferred": "Inferred - verify",
    "manual": "Set by you",
    "unspecified": "Date not specified",
    "none": "No deadline",
}


def effective_status(task: Dict[str, Any], today: Optional[date] = None) -> str:
    """Return 'completed', 'overdue' or 'pending' for a stored task."""
    if task.get("status") == "completed":
        return "completed"
    due = task.get("due_date")
    if due and date.fromisoformat(due) < (today or date.today()):
        return "overdue"
    return "pending"


def deadline_label(date_type: Optional[str], has_date: bool = True) -> str:
    if not has_date:
        return DATE_LABELS["none"] if date_type in (None, "none") else DATE_LABELS["unspecified"]
    return DATE_LABELS.get(date_type or "none", DATE_LABELS["none"])


def days_until(due: Optional[str], today: Optional[date] = None) -> Optional[int]:
    if not due:
        return None
    return (date.fromisoformat(due) - (today or date.today())).days


def describe_due(due: Optional[str], today: Optional[date] = None) -> str:
    delta = days_until(due, today)
    if delta is None:
        return "No date"
    if delta < 0:
        return f"{-delta} day(s) overdue"
    if delta == 0:
        return "Due today"
    return f"In {delta} day(s)"


def annotate_tasks(tasks: Iterable[Dict[str, Any]], today: Optional[date] = None) -> List[Dict[str, Any]]:
    today = today or date.today()
    return [{**t, "effective_status": effective_status(t, today)} for t in tasks]


def filter_tasks(tasks: Iterable[Dict[str, Any]], status: str = "All", priority: str = "All",
                 deadline_kind: str = "All") -> List[Dict[str, Any]]:
    """Filter annotated tasks. deadline_kind: All / Confirmed / Inferred / No deadline."""
    out = []
    for t in tasks:
        if status != "All" and t["effective_status"] != status.lower():
            continue
        if priority != "All" and t["priority"] != priority.lower():
            continue
        if deadline_kind == "Confirmed" and t["due_date_type"] not in ("explicit", "manual"):
            continue
        if deadline_kind == "Inferred" and t["due_date_type"] != "inferred":
            continue
        if deadline_kind == "No deadline" and t["due_date"]:
            continue
        out.append(t)
    return out


def task_counts(tasks: Iterable[Dict[str, Any]], today: Optional[date] = None) -> Dict[str, int]:
    counts = {"pending": 0, "completed": 0, "overdue": 0}
    for t in annotate_tasks(tasks, today):
        counts[t["effective_status"]] += 1
    return counts
