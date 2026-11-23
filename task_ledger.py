"""Durable workflow task ledger with fail-closed GitHub Contents API persistence."""
import base64
import json
import os
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

import requests

LEDGER_PATH = ".github/state/workflow_tasks.json"
LEDGER_API_TIMEOUT = 20


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
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError("Unsupported task ledger schema")
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Task ledger tasks must be a list")
    seen = set()
    required = {"task_id", "provider", "workflow", "status", "created_at",
                "updated_at", "attempt", "parameters", "reservation_id",
                "external_job_id", "last_error"}
    statuses = {"pending", "running", "interrupted", "completed", "needs_manual_review"}
    for task in tasks:
        if not isinstance(task, dict) or not required.issubset(task):
            raise ValueError("Task ledger record is missing required fields")
        try:
            task_id = str(uuid5(NAMESPACE_URL, task["task_id"])) if False else task["task_id"]
            from uuid import UUID
            task_id = str(UUID(task_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Task ledger task_id must be a UUID") from error
        if task_id in seen:
            raise ValueError("Task ledger contains duplicate task_id")
        seen.add(task_id)
        if task.get("provider") not in {"SerpApi", "Upscrape"}:
            raise ValueError("Task ledger provider is unsupported")
        if not isinstance(task.get("workflow"), str) or not task["workflow"]:
            raise ValueError("Task ledger workflow is required")
        if task.get("status") not in statuses:
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
    allowed = {
        "pending": {"running", "needs_manual_review"},
        "running": {"completed", "interrupted", "needs_manual_review"},
        "interrupted": {"needs_manual_review"},
        "completed": set(),
        "needs_manual_review": set(),
    }
    if not isinstance(task, dict) or task.get("status") not in allowed:
        raise ValueError("Cannot transition an invalid task")
    if new_status not in allowed[task["status"]]:
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
    validated = validate_task_ledger(state)
    _timestamp(updated_at, "updated_at")
    for task in validated["tasks"]:
        if task["status"] == "running":
            task["status"] = "needs_manual_review"
            task["updated_at"] = updated_at
            task["last_error"] = (
                "Previous workflow ended before a completion record was written; "
                "manual review required."
            )
    return validated


def _github_context():
    token = os.getenv("GITHUB_TOKEN")
    repository = os.getenv("GITHUB_REPOSITORY")
    branch = os.getenv("GITHUB_REF_NAME")
    if not token or not repository or not branch:
        raise RuntimeError("Task ledger credentials unavailable; refusing to start provider work.")
    url = f"https://api.github.com/repos/{repository}/contents/{quote(LEDGER_PATH)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return url, headers, branch


def _read_remote_ledger(url, headers, branch):
    response = requests.get(
        url, headers=headers, params={"ref": branch}, timeout=LEDGER_API_TIMEOUT
    )
    if response.status_code == 404:
        return {"schema_version": 1, "tasks": []}, None
    response.raise_for_status()
    remote = response.json()
    try:
        state = json.loads(base64.b64decode(remote["content"]).decode("utf-8"))
        sha = remote["sha"]
    except (KeyError, ValueError, TypeError) as error:
        raise RuntimeError("Task ledger is unreadable; refusing provider work.") from error
    try:
        return validate_task_ledger(state), sha
    except ValueError as error:
        raise RuntimeError("Task ledger schema invalid; refusing provider work.") from error


def _write_remote_ledger(url, headers, branch, state, sha, message):
    body = {
        "message": message,
        "content": base64.b64encode(
            (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        ).decode("ascii"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    response = requests.put(
        url, headers=headers, json=body, timeout=LEDGER_API_TIMEOUT
    )
    if response.status_code in (409, 422):
        raise RuntimeError("Task ledger write conflict; refusing provider work.")
    response.raise_for_status()


def begin_task(provider, parameters, *, reservation_id=None):
    """Persist running status before any provider API call; fail closed on errors."""
    url, headers, branch = _github_context()
    run_id = os.getenv("GITHUB_RUN_ID", "local")
    attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1")
    workflow = os.getenv("GITHUB_WORKFLOW", "local")
    task_key = f"{provider}:{run_id}:{attempt}"
    task_id = str(uuid5(NAMESPACE_URL, task_key))
    now = datetime.now(timezone.utc).isoformat()
    state, sha = _read_remote_ledger(url, headers, branch)
    existing = next((item for item in state["tasks"] if item["task_id"] == task_id), None)
    if existing:
        if existing["status"] != "pending":
            raise RuntimeError(
                f"Task {task_id} already exists with status {existing['status']}; "
                "manual review required; refusing duplicate provider work."
            )
        task = existing
        task["status"] = "running"
        task["updated_at"] = now
    else:
        task = {
            "task_id": task_id, "provider": provider, "workflow": workflow,
            "status": "running", "created_at": now, "updated_at": now,
            "attempt": int(attempt), "parameters": deepcopy(parameters),
            "reservation_id": reservation_id, "external_job_id": None,
            "last_error": None,
        }
        state["tasks"].append(task)
    validate_task_ledger(state)
    _write_remote_ledger(url, headers, branch, state, sha,
                         f"Start {provider} task {task_id}")
    return task_id


def update_task(task_id, new_status, *, last_error=None, external_job_id=None):
    """Persist a single explicit state transition; never auto-resume."""
    url, headers, branch = _github_context()
    state, sha = _read_remote_ledger(url, headers, branch)
    task = next((item for item in state["tasks"] if item["task_id"] == task_id), None)
    if task is None:
        raise RuntimeError("Task record not found; refusing to continue.")
    changed = transition_task(
        task, new_status, updated_at=datetime.now(timezone.utc).isoformat(),
        last_error=last_error, external_job_id=external_job_id,
    )
    state["tasks"][state["tasks"].index(task)] = changed
    validate_task_ledger(state)
    _write_remote_ledger(url, headers, branch, state, sha,
                         f"Update task {task_id} to {new_status}")
    return changed


_SAFE_PARAMETER_FIELDS = {"mode", "origin", "destination", "destinations",
                           "departure_date", "return_date"}
_SECRET_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
    r"(\\s*[\\"']?\\s*[:=]\\s*[\\"']?)([^\\s,;\\\"']+)"
)
_BEARER_RE = re.compile(r"(?i)\\bBearer\\s+[A-Za-z0-9._~+/-]+=*")
_URL_RE = re.compile(r"https?://[^\\s]+", re.IGNORECASE)


def _safe_task_text(value, limit=500):
    """Bound and redact untrusted task text before displaying it in logs."""
    if value is None:
        return None
    text = str(value)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _URL_RE.sub("[URL REDACTED]", text)
    text = _SECRET_VALUE_RE.sub(r"\\1\\2[REDACTED]", text)
    return text[:limit]


def _safe_task_parameters(parameters):
    """Expose only known, non-secret route metadata; discard unknown fields."""
    if not isinstance(parameters, dict):
        return {}
    safe = {}
    for key in _SAFE_PARAMETER_FIELDS:
        value = parameters.get(key)
        if key in {"origin", "destination"}:
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value):
                safe[key] = value.upper()
        elif key == "destinations":
            if isinstance(value, list):
                codes = [item.upper() for item in value
                         if isinstance(item, str) and re.fullmatch(r"[A-Za-z]{3}", item)]
                if codes:
                    safe[key] = codes[:50]
        elif key in {"departure_date", "return_date"}:
            if isinstance(value, str) and re.fullmatch(r"\\d{4}-\\d{2}-\\d{2}", value):
                safe[key] = value
        elif key == "mode" and value in {"flexible"}:
            safe[key] = value
    return safe


def _safe_task_view(task):
    """Return the deliberately limited, redacted public view of a task."""
    return {
        "task_id": task["task_id"],
        "provider": task["provider"],
        "workflow": _safe_task_text(task["workflow"], 120),
        "status": task["status"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "attempt": task["attempt"],
        "parameters": _safe_task_parameters(task["parameters"]),
        "external_job_id": _safe_task_text(task["external_job_id"], 160),
        "last_error": _safe_task_text(task["last_error"]),
    }


def list_tasks(*, statuses=None):
    """Read-only, redacted task listing. Never contacts flight-search providers."""
    url, headers, branch = _github_context()
    state, _sha = _read_remote_ledger(url, headers, branch)
    selected = set(statuses) if statuses else None
    return [
        _safe_task_view(task)
        for task in state["tasks"]
        if selected is None or task["status"] in selected
    ]


def get_task(task_id):
    """Read-only, redacted lookup for one task by exact task ID."""
    url, headers, branch = _github_context()
    state, _sha = _read_remote_ledger(url, headers, branch)
    task = next((item for item in state["tasks"] if item["task_id"] == task_id), None)
    return _safe_task_view(task) if task else None

def reconcile_orphaned_tasks():
    """Manual-only operation: mark leftover running tasks for review, never resume."""
    url, headers, branch = _github_context()
    state, sha = _read_remote_ledger(url, headers, branch)
    now = datetime.now(timezone.utc).isoformat()
    reviewed = mark_orphaned_running_tasks_for_review(state, updated_at=now)
    if reviewed != state:
        _write_remote_ledger(
            url, headers, branch, reviewed, sha,
            "Mark orphaned workflow tasks for manual review",
        )
    return [
        task["task_id"] for task in reviewed["tasks"]
        if task["status"] == "needs_manual_review"
    ]


if __name__ == "__main__":
    command = sys.argv[1:] or ["list"]
    if command == ["list"]:
        records = list_tasks()
        print(json.dumps(records, ensure_ascii=False, indent=2))
        print(f"Listed {len(records)} task(s). Read-only; no provider API was called.")
    elif command == ["reconcile"]:
        task_ids = reconcile_orphaned_tasks()
        print(f"Manual review required for {len(task_ids)} task(s). No provider API was called.")
    elif len(command) == 2 and command[0] == "get":
        task = get_task(command[1])
        if task is None:
            raise SystemExit("Task ID not found.")
        print(json.dumps(task, ensure_ascii=False, indent=2))
        print("Read-only; no provider API was called.")
    else:
        raise SystemExit("Usage: python task_ledger.py [list|reconcile|get TASK_ID]")
