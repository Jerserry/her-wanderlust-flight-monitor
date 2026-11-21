"""Pure validation and transition rules for durable workflow task records.

This module performs no network or API operations. Runtime integration is added
in a later implementation stage.
"""
from copy import deepcopy
from datetime import datetime
from uuid import UUID

SCHEMA_VERSION = 1
STATUSES = {"pending", "running", "interrupted", "completed", "needs_manual_review"}
TERMINAL_STATUSES = {"completed", "needs_manual_review"}
ALLOWED_TRANSITIONS = {
    "pending": {"running", "needs_manual_review"},
    "running": {"completed", "interrupted", "needs_manual_review"},
    "interrupted": {"needs_manual_review"},
    "completed": set(),
    "needs_manual_review": set(),
}
REQUIRED_TASK_FIELDS = {
    "task_id", "provider", "workflow", "status", "created_at", "updated_at",
    "attempt", "parameters", "reservation_id", "external_job_id", "last_error",
}


def _timestamp(value, field):
    if not isinstance(value, str):
        raise ValueError(f"Task ledger {field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Task ledger {field} must be an ISO-8601 string") from error
    if parsed.tzinfo is None:
        raise ValueError(f"Task ledger {field} must include a timezone")
    return parsed


def validate_task_ledger(state):
    """Validate the complete task ledger; reject unknown schema/status values."""
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported task ledger schema")
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Task ledger tasks must be a list")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict) or not REQUIRED_TASK_FIELDS.issubset(task):
            raise ValueError("Task ledger record is missing required fields")
        try:
            task_id = str(UUID(task["task_id"]))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Task ledger task_id must be a UUID") from error
        if task_id in seen:
            raise ValueError("Task ledger contains duplicate task_id")
        seen.add(task_id)
        if task.get("provider") not in {"SerpApi", "Upscrape"}:
            raise ValueError("Task ledger provider is unsupported")
        if not isinstance(task.get("workflow"), str) or not task["workflow"]:
            raise ValueError("Task ledger workflow is required")
        if task.get("status") not in STATUSES:
            raise ValueError("Task ledger status is unsupported")
        _timestamp(task.get("created_at"), "created_at")
        _timestamp(task.get("updated_at"), "updated_at")
        if not isinstance(task.get("attempt"), int) or task["attempt"] < 1:
            raise ValueError("Task ledger attempt must be a positive integer")
        if not isinstance(task.get("parameters"), dict):
            raise ValueError("Task ledger parameters must be an object")
        for field in ("reservation_id", "external_job_id", "last_error"):
            if task[field] is not None and not isinstance(task[field], str):
                raise ValueError(f"Task ledger {field} must be a string or null")
        if task["status"] == "completed" and task["last_error"] is not None:
            raise ValueError("Completed task cannot retain last_error")
    return deepcopy(state)


def transition_task(task, new_status, *, updated_at, last_error=None,
                    external_job_id=None):
    """Return a validated copy after one permitted state transition."""
    if not isinstance(task, dict) or task.get("status") not in STATUSES:
        raise ValueError("Cannot transition an invalid task")
    if new_status not in ALLOWED_TRANSITIONS[task["status"]]:
        raise ValueError(f"Invalid task transition: {task['status']} -> {new_status}")
    _timestamp(updated_at, "updated_at")
    changed = deepcopy(task)
    changed["status"] = new_status
    changed["updated_at"] = updated_at
    if new_status == "completed":
        changed["last_error"] = None
    elif last_error is not None:
        changed["last_error"] = str(last_error)[:500]
    if external_job_id is not None:
        changed["external_job_id"] = str(external_job_id)
    return changed


def mark_orphaned_running_tasks_for_review(state, *, updated_at):
    """Never resume automatically: running tasks found after restart need review."""
    validated = validate_task_ledger(state)
    _timestamp(updated_at, "updated_at")
    for task in validated["tasks"]:
        if task["status"] == "running":
            task["status"] = "needs_manual_review"
            task["updated_at"] = updated_at
            task["last_error"] = "Previous workflow ended before a completion record was written; manual review required."
    return validated
