"""Append-only, fenced JSONL event authority for orchestration runs."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid
from typing import Any, Iterable, Mapping


EVENT_VERSION = 1
TERMINAL_TASK_EVENTS = {
    "task_succeeded": "succeeded",
    "task_failed": "failed",
    "task_timed_out": "timed-out",
    "task_canceled": "canceled",
    "task_blocked": "blocked",
    "task_failed_unsafe": "failed-unsafe",
}
TERMINAL_RUN_EVENTS = {
    "run_completed": "completed",
    "run_failed": "failed",
    "run_canceled": "canceled",
    "run_failed_unsafe": "failed-unsafe",
}
ALLOWED_EVENT_TYPES = {
    "controller_acquired",
    "run_started",
    "run_resumed",
    "dispatch_intent",
    "dispatch_claimed",
    "resource_intent",
    "resource_created",
    "resource_failed",
    "resource_reconciled",
    "dependency_context_prepared",
    "review_context_prepared",
    "task_retry_scheduled",
    "cancel_requested",
    "run_canceling",
    "cleanup_recorded",
    "integration_succeeded",
    "integration_failed",
    *TERMINAL_TASK_EVENTS,
    *TERMINAL_RUN_EVENTS,
}
SENSITIVE_KEYS = {
    "prompt",
    "prompt_body",
    "response",
    "response_body",
    "transcript",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "api_key",
    "password",
    "secret",
    "client_secret",
    "private_key",
    "authorization",
    "account",
    "account_id",
    "email",
    "command",
    "commands",
    "argv",
    "environment",
    "env",
    "base_url",
    "config_body",
}
TOKENISH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/-]{8,})",
    re.I,
)


class JournalError(RuntimeError):
    pass


class LeaseContended(JournalError):
    pass


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def process_start_fingerprint(pid: int | None = None) -> str:
    """Return a non-secret PID/start identity suitable for stale-process checks."""
    pid = pid or os.getpid()
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        observed = completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        observed = ""
    raw = f"{pid}:{observed or 'unknown'}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _privacy_check(value: Any, where: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in SENSITIVE_KEYS or (
                normalized.endswith("_token") and normalized != "fencing_token"
            ):
                raise JournalError(
                    f"sensitive field is forbidden in journal: {where}.{key}"
                )
            _privacy_check(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _privacy_check(child, f"{where}[{index}]")
    elif isinstance(value, str) and TOKENISH_RE.search(value):
        raise JournalError(f"secret-like value is forbidden in journal: {where}")


def validate_payload(value: Mapping[str, Any]) -> None:
    """Validate a prospective adapter/event payload before state mutation."""
    if not isinstance(value, Mapping):
        raise JournalError("payload must be a mapping")
    _privacy_check(value)


def _assert_transition(events: list[dict[str, Any]], event: Mapping[str, Any]) -> None:
    kind = event["event_type"]
    if kind == "controller_acquired":
        return
    state = fold_events(events) if events else {"tasks": {}, "resources": {}}
    task = state["tasks"].get(event.get("task_id"), {})
    status = task.get("status", "pending")
    if kind == "dispatch_intent" and status not in {"pending", "retry-pending"}:
        raise JournalError(f"dispatch_intent is invalid from task state {status}")
    if kind == "dispatch_claimed" and (
        status != "dispatch-intent"
        or task.get("current_attempt_id") != event["attempt_id"]
    ):
        raise JournalError("dispatch_claimed requires the matching dispatch_intent")
    if kind == "task_succeeded" and status not in {"running", "dispatch-intent"}:
        raise JournalError(
            "task_succeeded requires a claimed task or a recovered dispatch intent"
        )
    if kind == "review_context_prepared" and status != "dispatch-intent":
        raise JournalError("review_context_prepared requires matching dispatch_intent")
    if kind == "dependency_context_prepared" and status != "dispatch-intent":
        raise JournalError(
            "dependency_context_prepared requires matching dispatch_intent"
        )
    if kind in {
        "task_failed",
        "task_timed_out",
        "task_failed_unsafe",
    } and status not in {
        "running",
        "dispatch-intent",
    }:
        raise JournalError(f"{kind} requires a claimed or resource-intent task")
    if kind == "task_retry_scheduled" and status not in {"failed", "timed-out"}:
        raise JournalError("task_retry_scheduled requires a terminal retryable failure")
    if kind in {"resource_created", "resource_failed", "resource_reconciled"}:
        resource_id = event["payload"].get("resource_id")
        resource = state["resources"].get(resource_id, {})
        expected_statuses = (
            {"created", "reconciled"} if kind == "resource_reconciled" else {"intent"}
        )
        if resource.get("status") not in expected_statuses:
            raise JournalError(f"{kind} requires a matching prior resource state")
        for field in (
            "created_by_run_id",
            "repo_root",
            "path",
            "branch",
            "base_sha",
            "ledger_slug",
        ):
            if field in resource and event["payload"].get(field) != resource[field]:
                raise JournalError(f"resource confirmation identity drift: {field}")


def _validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "version",
        "event_id",
        "event_type",
        "timestamp",
        "run_id",
        "attempt_id",
        "generation",
        "fencing_token",
        "controller_pid",
        "controller_start_fingerprint",
        "payload",
    }
    missing = sorted(required - set(event))
    unknown = sorted(set(event) - required - {"task_id"})
    if missing or unknown:
        raise JournalError(
            "invalid event fields"
            + (f"; missing={missing}" if missing else "")
            + (f"; unknown={unknown}" if unknown else "")
        )
    if event["version"] != EVENT_VERSION:
        raise JournalError("event.version must be 1")
    if event["event_type"] not in ALLOWED_EVENT_TYPES:
        raise JournalError(f"unknown event type: {event['event_type']!r}")
    for field in (
        "event_id",
        "timestamp",
        "run_id",
        "attempt_id",
        "fencing_token",
        "controller_start_fingerprint",
    ):
        if not isinstance(event[field], str) or not event[field]:
            raise JournalError(f"event.{field} must be a non-empty string")
    if (
        not isinstance(event["generation"], int)
        or isinstance(event["generation"], bool)
        or event["generation"] <= 0
    ):
        raise JournalError("event.generation must be a positive integer")
    if not isinstance(event["controller_pid"], int) or event["controller_pid"] <= 0:
        raise JournalError("event.controller_pid must be positive")
    if "task_id" in event and (
        not isinstance(event["task_id"], str) or not event["task_id"]
    ):
        raise JournalError("event.task_id must be a non-empty string")
    if not isinstance(event["payload"], Mapping):
        raise JournalError("event.payload must be a mapping")
    if event["event_type"].startswith("task_") or event["event_type"].startswith(
        "dispatch_"
    ):
        if "task_id" not in event:
            raise JournalError(f"{event['event_type']} requires task_id")
    _privacy_check(event["payload"])
    return dict(event)


class ControllerLease:
    """Non-blocking cross-process lease for exactly one run controller."""

    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self._handle = None
        self.owner_pid = os.getpid()
        self.owner_start_fingerprint = process_start_fingerprint(self.owner_pid)

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, *, blocking: bool = False) -> "ControllerLease":
        if self.held:
            raise JournalError("controller lease is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            handle.close()
            raise LeaseContended(f"run {self.run_id!r} is already controlled") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(
            canonical_json(
                {
                    "run_id": self.run_id,
                    "controller_pid": self.owner_pid,
                    "controller_start_fingerprint": self.owner_start_fingerprint,
                }
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "ControllerLease":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def _read_locked(handle) -> list[dict[str, Any]]:
    handle.seek(0)
    events: list[dict[str, Any]] = []
    for line_number, raw in enumerate(handle, 1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JournalError(f"invalid JSONL at line {line_number}: {exc}") from exc
        if not isinstance(event, Mapping):
            raise JournalError(f"journal line {line_number} is not an event mapping")
        events.append(_validate_event(event))
    return events


class EventJournal:
    """Mode-0600 JSONL writer with generation/fencing enforcement."""

    def __init__(self, path: Path, run_id: str):
        if not run_id:
            raise JournalError("run_id is required")
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)

    def read(self) -> list[dict[str, Any]]:
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                events = _read_locked(handle)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        foreign = [
            event["event_id"] for event in events if event["run_id"] != self.run_id
        ]
        if foreign:
            raise JournalError("journal contains events from another run")
        return events

    def current_controller(self) -> tuple[int, str] | None:
        current = None
        for event in self.read():
            if event["event_type"] == "controller_acquired":
                current = (event["generation"], event["fencing_token"])
        return current

    def next_generation(self) -> int:
        current = self.current_controller()
        return 1 if current is None else current[0] + 1

    def append(
        self,
        event_type: str,
        *,
        attempt_id: str,
        generation: int,
        fencing_token: str,
        task_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: str | None = None,
        controller_pid: int | None = None,
        controller_start_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "version": EVENT_VERSION,
            "event_id": event_id or f"evt-{uuid.uuid4()}",
            "event_type": event_type,
            "timestamp": timestamp or utc_now(),
            "run_id": self.run_id,
            "attempt_id": attempt_id,
            "generation": generation,
            "fencing_token": fencing_token,
            "controller_pid": controller_pid or os.getpid(),
            "controller_start_fingerprint": controller_start_fingerprint
            or process_start_fingerprint(controller_pid),
            "payload": dict(payload or {}),
        }
        if task_id is not None:
            event["task_id"] = task_id
        return self.append_event(event)

    def append_event(self, raw_event: Mapping[str, Any]) -> dict[str, Any]:
        event = _validate_event(raw_event)
        if event["run_id"] != self.run_id:
            raise JournalError("event run_id does not match journal scope")
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                events = _read_locked(handle)
                by_id = {item["event_id"]: item for item in events}
                existing = by_id.get(event["event_id"])
                if existing is not None:
                    if existing != event:
                        raise JournalError("event_id collision has different content")
                    return existing
                current = None
                for item in events:
                    if item["event_type"] == "controller_acquired":
                        current = (item["generation"], item["fencing_token"])
                if event["event_type"] == "controller_acquired":
                    expected_generation = 1 if current is None else current[0] + 1
                    if event["generation"] != expected_generation:
                        raise JournalError(
                            "controller generation is stale or non-monotonic"
                        )
                elif current != (event["generation"], event["fencing_token"]):
                    raise JournalError("stale generation or fencing token")
                _assert_transition(events, event)
                handle.seek(0, os.SEEK_END)
                handle.write(canonical_json(event) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return event

    def write_manifest(
        self,
        path: Path,
        value: Mapping[str, Any],
        *,
        generation: int,
        fencing_token: str,
    ) -> None:
        if self.current_controller() != (generation, fencing_token):
            raise JournalError("stale generation cannot replace a manifest")
        write_replaceable_manifest(path, value)


def write_replaceable_manifest(path: Path, value: Mapping[str, Any]) -> None:
    """Durably replace a small controller manifest; never use for the journal."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(dict(value)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def request_cancel_file(
    path: Path,
    *,
    run_id: str,
    generation: int,
    fencing_token: str,
    timestamp: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Atomically request cancellation without writing journal state.

    A controller consumes the request only while its run/generation/fencing
    identity matches.  The helper intentionally cannot emit terminal events.
    """
    if not isinstance(run_id, str) or not run_id:
        raise JournalError("cancel request requires run_id")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
    ):
        raise JournalError("cancel request requires a positive generation")
    if not isinstance(fencing_token, str) or not fencing_token:
        raise JournalError("cancel request requires fencing_token")
    request = {
        "version": 1,
        "request_id": request_id or f"cancel-{uuid.uuid4()}",
        "requested_at": timestamp or utc_now(),
        "run_id": run_id,
        "generation": generation,
        "fencing_token": fencing_token,
        "mode": "graceful-drain",
    }
    write_replaceable_manifest(path, request)
    return request


def read_cancel_file(path: Path) -> dict[str, Any] | None:
    """Read a strict cancellation request; malformed files fail closed."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JournalError(f"invalid cancel request: {exc}") from exc
    required = {
        "version",
        "request_id",
        "requested_at",
        "run_id",
        "generation",
        "fencing_token",
        "mode",
    }
    if not isinstance(raw, Mapping) or set(raw) != required:
        raise JournalError("cancel request has invalid fields")
    if raw["version"] != 1 or raw["mode"] != "graceful-drain":
        raise JournalError("cancel request has unsupported version or mode")
    if any(
        not isinstance(raw[field], str) or not raw[field]
        for field in ("request_id", "requested_at", "run_id", "fencing_token")
    ):
        raise JournalError("cancel request string fields must be non-empty")
    if (
        not isinstance(raw["generation"], int)
        or isinstance(raw["generation"], bool)
        or raw["generation"] <= 0
    ):
        raise JournalError("cancel request generation must be positive")
    return dict(raw)


def reconcile_identity(
    expected: Mapping[str, Any], observed: Mapping[str, Any]
) -> None:
    """Fail closed unless the complete mechanically-owned identity chain matches."""
    fields = (
        "run_id",
        "task_id",
        "attempt_id",
        "generation",
        "session_id",
        "wrapper_pid",
        "wrapper_start_fingerprint",
        "worktree_path",
        "branch",
        "base_sha",
    )
    missing = [
        field for field in fields if field not in expected or field not in observed
    ]
    if missing:
        raise JournalError("identity chain is incomplete: " + ", ".join(missing))
    drift = [field for field in fields if expected[field] != observed[field]]
    if drift:
        raise JournalError("identity chain drift: " + ", ".join(drift))


def fold_events(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Deterministically deduplicate and fold an event stream."""
    unique: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for raw in events:
        event = _validate_event(raw)
        existing = unique.get(event["event_id"])
        if existing is not None:
            if existing != event:
                raise JournalError("event_id collision has different content")
            continue
        unique[event["event_id"]] = event
        ordered.append(event)
    state: dict[str, Any] = {
        "run_id": ordered[0]["run_id"] if ordered else None,
        "attempt_id": None,
        "generation": 0,
        "fencing_token": None,
        "status": "not-started",
        "cancel_requested": False,
        "tasks": {},
        "resources": {},
        "cleanup": {},
        "integration": None,
        "event_count": len(ordered),
        "last_timestamp": ordered[-1]["timestamp"] if ordered else None,
    }
    for event in ordered:
        if state["run_id"] != event["run_id"]:
            raise JournalError("cannot fold events from multiple runs")
        kind = event["event_type"]
        if kind == "controller_acquired":
            if event["generation"] <= state["generation"]:
                raise JournalError("controller generations are not monotonic")
            state.update(
                {
                    "attempt_id": event["attempt_id"],
                    "generation": event["generation"],
                    "fencing_token": event["fencing_token"],
                    "controller_pid": event["controller_pid"],
                    "controller_start_fingerprint": event[
                        "controller_start_fingerprint"
                    ],
                }
            )
        elif (
            event["generation"] != state["generation"]
            or event["fencing_token"] != state["fencing_token"]
        ):
            raise JournalError("event stream contains stale fenced mutation")
        if kind in {"run_started", "run_resumed"}:
            state["status"] = "running"
        elif kind == "cancel_requested":
            state["cancel_requested"] = True
            state["status"] = "canceling"
        elif kind == "run_canceling":
            state["status"] = "canceling"
            state["eta_seconds"] = event["payload"].get("eta_seconds", 0)
        elif kind in TERMINAL_RUN_EVENTS:
            state["status"] = TERMINAL_RUN_EVENTS[kind]
            state["eta_seconds"] = 0
        # Cleanup records may use synthetic resource ids such as
        # ``integration``.  They belong in the cleanup projection only and
        # must not manufacture a phantom pending task in status output.
        if "task_id" in event and kind != "cleanup_recorded":
            task = state["tasks"].setdefault(
                event["task_id"],
                {"status": "pending", "attempts": [], "current_attempt_id": None},
            )
            if kind == "dispatch_intent":
                task["status"] = "dispatch-intent"
                task["current_attempt_id"] = event["attempt_id"]
                if event["attempt_id"] not in task["attempts"]:
                    task["attempts"].append(event["attempt_id"])
                task["deadline_at"] = event["payload"].get("deadline_at")
            elif kind == "dispatch_claimed":
                task["status"] = "running"
                task["wrapper_pid"] = event["payload"].get("wrapper_pid")
                task["wrapper_start_fingerprint"] = event["payload"].get(
                    "wrapper_start_fingerprint"
                )
                task["checkpoint_event"] = event["payload"].get("checkpoint_event")
                task["compiled_seat"] = event["payload"].get("compiled_seat")
            elif kind == "review_context_prepared":
                task["review_context"] = dict(event["payload"])
            elif kind == "dependency_context_prepared":
                task["dependency_context"] = dict(event["payload"])
            elif kind == "task_retry_scheduled":
                task["status"] = "retry-pending"
                task["failure_class"] = event["payload"].get("failure_class")
            elif kind in TERMINAL_TASK_EVENTS:
                task["status"] = TERMINAL_TASK_EVENTS[kind]
                task["failure_class"] = event["payload"].get("failure_class")
                task["result"] = dict(event["payload"])
        if kind.startswith("resource_"):
            resource_id = event["payload"].get("resource_id")
            if not isinstance(resource_id, str) or not resource_id:
                raise JournalError(f"{kind} requires payload.resource_id")
            resource = state["resources"].setdefault(resource_id, {})
            resource.update(event["payload"])
            resource["status"] = kind.removeprefix("resource_")
        if kind == "cleanup_recorded":
            task_id = event.get("task_id", "run")
            state["cleanup"][task_id] = dict(event["payload"])
        if kind in {"integration_succeeded", "integration_failed"}:
            state["integration"] = {
                "status": "succeeded" if kind == "integration_succeeded" else "failed",
                **dict(event["payload"]),
            }
    return state
