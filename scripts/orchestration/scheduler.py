"""Ready-set DAG scheduler using only governed, compiled plan projections."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import datetime as dt
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Protocol
import uuid

from .journal import (
    ControllerLease,
    EventJournal,
    JournalError,
    LeaseContended,
    fold_events,
    process_start_fingerprint,
    read_cancel_file,
    request_cancel_file,
    validate_payload,
)


SUCCESS = {"succeeded"}
FINAL_FAILURE = {"failed", "timed-out", "blocked", "failed-unsafe", "canceled"}
TERMINAL = SUCCESS | FINAL_FAILURE


class SchedulerError(RuntimeError):
    pass


class AlreadyControlled(SchedulerError):
    pass


class Adapter(Protocol):
    def run_task(
        self,
        task: Mapping[str, Any],
        *,
        run_id: str,
        attempt_id: str,
        generation: int,
    ) -> Mapping[str, Any]: ...


class Clock(Protocol):
    def time(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _iso_timestamp(epoch: float) -> str:
    return (
        dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _attempt_id(run_id: str, task_id: str, ordinal: int) -> str:
    return "attempt-" + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"agent-run:{run_id}:{task_id}:{ordinal}")
    )


def _fencing_token(run_id: str, generation: int) -> str:
    return "fence-" + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"agent-run:{run_id}:generation:{generation}")
    )


def _retry_delay_seconds(attempts_used: int) -> float:
    """Return deterministic exponential backoff bounded by the V1 retry cap."""

    return float(min(4, 2 ** max(0, attempts_used - 1)))


class Scheduler:
    """Synchronous controller; adapters may execute tasks concurrently.

    The controller thread is the only event writer.  Worker threads call only
    the adapter seam and return structured results to that writer.
    """

    def __init__(
        self,
        plan: Mapping[str, Any],
        adapter: Adapter | Callable[..., Mapping[str, Any]],
        journal: EventJournal,
        lease_path: Path,
        *,
        clock: Clock | None = None,
        cancel_request_path: Path | None = None,
    ):
        self.plan = dict(plan)
        self.adapter = adapter
        self.journal = journal
        self.run_id = str(self.plan.get("run_id") or journal.run_id)
        if self.run_id != journal.run_id:
            raise SchedulerError("plan and journal run_id differ")
        if not isinstance(self.plan.get("topological_order"), list):
            raise SchedulerError(
                "scheduler requires a compiled plan from validate_plan"
            )
        self.tasks = {task["id"]: task for task in self.plan["tasks"]}
        self.lease = ControllerLease(Path(lease_path), self.run_id)
        self.clock = clock or SystemClock()
        self.cancel_request_path = (
            Path(cancel_request_path)
            if cancel_request_path
            else journal.path.with_name(journal.path.name + ".cancel-request.json")
        )
        self.generation = 0
        self.fencing_token = ""
        self.controller_attempt_id = "run-attempt-" + str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"agent-run:{self.run_id}")
        )
        self._cancel_requested = threading.Event()
        self._event_sequence = 0

    def _event_id(self, event_type: str, task_id: str | None, attempt_id: str) -> str:
        self._event_sequence += 1
        value = f"{self.run_id}:{self.generation}:{self._event_sequence}:{event_type}:{task_id or '-'}:{attempt_id}"
        return "evt-" + str(uuid.uuid5(uuid.NAMESPACE_URL, value))

    def _emit(
        self,
        event_type: str,
        *,
        task_id: str | None = None,
        attempt_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        actual_attempt = attempt_id or self.controller_attempt_id
        return self.journal.append(
            event_type,
            task_id=task_id,
            attempt_id=actual_attempt,
            generation=self.generation,
            fencing_token=self.fencing_token,
            payload=payload,
            event_id=self._event_id(event_type, task_id, actual_attempt),
            timestamp=_iso_timestamp(self.clock.time()),
            controller_pid=self.lease.owner_pid,
            controller_start_fingerprint=self.lease.owner_start_fingerprint,
        )

    def _call_adapter(
        self, task: Mapping[str, Any], attempt_id: str
    ) -> Mapping[str, Any]:
        runner = getattr(self.adapter, "run_task", self.adapter)
        result = runner(
            task,
            run_id=self.run_id,
            attempt_id=attempt_id,
            generation=self.generation,
        )
        return self._normalize_adapter_result(result)

    def _normalize_adapter_result(self, result: Any) -> Mapping[str, Any]:
        if not isinstance(result, Mapping):
            raise SchedulerError("adapter result must be a mapping")
        result = dict(result)
        if result.get("status") not in {
            "succeeded",
            "failed",
            "timed-out",
            "failed-unsafe",
        }:
            return {
                "status": "failed-unsafe",
                "failure_class": "adapter-contract-invalid",
            }
        try:
            validate_payload(result)
        except JournalError:
            return {
                "status": "failed-unsafe",
                "failure_class": "adapter-sensitive-payload",
            }
        return result

    def _launch_adapter(
        self,
        task: Mapping[str, Any],
        attempt_id: str,
        deadline_at: str,
    ) -> tuple[Any, dict[str, Any]]:
        launcher = getattr(self.adapter, "launch_task", None)
        if launcher is None:
            raise SchedulerError("adapter has no two-phase launch seam")
        handle = launcher(
            task,
            run_id=self.run_id,
            attempt_id=attempt_id,
            generation=self.generation,
            deadline_at=deadline_at,
        )
        evidence_getter = getattr(handle, "journal_evidence", None)
        if not callable(evidence_getter):
            raise SchedulerError("launch handle has no journal evidence")
        evidence = evidence_getter()
        if not isinstance(evidence, Mapping):
            raise SchedulerError("launch evidence must be a mapping")
        evidence = dict(evidence)
        pid = evidence.get("wrapper_pid")
        fingerprint = evidence.get("wrapper_start_fingerprint")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise SchedulerError("launch evidence has no wrapper pid")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise SchedulerError("launch evidence has no start fingerprint")
        validate_payload(evidence)
        return handle, evidence

    def _collect_adapter(self, handle: Any) -> Mapping[str, Any]:
        collector = getattr(self.adapter, "collect_task", None)
        if collector is None:
            raise SchedulerError("adapter has no two-phase collect seam")
        return self._normalize_adapter_result(collector(handle))

    def _prepare_resource(self, task: Mapping[str, Any], attempt_id: str) -> bool:
        if task["workspace"]["kind"] != "isolated-writer":
            return True
        resource_id = f"{self.run_id}:{task['id']}:worktree"
        intended_path = str(
            (
                Path(self.plan["repo_root"]).parent
                / ".agent-run-worktrees"
                / self.run_id
                / task["id"]
            ).resolve()
        )
        ownership = {
            "resource_id": resource_id,
            "resource_type": "worktree",
            "created_by_run_id": self.run_id,
            "task_id": task["id"],
            "attempt_id": attempt_id,
            "repo_root": self.plan["repo_root"],
            "path": intended_path,
            "branch": f"agent-run/{self.run_id}/{task['id']}",
            "base_sha": self.plan["base_sha"],
            "ledger_slug": self.plan["ledger_slug"],
            "generation": self.generation,
            "fencing_token": self.fencing_token,
            "own": task["workspace"]["own"],
            "status": "intent",
        }
        prior = fold_events(self.journal.read())["resources"].get(resource_id)
        if prior and prior.get("status") in {"created", "reconciled"}:
            for field in (
                "created_by_run_id",
                "repo_root",
                "path",
                "branch",
                "base_sha",
                "ledger_slug",
            ):
                if prior.get(field) != ownership[field]:
                    return False
            if prior.get("generation") == self.generation:
                return True
            reconciler = getattr(self.adapter, "reconcile_resource", None)
            if reconciler is None:
                return False
            try:
                observation = reconciler(
                    task,
                    ownership=dict(prior),
                    current_ownership=dict(ownership),
                )
                if (
                    not isinstance(observation, Mapping)
                    or observation.get("status") != "created"
                ):
                    return False
                for field in ("path", "branch", "base_sha", "ledger_slug"):
                    if field in observation and observation[field] != ownership[field]:
                        return False
            except Exception:
                return False
            self._emit(
                "resource_reconciled",
                task_id=task["id"],
                attempt_id=attempt_id,
                payload={**ownership, **dict(observation), "status": "reconciled"},
            )
            return True
        self._emit(
            "resource_intent",
            task_id=task["id"],
            attempt_id=attempt_id,
            payload=ownership,
        )
        preparer = getattr(self.adapter, "prepare_resource", None)
        try:
            confirmation = (
                preparer(task, ownership=dict(ownership))
                if preparer
                else {"status": "created"}
            )
            if (
                not isinstance(confirmation, Mapping)
                or confirmation.get("status") != "created"
            ):
                raise SchedulerError("resource creation was not confirmed")
            for field in ("path", "branch", "base_sha", "ledger_slug"):
                if field in confirmation and confirmation[field] != ownership[field]:
                    raise SchedulerError(
                        f"resource confirmation drifted from intent: {field}"
                    )
        except Exception as exc:
            self._emit(
                "resource_failed",
                task_id=task["id"],
                attempt_id=attempt_id,
                payload={
                    **ownership,
                    "status": "failed",
                    "failure_class": "resource-create-failed",
                    "detail": type(exc).__name__,
                },
            )
            return False
        self._emit(
            "resource_created",
            task_id=task["id"],
            attempt_id=attempt_id,
            payload={**ownership, **dict(confirmation), "status": "created"},
        )
        return True

    def request_cancel(self) -> dict[str, Any]:
        """Request graceful drain from the controller process; never kill a child."""
        current_controller = self.journal.current_controller()
        if current_controller is None:
            return self.status()
        request_cancel_file(
            self.cancel_request_path,
            run_id=self.run_id,
            generation=current_controller[0],
            fencing_token=current_controller[1],
            timestamp=_iso_timestamp(self.clock.time()),
        )
        if self.lease.held:
            self._poll_cancel_request()
        return self.status()

    def _poll_cancel_request(self) -> bool:
        try:
            request = read_cancel_file(self.cancel_request_path)
        except JournalError:
            # Corruption or an authority-free request can never cancel a run.
            return False
        if request is None or (
            request["run_id"] != self.run_id
            or request["generation"] != self.generation
            or request["fencing_token"] != self.fencing_token
        ):
            return False
        self._cancel_requested.set()
        current = fold_events(self.journal.read())
        if not current["cancel_requested"] and current["status"] not in TERMINAL:
            self._emit(
                "cancel_requested",
                payload={
                    "mode": "graceful-drain",
                    "request_id": request["request_id"],
                },
            )
        return True

    def status(self) -> dict[str, Any]:
        state = fold_events(self.journal.read())
        now = self.clock.time()
        remaining = []
        for task in state["tasks"].values():
            if task["status"] == "running" and task.get("deadline_at"):
                try:
                    deadline_epoch = dt.datetime.fromisoformat(
                        task["deadline_at"].replace("Z", "+00:00")
                    ).timestamp()
                    remaining.append(max(0, int(deadline_epoch - now)))
                except (TypeError, ValueError):
                    pass
        state["eta_seconds"] = max(remaining, default=0)
        counts = defaultdict(int)
        for task in state["tasks"].values():
            counts[task["status"]] += 1
        state["progress"] = {
            "total": len(self.tasks),
            "terminal": sum(counts[name] for name in TERMINAL),
            "by_status": dict(sorted(counts.items())),
        }
        return state

    def _finalize_integration(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        finalizer = getattr(self.adapter, "finalize_run", None)
        if finalizer is None:
            return {
                "status": "succeeded",
                "integration_head": str(self.plan.get("base_sha") or "read-only"),
                "artifact_path": "none",
            }
        try:
            integration = finalizer(
                self.plan,
                state,
                run_id=self.run_id,
                generation=self.generation,
                fencing_token=self.fencing_token,
            )
            if not isinstance(integration, Mapping):
                raise SchedulerError("finalize_run must return a mapping")
            integration = dict(integration)
            validate_payload(integration)
            return integration
        except (Exception, JournalError) as exc:
            return {
                "status": "failed-unsafe",
                "failure_class": "integration-hook-invalid",
                "detail": type(exc).__name__,
            }

    def _resume_running(self, state: Mapping[str, Any]) -> None:
        """Reconcile previously claimed tasks without ever replaying them."""
        reconciler = getattr(self.adapter, "reconcile_task", None)
        for task_id, task_state in state["tasks"].items():
            if task_state["status"] not in {"running", "dispatch-intent"}:
                continue
            attempt_id = task_state["current_attempt_id"]
            if reconciler is None:
                self._emit(
                    "task_failed_unsafe",
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload={
                        "failure_class": "ambiguous-live-wrapper",
                        "replayed": False,
                    },
                )
                continue
            try:
                observation = self._normalize_adapter_result(
                    reconciler(
                        self.tasks[task_id],
                        run_id=self.run_id,
                        attempt_id=attempt_id,
                        generation=self.generation,
                        prior_state=dict(task_state),
                    )
                )
            except Exception:
                observation = {
                    "status": "failed-unsafe",
                    "failure_class": "reconciliation-contract-invalid",
                    "replayed": False,
                }
            observed_status = observation.get("status")
            event_type = {
                "succeeded": "task_succeeded",
                "failed": "task_failed",
                "timed-out": "task_timed_out",
                "failed-unsafe": "task_failed_unsafe",
            }.get(observed_status)
            if event_type is None:
                self._emit(
                    "task_failed_unsafe",
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload={
                        "failure_class": "unreconciled-live-wrapper",
                        "replayed": False,
                    },
                )
            else:
                self._emit(
                    event_type,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload=dict(observation),
                )

    def run(self, *, resume: bool = False) -> dict[str, Any]:
        try:
            self.lease.acquire()
        except LeaseContended as exc:
            raise AlreadyControlled(str(exc)) from exc
        executor: ThreadPoolExecutor | None = None
        try:
            prior = fold_events(self.journal.read())
            if prior["status"] in {"completed", "canceled", "failed", "failed-unsafe"}:
                return self.status()
            self.generation = self.journal.next_generation()
            self.fencing_token = _fencing_token(self.run_id, self.generation)
            self._event_sequence = sum(
                1
                for event in self.journal.read()
                if event["generation"] == self.generation
            )
            self._emit(
                "controller_acquired",
                payload={
                    "action": "resume" if resume or prior["event_count"] else "start",
                    "controller_owner": "scheduler",
                },
            )
            self._emit(
                "run_resumed" if resume or prior["event_count"] else "run_started"
            )
            if prior["event_count"]:
                self._resume_running(prior)

            total_limit = self.plan["budgets"]["total_concurrency"]
            writer_limit = self.plan["budgets"]["writer_concurrency"]
            executor = ThreadPoolExecutor(
                max_workers=total_limit, thread_name_prefix="agent-orchestrate"
            )
            running: dict[Future, dict[str, Any]] = {}
            family_running: defaultdict[str, int] = defaultdict(int)
            writer_running = 0
            attempt_ordinals: defaultdict[str, int] = defaultdict(int)
            for task_id, task_state in fold_events(self.journal.read())[
                "tasks"
            ].items():
                attempt_ordinals[task_id] = len(task_state["attempts"])
            retry_ready_at: dict[str, float] = {}
            for event in self.journal.read():
                if event["event_type"] != "task_retry_scheduled":
                    continue
                raw = event["payload"].get("retry_not_before")
                if not isinstance(raw, str):
                    continue
                try:
                    retry_ready_at[str(event["task_id"])] = dt.datetime.fromisoformat(
                        raw.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    continue

            while True:
                self._poll_cancel_request()
                state = fold_events(self.journal.read())
                task_states = state["tasks"]
                if state["cancel_requested"]:
                    self._cancel_requested.set()

                # Propagate failed dependencies and cancel every task never admitted.
                for task_id in self.plan["topological_order"]:
                    task = self.tasks[task_id]
                    status = task_states.get(task_id, {}).get("status", "pending")
                    if status in TERMINAL or status in {
                        "running",
                        "dispatch-intent",
                        "retry-pending",
                    }:
                        continue
                    dependencies = [
                        task_states.get(dep, {}).get("status", "pending")
                        for dep in task["depends_on"]
                    ]
                    if any(dep in FINAL_FAILURE for dep in dependencies):
                        attempt = _attempt_id(
                            self.run_id, task_id, max(1, attempt_ordinals[task_id] + 1)
                        )
                        self._emit(
                            "task_blocked",
                            task_id=task_id,
                            attempt_id=attempt,
                            payload={"failure_class": "dependency-failed"},
                        )
                    elif self._cancel_requested.is_set():
                        attempt = _attempt_id(
                            self.run_id, task_id, max(1, attempt_ordinals[task_id] + 1)
                        )
                        self._emit(
                            "task_canceled",
                            task_id=task_id,
                            attempt_id=attempt,
                            payload={"failure_class": "cancel-before-dispatch"},
                        )

                state = fold_events(self.journal.read())
                task_states = state["tasks"]
                admitted = False
                if not self._cancel_requested.is_set():
                    review_ids = [
                        task_id
                        for task_id, task in self.tasks.items()
                        if task.get("reviewer_for")
                    ]
                    producer_ids = [
                        task_id for task_id in self.tasks if task_id not in review_ids
                    ]
                    pending_reviews = [
                        task_id
                        for task_id in review_ids
                        if task_states.get(task_id, {}).get("status", "pending")
                        not in TERMINAL
                    ]
                    producers_succeeded = all(
                        task_states.get(task_id, {}).get("status") == "succeeded"
                        for task_id in producer_ids
                    )
                    if pending_reviews and producers_succeeded and state.get("integration") is None:
                        integration = self._finalize_integration(state)
                        if integration.get("status") == "succeeded":
                            self._emit("integration_succeeded", payload=integration)
                        else:
                            unsafe = integration.get("status") != "failed"
                            self._emit("integration_failed", payload=integration)
                            for review_id in pending_reviews:
                                attempt = _attempt_id(
                                    self.run_id,
                                    review_id,
                                    max(1, attempt_ordinals[review_id] + 1),
                                )
                                self._emit(
                                    "task_blocked",
                                    task_id=review_id,
                                    attempt_id=attempt,
                                    payload={
                                        "failure_class": (
                                            "integration-failed-unsafe"
                                            if unsafe
                                            else "integration-failed"
                                        )
                                    },
                                )
                            state = fold_events(self.journal.read())
                            task_states = state["tasks"]
                        state = fold_events(self.journal.read())
                        task_states = state["tasks"]
                    for task_id in self.plan["topological_order"]:
                        if len(running) >= total_limit:
                            break
                        task = self.tasks[task_id]
                        status = task_states.get(task_id, {}).get("status", "pending")
                        if status not in {
                            "pending",
                            "retry-pending",
                            "dispatch-intent",
                        }:
                            continue
                        if (
                            status == "retry-pending"
                            and self.clock.time() < retry_ready_at.get(task_id, 0.0)
                        ):
                            continue
                        if any(
                            task_states.get(dep, {}).get("status") != "succeeded"
                            for dep in task["depends_on"]
                        ):
                            continue
                        if task.get("reviewer_for"):
                            if (state.get("integration") or {}).get("status") != "succeeded":
                                continue
                            if not all(
                                task_states.get(producer_id, {}).get("status") == "succeeded"
                                for producer_id in producer_ids
                            ):
                                continue
                        family = task["family"]
                        family_limit = min(
                            task["family_limit"],
                            self.plan["budgets"]["family_concurrency"].get(
                                family, total_limit
                            ),
                        )
                        is_writer = task["workspace"]["kind"] == "isolated-writer"
                        resource_prepared = False
                        if family_running[family] >= family_limit or (
                            is_writer and writer_running >= writer_limit
                        ):
                            continue
                        deadline_at: str
                        if status == "dispatch-intent":
                            attempt_id = task_states[task_id]["current_attempt_id"]
                            deadline_at = str(task_states[task_id].get("deadline_at") or "")
                            if not deadline_at:
                                self._emit(
                                    "task_failed_unsafe",
                                    task_id=task_id,
                                    attempt_id=attempt_id,
                                    payload={"failure_class": "dispatch-intent-deadline-missing"},
                                )
                                continue
                        else:
                            attempt_ordinals[task_id] += 1
                            attempt_id = _attempt_id(
                                self.run_id, task_id, attempt_ordinals[task_id]
                            )
                            deadline_at = _iso_timestamp(
                                self.clock.time() + task["deadline_seconds"]
                            )
                            self._emit(
                                "dispatch_intent",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                payload={
                                    "deadline_at": deadline_at,
                                    "task_shape": task["task_shape"],
                                },
                            )
                        if status == "retry-pending" and is_writer:
                            if not self._prepare_resource(task, attempt_id):
                                self._emit(
                                    "task_failed_unsafe",
                                    task_id=task_id,
                                    attempt_id=attempt_id,
                                    payload={
                                        "status": "failed-unsafe",
                                        "failure_class": (
                                            "writer-retry-resource-unverifiable"
                                        ),
                                        "resource_preserved": True,
                                    },
                                )
                                continue
                            resource_prepared = True
                            retry_preparer = getattr(
                                self.adapter, "prepare_retry", None
                            )
                            retry_check: Mapping[str, Any] | None = None
                            try:
                                retry_check = (
                                    retry_preparer(
                                        task,
                                        run_id=self.run_id,
                                        attempt_id=attempt_id,
                                        generation=self.generation,
                                        fencing_token=self.fencing_token,
                                    )
                                    if retry_preparer is not None
                                    else None
                                )
                                if (
                                    not isinstance(retry_check, Mapping)
                                    or retry_check.get("status") != "succeeded"
                                ):
                                    raise SchedulerError(
                                        str(
                                            (retry_check or {}).get("failure_class")
                                            or "writer-retry-safety-unverifiable"
                                        )
                                    )
                                validate_payload(retry_check)
                            except (Exception, JournalError) as exc:
                                failure_class = (
                                    str(retry_check.get("failure_class"))
                                    if isinstance(retry_check, Mapping)
                                    and retry_check.get("failure_class")
                                    else "writer-retry-safety-unverifiable"
                                )
                                self._emit(
                                    "task_failed_unsafe",
                                    task_id=task_id,
                                    attempt_id=attempt_id,
                                    payload={
                                        "status": "failed-unsafe",
                                        "failure_class": failure_class,
                                        "resource_preserved": True,
                                        "detail": type(exc).__name__,
                                    },
                                )
                                continue
                            retry_ready_at.pop(task_id, None)
                        if task["depends_on"] and not task.get("reviewer_for"):
                            dependency_preparer = getattr(
                                self.adapter, "prepare_dependencies", None
                            )
                            if dependency_preparer is None:
                                self._emit(
                                    "task_failed_unsafe",
                                    task_id=task_id,
                                    attempt_id=attempt_id,
                                    payload={
                                        "status": "failed-unsafe",
                                        "failure_class": "dependency-context-adapter-unavailable",
                                    },
                                )
                                continue
                            try:
                                dependency_context = dependency_preparer(
                                    task,
                                    fold_events(self.journal.read()),
                                    run_id=self.run_id,
                                    attempt_id=attempt_id,
                                    generation=self.generation,
                                    fencing_token=self.fencing_token,
                                )
                                if (
                                    not isinstance(dependency_context, Mapping)
                                    or dependency_context.get("status") != "succeeded"
                                ):
                                    raise SchedulerError(
                                        "dependency context was not confirmed"
                                    )
                                validate_payload(dependency_context)
                            except (Exception, JournalError) as exc:
                                self._emit(
                                    "task_failed_unsafe",
                                    task_id=task_id,
                                    attempt_id=attempt_id,
                                    payload={
                                        "status": "failed-unsafe",
                                        "failure_class": "dependency-context-unverifiable",
                                        "detail": type(exc).__name__,
                                    },
                                )
                                continue
                            self._emit(
                                "dependency_context_prepared",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                payload=dict(dependency_context),
                            )
                        if task.get("reviewer_for"):
                            preparer = getattr(self.adapter, "prepare_review", None)
                            if preparer is None:
                                self._emit(
                                    "task_failed_unsafe",
                                    task_id=task_id,
                                    attempt_id=attempt_id,
                                    payload={
                                        "status": "failed-unsafe",
                                        "failure_class": "review-context-adapter-unavailable",
                                    },
                                )
                                continue
                            try:
                                review_context = preparer(
                                    task,
                                    fold_events(self.journal.read()),
                                    run_id=self.run_id,
                                    attempt_id=attempt_id,
                                    generation=self.generation,
                                    fencing_token=self.fencing_token,
                                )
                                if (
                                    not isinstance(review_context, Mapping)
                                    or review_context.get("status") != "succeeded"
                                ):
                                    raise SchedulerError("review context was not confirmed")
                                validate_payload(review_context)
                            except (Exception, JournalError) as exc:
                                self._emit(
                                    "task_failed_unsafe",
                                    task_id=task_id,
                                    attempt_id=attempt_id,
                                    payload={
                                        "status": "failed-unsafe",
                                        "failure_class": "review-context-unverifiable",
                                        "detail": type(exc).__name__,
                                    },
                                )
                                continue
                            self._emit(
                                "review_context_prepared",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                payload=dict(review_context),
                            )
                        if not resource_prepared and not self._prepare_resource(
                            task, attempt_id
                        ):
                            self._emit(
                                "task_failed",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                payload={"failure_class": "resource-create-failed"},
                            )
                            continue
                        launcher = (
                            getattr(self.adapter, "launch_task", None)
                            if getattr(
                                self.adapter,
                                "two_phase_process",
                                hasattr(self.adapter, "launch_task"),
                            )
                            else None
                        )
                        handle = None
                        if launcher is not None:
                            try:
                                handle, launch_evidence = self._launch_adapter(
                                    task, attempt_id, deadline_at
                                )
                            except Exception as exc:
                                self._emit(
                                    "task_failed_unsafe",
                                    task_id=task_id,
                                    attempt_id=attempt_id,
                                    payload={
                                        "status": "failed-unsafe",
                                        "failure_class": "wrapper-launch-unconfirmed",
                                        "detail": type(exc).__name__,
                                    },
                                )
                                continue
                            self._emit(
                                "dispatch_claimed",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                payload=launch_evidence,
                            )
                            future = executor.submit(self._collect_adapter, handle)
                        else:
                            self._emit(
                                "dispatch_claimed",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                payload={
                                    "wrapper_pid": None,
                                    "wrapper_start_fingerprint": None,
                                    "deadline_at": deadline_at,
                                },
                            )
                            future = executor.submit(self._call_adapter, task, attempt_id)
                        running[future] = {
                            "task": task,
                            "attempt_id": attempt_id,
                            "deadline_epoch": self.clock.time()
                            + task["deadline_seconds"],
                            "family": family,
                            "writer": is_writer,
                            "adapter_owns_deadline": bool(
                                launcher is not None
                                or getattr(self.adapter, "owns_deadline", False)
                            ),
                        }
                        family_running[family] += 1
                        writer_running += int(is_writer)
                        admitted = True

                if running:
                    done, _ = wait(
                        tuple(running), timeout=0.02, return_when=FIRST_COMPLETED
                    )
                    # A fake clock may advance independently; real adapters rely on the runner timeout contract.
                    expired = [
                        future
                        for future, meta in running.items()
                        if self.clock.time() >= meta["deadline_epoch"]
                        and not meta["adapter_owns_deadline"]
                        and not future.done()
                    ]
                    for future in set(done) | set(expired):
                        meta = running.pop(future)
                        task = meta["task"]
                        task_id = task["id"]
                        attempt_id = meta["attempt_id"]
                        family_running[meta["family"]] -= 1
                        writer_running -= int(meta["writer"])
                        if future in expired and not future.done():
                            future.cancel()
                            result = {
                                "status": "timed-out",
                                "failure_class": "deadline-exceeded",
                            }
                        else:
                            try:
                                result = dict(future.result())
                            except Exception as exc:
                                result = {
                                    "status": "failed",
                                    "failure_class": "adapter-transient",
                                    "detail": type(exc).__name__,
                                }
                        status = result.get("status")
                        if status == "succeeded":
                            self._emit(
                                "task_succeeded",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                payload=result,
                            )
                        else:
                            failure_class = str(
                                result.get("failure_class") or "task-quality-failure"
                            )
                            retryable = failure_class in task["retry"]["retry_on"]
                            attempts_used = attempt_ordinals[task_id]
                            event_type = (
                                "task_timed_out"
                                if status == "timed-out"
                                else "task_failed_unsafe"
                                if status == "failed-unsafe"
                                else "task_failed"
                            )
                            self._emit(
                                event_type,
                                task_id=task_id,
                                attempt_id=attempt_id,
                                payload={**result, "failure_class": failure_class},
                            )
                            if (
                                retryable
                                and attempts_used < task["retry"]["max_attempts"]
                                and not self._cancel_requested.is_set()
                            ):
                                retry_after = _retry_delay_seconds(attempts_used)
                                ready_at = self.clock.time() + retry_after
                                retry_ready_at[task_id] = ready_at
                                self._emit(
                                    "task_retry_scheduled",
                                    task_id=task_id,
                                    attempt_id=attempt_id,
                                    payload={
                                        "failure_class": failure_class,
                                        "next_ordinal": attempts_used + 1,
                                        "retry_after_seconds": retry_after,
                                        "retry_not_before": _iso_timestamp(ready_at),
                                    },
                                )
                elif not admitted:
                    final_state = fold_events(self.journal.read())
                    statuses = [
                        final_state["tasks"].get(task_id, {}).get("status", "pending")
                        for task_id in self.tasks
                    ]
                    if all(status in TERMINAL for status in statuses):
                        break
                    retry_waits = [
                        ready_at - self.clock.time()
                        for task_id, ready_at in retry_ready_at.items()
                        if final_state["tasks"].get(task_id, {}).get("status")
                        == "retry-pending"
                        and ready_at > self.clock.time()
                    ]
                    if retry_waits:
                        self.clock.sleep(min(retry_waits))
                        continue
                    raise SchedulerError(
                        "scheduler made no progress; graph/admission state is inconsistent"
                    )

                if self._cancel_requested.is_set():
                    eta = max(
                        [
                            max(0, int(meta["deadline_epoch"] - self.clock.time()))
                            for meta in running.values()
                        ],
                        default=0,
                    )
                    current = fold_events(self.journal.read())
                    if (
                        current["status"] != "canceling"
                        or current.get("eta_seconds") != eta
                    ):
                        self._emit("run_canceling", payload={"eta_seconds": eta})

            final_state = fold_events(self.journal.read())
            statuses = [
                final_state["tasks"][task_id]["status"] for task_id in self.tasks
            ]
            cleanup = getattr(self.adapter, "terminal_cleanup", None)
            if cleanup is not None and not self._cancel_requested.is_set():
                try:
                    observed_cleanup = cleanup(
                        self.plan,
                        final_state,
                        run_id=self.run_id,
                        generation=self.generation,
                        fencing_token=self.fencing_token,
                    )
                    if not isinstance(observed_cleanup, Mapping):
                        raise SchedulerError("terminal cleanup must return a mapping")
                    existing_cleanup = final_state.get("cleanup", {})
                    for cleanup_id, outcomes in observed_cleanup.items():
                        if cleanup_id in existing_cleanup:
                            continue
                        if not isinstance(outcomes, Mapping):
                            raise SchedulerError("cleanup outcomes must be mappings")
                        self.record_terminal_cleanup(
                            str(cleanup_id),
                            self.controller_attempt_id,
                            process=outcomes.get(
                                "process", {"status": "not-applicable"}
                            ),
                            worktree=outcomes.get(
                                "worktree", {"status": "not-applicable"}
                            ),
                            branch=outcomes.get(
                                "branch", {"status": "not-applicable"}
                            ),
                        )
                    final_state = fold_events(self.journal.read())
                except Exception:
                    # Cleanup is secondary.  Exact ownership checks preserve
                    # anything that cannot be safely removed, and a teardown
                    # failure must not rewrite the primary run result.
                    pass
            if self._cancel_requested.is_set():
                self._emit("run_canceled")
            elif (
                (final_state.get("integration") or {}).get("status") == "failed-unsafe"
                or any(status == "failed-unsafe" for status in statuses)
            ):
                self._emit("run_failed_unsafe")
            elif any(status in FINAL_FAILURE for status in statuses):
                self._emit("run_failed")
            else:
                existing_integration = final_state.get("integration")
                if existing_integration is not None:
                    if existing_integration.get("status") == "succeeded":
                        self._emit(
                            "run_completed", payload={"integration_status": "succeeded"}
                        )
                    else:
                        self._emit("run_failed_unsafe")
                elif getattr(self.adapter, "finalize_run", None) is None:
                    self._emit("run_completed")
                else:
                    integration = self._finalize_integration(final_state)
                    integration_status = integration.get("status")
                    if integration_status == "succeeded":
                        self._emit("integration_succeeded", payload=integration)
                        self._emit(
                            "run_completed", payload={"integration_status": "succeeded"}
                        )
                    else:
                        unsafe = integration_status == "failed-unsafe"
                        safe_payload = dict(integration)
                        if integration_status not in {"failed", "failed-unsafe"}:
                            unsafe = True
                            safe_payload = {
                                "status": "failed-unsafe",
                                "failure_class": "integration-contract-invalid",
                            }
                        self._emit("integration_failed", payload=safe_payload)
                        self._emit("run_failed_unsafe" if unsafe else "run_failed")
            return self.status()
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=False)
            self.lease.release()

    def record_terminal_cleanup(
        self,
        task_id: str,
        attempt_id: str,
        *,
        process: Mapping[str, Any],
        worktree: Mapping[str, Any],
        branch: Mapping[str, Any],
    ) -> None:
        """Record teardown outcomes independently; no success masks another failure."""
        outcomes = {
            "process": dict(process),
            "worktree": dict(worktree),
            "branch": dict(branch),
        }
        for name, outcome in outcomes.items():
            if outcome.get("status") not in {
                "succeeded",
                "failed",
                "preserved",
                "not-applicable",
            }:
                raise SchedulerError(f"cleanup outcome {name} has invalid status")
        self._emit(
            "cleanup_recorded", task_id=task_id, attempt_id=attempt_id, payload=outcomes
        )


class FakeAdapter:
    """Deterministic scripted adapter for offline tests and functional QA."""

    def __init__(
        self,
        script: Mapping[str, list[Mapping[str, Any] | Callable[..., Mapping[str, Any]]]]
        | None = None,
        *,
        reconcile: Mapping[str, Mapping[str, Any]] | None = None,
        finalize: Mapping[str, Any] | Callable[..., Mapping[str, Any]] | None = None,
    ):
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self.reconcile = dict(reconcile or {})
        self.finalize = finalize
        self.finalize_calls = 0
        self.calls: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.family_active: defaultdict[str, int] = defaultdict(int)
        self.max_family_active: defaultdict[str, int] = defaultdict(int)

    def prepare_resource(
        self, task: Mapping[str, Any], *, ownership: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.resources.append(dict(ownership))
        return {"status": "created", "path_ref": f"managed:{task['id']}"}

    def prepare_dependencies(
        self,
        task: Mapping[str, Any],
        state: Mapping[str, Any],
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        dependencies = list(task.get("depends_on") or [])
        if any(
            state.get("tasks", {}).get(task_id, {}).get("status") != "succeeded"
            for task_id in dependencies
        ):
            return {"status": "failed-unsafe"}
        return {
            "status": "succeeded",
            "dependency_bundle_path": "/private/fake-dependencies.json",
            "dependency_bundle_sha256": "0" * 64,
            "dependency_count": len(dependencies),
        }

    def run_task(
        self, task: Mapping[str, Any], *, run_id: str, attempt_id: str, generation: int
    ) -> Mapping[str, Any]:
        family = str(task["family"])
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.family_active[family] += 1
            self.max_family_active[family] = max(
                self.max_family_active[family], self.family_active[family]
            )
            call_index = sum(1 for call in self.calls if call["task_id"] == task["id"])
            self.calls.append(
                {
                    "task_id": task["id"],
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "generation": generation,
                }
            )
        try:
            rows = self.script.get(task["id"], [{"status": "succeeded"}])
            item = rows[min(call_index, len(rows) - 1)]
            if callable(item):
                return item(
                    task=task,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    generation=generation,
                )
            return dict(item)
        finally:
            with self._lock:
                self.active -= 1
                self.family_active[family] -= 1

    def reconcile_task(self, task: Mapping[str, Any], **_kwargs) -> Mapping[str, Any]:
        return dict(
            self.reconcile.get(
                task["id"],
                {
                    "status": "failed-unsafe",
                    "failure_class": "unreconciled-live-wrapper",
                },
            )
        )

    def reconcile_resource(
        self, _task: Mapping[str, Any], *, ownership: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {
            "status": "created",
            "path": ownership["path"],
            "branch": ownership["branch"],
            "base_sha": ownership["base_sha"],
            "ledger_slug": ownership["ledger_slug"],
        }

    def finalize_run(
        self, plan: Mapping[str, Any], state: Mapping[str, Any], **kwargs
    ) -> Mapping[str, Any]:
        self.finalize_calls += 1
        if callable(self.finalize):
            return self.finalize(plan=plan, state=state, **kwargs)
        return dict(
            self.finalize or {"status": "succeeded", "integration_head": "fake"}
        )


class FakeClock:
    def __init__(self, epoch: float = 1_750_000_000.0):
        self.epoch = epoch
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self.epoch

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.epoch += seconds


class FakeProcessAdapter:
    """PID/start-fingerprint fixture without signalling a host process."""

    def __init__(self):
        self.live: dict[int, str] = {}

    def add(self, pid: int, fingerprint: str | None = None) -> str:
        value = fingerprint or process_start_fingerprint(pid)
        self.live[pid] = value
        return value

    def matches(self, pid: int, fingerprint: str) -> bool:
        return self.live.get(pid) == fingerprint

    def finish(self, pid: int) -> None:
        self.live.pop(pid, None)


class FakeWorktreeAdapter:
    """Write-ahead ownership fixture; it never invokes Git or removes paths."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.intents: list[dict[str, Any]] = []

    def create(self, ownership: Mapping[str, Any]) -> Mapping[str, Any]:
        self.intents.append(dict(ownership))
        if self.fail:
            raise SchedulerError("injected worktree failure")
        return {"status": "created", "path_ref": "fake-worktree"}
