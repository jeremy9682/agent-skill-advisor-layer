"""Concrete controller adapter for the V1 orchestration scheduler.

This module deliberately owns no routing policy.  It turns a *compiled* plan
into a narrow, auditable call to :class:`NativeAgentRunBridge`, and keeps the
two state authorities separate: the orchestration JSONL is scheduler state;
the checkpoint ledger is only a governance hand-off record.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from typing import Any, Mapping, Protocol, Sequence
import uuid
import re
import stat
import time

from .bridge import (
    BridgeError,
    BridgeLaunch,
    NativeAgentRunBridge,
    review_verdict_failure,
)
from .journal import write_replaceable_manifest
from .journal import EventJournal
from .join import JoinDispute, join_candidates
from .scheduler import Scheduler
from .worktree import (
    AcceptanceResult,
    CandidateCommit,
    ResourceOwnership,
    WorktreeError,
    WorktreeManager,
)


class RuntimeErrorSafe(RuntimeError):
    """A runtime input or observed state was unsafe to consume."""


CHECKPOINT_SEAT_RE = re.compile(
    r"^(?:claude|codex|fable|opus|sonnet|human|founder)"
    r"(?:-[a-z]+(?:-[a-z]+)*)?$"
)
CONTROLLER_SEAT = "codex-orchestrator"
DEPENDENCY_BUNDLE_MAX_BYTES = 256 * 1024
MANIFEST_VERSION = 1


def _compiled_execution_seat(task: Mapping[str, Any]) -> str:
    """Return only the compiler-projected seat, never task annotations.

    ``metadata`` and top-level task fields are untrusted plan input.  The plan
    compiler places authority under ``binding``; a caller bypassing compilation
    fails closed here rather than smuggling a checkpoint owner override.
    """

    if "seat" in task:
        raise RuntimeErrorSafe("task may not override the compiled execution seat")
    binding = task.get("binding")
    if not isinstance(binding, Mapping):
        raise RuntimeErrorSafe("task has no compiled routing binding")
    seat = binding.get("seat")
    if not isinstance(seat, str) or not CHECKPOINT_SEAT_RE.fullmatch(seat):
        raise RuntimeErrorSafe("compiled routing seat is outside checkpoint vocabulary")
    return seat


class CheckpointLedger(Protocol):
    def open(self, *, task: Mapping[str, Any], cwd: Path, run_id: str, attempt_id: str) -> str: ...
    def claim(self, event_id: str) -> None: ...
    def close(self, event_id: str, *, outcome: str) -> None: ...
    def bind_existing(self, event_id: str, *, seat: str) -> None: ...


def _private_atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, mode)
    finally:
        if temp.exists():
            temp.unlink()


class AgentLedgerCLI:
    """Small injection-friendly facade over the existing ``agent-ledger`` CLI."""

    def __init__(
        self,
        *,
        slug: str,
        intent_ref: str,
        repo_root: Path,
        binary: str = "agent-ledger",
        runner=subprocess.run,
    ) -> None:
        self.slug, self.intent_ref = slug, intent_ref
        self.repo_root, self.binary, self.runner = repo_root.resolve(), binary, runner
        self._event_seats: dict[str, str] = {}

    def _run(self, argv: Sequence[str]) -> str:
        completed = self.runner(list(argv), capture_output=True, text=True, check=False)
        if completed.returncode:
            raise RuntimeErrorSafe("checkpoint ledger command failed")
        return (completed.stdout or "").strip()

    def open(self, *, task: Mapping[str, Any], cwd: Path, run_id: str, attempt_id: str) -> str:
        execution_seat = _compiled_execution_seat(task)
        if not CHECKPOINT_SEAT_RE.fullmatch(CONTROLLER_SEAT):
            raise RuntimeErrorSafe("controller seat is outside checkpoint vocabulary")
        head = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        branch = subprocess.run(["git", "-C", str(cwd), "symbolic-ref", "--short", "HEAD"], capture_output=True, text=True, check=False)
        if head.returncode or branch.returncode or not branch.stdout.strip():
            raise RuntimeErrorSafe("checkpoint cwd is not a Git worktree")
        worktree = f"{cwd.resolve()} @ {branch.stdout.strip()} @ {head.stdout.strip()}"
        argv = [
            self.binary, "open", self.slug,
            "--intent-ref", self.intent_ref,
            "--from-seat", CONTROLLER_SEAT,
            "--to-seat", execution_seat,
            "--worktree", worktree,
            "--verification", "orchestration result envelope and frozen acceptance",
            "--next-action", f"complete governed task {task['id']}",
        ]
        own = task.get("workspace", {}).get("own", [])
        denied = task.get("workspace", {}).get("do_not_touch", [])
        if own:
            argv.extend(["--own", *own])
        if denied:
            argv.extend(["--do-not-touch", *denied])
        output = self._run(argv)
        # Current CLI prints only the generated id; retain JSON support for a
        # future machine-envelope version without accepting arbitrary prose.
        for line in reversed(output.splitlines()):
            line = line.strip()
            if line.startswith("evt-") and " " not in line:
                self._event_seats[line] = execution_seat
                return line
        for line in reversed(output.splitlines()):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            event_id = value.get("event_id") if isinstance(value, Mapping) else None
            if isinstance(event_id, str) and event_id.startswith("evt-"):
                self._event_seats[event_id] = execution_seat
                return event_id
        raise RuntimeErrorSafe("checkpoint open did not return an event id")

    def claim(self, event_id: str) -> None:
        seat = self._event_seats.get(event_id)
        if seat is None:
            raise RuntimeErrorSafe("checkpoint event has no compiled execution seat")
        self._run([self.binary, "claim", self.slug, event_id, "--seat", seat, "--note", "orchestrated dispatch"])

    def close(self, event_id: str, *, outcome: str) -> None:
        seat = self._event_seats.get(event_id)
        if seat is None:
            raise RuntimeErrorSafe("checkpoint event has no compiled execution seat")
        self._run([self.binary, "close", self.slug, event_id, "--seat", seat, "--outcome", outcome])

    def bind_existing(self, event_id: str, *, seat: str) -> None:
        if not event_id.startswith("evt-") or not CHECKPOINT_SEAT_RE.fullmatch(seat):
            raise RuntimeErrorSafe("recovered checkpoint identity is invalid")
        existing = self._event_seats.get(event_id)
        if existing is not None and existing != seat:
            raise RuntimeErrorSafe("recovered checkpoint seat drift")
        self._event_seats[event_id] = seat


class InMemoryLedger:
    """Explicit test double; never selected by a live invocation."""

    def __init__(self) -> None:
        self.events: list[dict[str, str]] = []

    def open(self, *, task: Mapping[str, Any], cwd: Path, run_id: str, attempt_id: str) -> str:
        seat = _compiled_execution_seat(task)
        event_id = "evt-" + str(uuid.uuid4())
        self.events.append({"event_id": event_id, "state": "open", "task_id": str(task["id"]), "seat": seat})
        return event_id

    def claim(self, event_id: str) -> None:
        self._lookup(event_id)["state"] = "claimed"

    def close(self, event_id: str, *, outcome: str) -> None:
        event = self._lookup(event_id)
        if event["state"] != "claimed":
            raise RuntimeErrorSafe("test ledger close without claim")
        event.update({"state": "closed", "outcome": outcome})

    def bind_existing(self, event_id: str, *, seat: str) -> None:
        if not event_id.startswith("evt-") or not CHECKPOINT_SEAT_RE.fullmatch(seat):
            raise RuntimeErrorSafe("recovered checkpoint identity is invalid")
        try:
            event = self._lookup(event_id)
        except RuntimeErrorSafe:
            self.events.append(
                {"event_id": event_id, "state": "claimed", "task_id": "recovered", "seat": seat}
            )
            return
        if event.get("seat") != seat:
            raise RuntimeErrorSafe("recovered checkpoint seat drift")
        if event["state"] not in {"claimed", "open"}:
            raise RuntimeErrorSafe("recovered checkpoint is already terminal")
        event["state"] = "claimed"

    def _lookup(self, event_id: str) -> dict[str, str]:
        for event in self.events:
            if event["event_id"] == event_id:
                return event
        raise RuntimeErrorSafe("unknown checkpoint event")


@dataclass
class RuntimeLaunch:
    """Private scheduler handle joining process and governance lifecycles."""

    bridge_launch: BridgeLaunch
    task: Mapping[str, Any]
    checkpoint_event: str

    def journal_evidence(self) -> dict[str, Any]:
        return self.bridge_launch.journal_evidence()


class ManualEventLifecycleAdapter:
    """Manual control plane over the same governed runtime lifecycle.

    It adds observable coordination events only; routing, checkpoint, process,
    review and integration authority remain in ``OrchestrationRuntime``.
    """

    def __init__(self, runtime: "OrchestrationRuntime") -> None:
        self.runtime = runtime
        self.events: list[dict[str, Any]] = []
        self.two_phase_process = runtime.two_phase_process
        self.owns_deadline = runtime.owns_deadline

    def _event(self, name: str, **fields: Any) -> None:
        self.events.append({"event": name, "at": time.time(), **fields})

    def launch_task(self, task: Mapping[str, Any], **kwargs: Any) -> RuntimeLaunch:
        self._event("coordination_started", interval_id=str(kwargs.get("attempt_id")))
        self._event("producer_started", task_id=str(task["id"]))
        return self.runtime.launch_task(task, **kwargs)

    def collect_task(self, launched: RuntimeLaunch) -> Mapping[str, Any]:
        result = self.runtime.collect_task(launched)
        launched_task = getattr(launched, "task", launched)
        task_id = (
            str(launched_task.get("id"))
            if isinstance(launched_task, Mapping)
            else "unknown"
        )
        bridge_launch = getattr(launched, "bridge_launch", None)
        interval_id = str(getattr(bridge_launch, "attempt_id", task_id))
        self._event("producer_completed", task_id=task_id)
        self._event("coordination_completed", interval_id=interval_id)
        return result

    def prepare_resource(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        return self.runtime.prepare_resource(*args, **kwargs)

    def reconcile_resource(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        return self.runtime.reconcile_resource(*args, **kwargs)

    def reconcile_task(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        return self.runtime.reconcile_task(*args, **kwargs)

    def prepare_review(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        self._event("review_started")
        return self.runtime.prepare_review(*args, **kwargs)

    def prepare_dependencies(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        return self.runtime.prepare_dependencies(*args, **kwargs)

    def finalize_run(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        result = self.runtime.finalize_run(*args, **kwargs)
        if result.get("status") == "succeeded":
            self._event("candidate_created")
        return result

    def terminal_cleanup(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        return self.runtime.terminal_cleanup(*args, **kwargs)


class _BenchmarkManualWrapper:
    """Adapts the narrow benchmark B seam to the existing runtime methods."""

    def __init__(self, runtime: ManualEventLifecycleAdapter, worktree_root: Path) -> None:
        self.runtime, self.worktree_root = runtime, worktree_root

    def prepare_resource(self, task: Mapping[str, Any], *, ownership: Mapping[str, Any]) -> Mapping[str, Any]:
        owned = dict(ownership)
        run_id = str(owned["created_by_run_id"])
        owned["resource_id"] = f"benchmark-{task['id']}"
        owned["path"] = str((self.worktree_root / run_id / str(task["id"])).resolve())
        return self.runtime.prepare_resource(task, ownership=owned)

    def launch_task(self, task: Mapping[str, Any], **kwargs: Any) -> Any:
        state = kwargs.pop("state", None)
        fencing_token = kwargs.pop("fencing_token", None)
        if state is not None and task.get("depends_on") and not task.get("reviewer_for"):
            prepared = self.runtime.prepare_dependencies(
                task,
                state,
                fencing_token=fencing_token,
                **kwargs,
            )
            if not isinstance(prepared, Mapping) or prepared.get("status") != "succeeded":
                raise RuntimeErrorSafe("manual dependency context was not confirmed")
        return self.runtime.launch_task(task, **kwargs)

    def collect_task(self, launched: Any) -> Mapping[str, Any]:
        return self.runtime.collect_task(launched)

    def finalize_run(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        return self.runtime.finalize_run(*args, **kwargs)

    def prepare_review(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        return self.runtime.prepare_review(*args, **kwargs)

    def cleanup_terminal(self, state: Mapping[str, Any], *, preserve: bool) -> Mapping[str, Any]:
        if preserve:
            return {"status": "preserved", "reason": "benchmark-default"}
        return self.runtime.terminal_cleanup(self.runtime.runtime.plan, state, run_id=self.runtime.runtime.plan["run_id"], generation=1, fencing_token="benchmark-manual")


class _BenchmarkRuntimeRecorder:
    """Record actual integration/acceptance call boundaries around a runtime."""

    def __init__(self, runtime: Any, event_sink: Any) -> None:
        self.runtime = runtime
        self._event_sink = event_sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)

    def finalize_run(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        self._event_sink({"event": "acceptance_started", "at": time.time()})
        result = dict(self.runtime.finalize_run(*args, **kwargs))
        self._event_sink(
            {
                "event": "acceptance_completed",
                "at": time.time(),
                "accepted": result.get("status") == "succeeded",
            }
        )
        return result


class BenchmarkLiveRuntimeAdapter:
    """Credential-free capability inspection and fail-closed live entrypoint.

    Benchmark arm construction stays in the benchmark harness.  This adapter
    attests the core lifecycle and refuses launch until the caller supplies
    independently observed provider-health evidence and a complete governed
    plan contract.
    """

    def __init__(
        self,
        checkout_root: Path,
        *,
        runtime_factory: Any | None = None,
        evidence_factory: Any | None = None,
        evidence_path: Path | None = None,
    ) -> None:
        self.checkout_root = checkout_root.expanduser().resolve()
        self._config_fingerprint = self._fingerprint()
        self._runtime_factory = runtime_factory
        self._evidence_factory = evidence_factory
        # Do not resolve here: resolving a final-component symlink before the
        # attested-evidence loader sees it would bypass that loader's explicit
        # non-symlink/O_NOFOLLOW trust boundary.
        self._evidence_path = evidence_path.expanduser() if evidence_path else None
        self._inspection: dict[str, Any] | None = None
        self._launched_cells: set[str] = set()

    def _fingerprint(self) -> str:
        digest = hashlib.sha256()
        for relative in ("routing-policy.yaml", "agent-providers.yaml"):
            path = self.checkout_root / relative
            if not path.is_file() or path.is_symlink():
                raise RuntimeErrorSafe(f"benchmark runtime input unavailable: {relative}")
            digest.update(relative.encode())
            digest.update(b"\0")
            # Preflight deliberately does not parse provider configuration.
            # Attested route/config digests are validated by the strict
            # evidence loader; here only local replacement is observable.
            state = path.stat()
            digest.update(f"{state.st_dev}:{state.st_ino}:{state.st_size}:{state.st_mtime_ns}".encode())
            digest.update(b"\0")
        for relative in (
            "scripts/agent_orchestrate.py",
            "scripts/agent_provider_run.py",
            "scripts/skill_audit.py",
            "scripts/skill_router_hook.py",
            "scripts/routing_eval.py",
            "routing-evals/hints.yaml",
            "scripts/orchestration/attestation.py",
            "scripts/orchestration/benchmark.py",
            "scripts/orchestration/runtime.py",
        ):
            path = self.checkout_root / relative
            if not path.is_file() or path.is_symlink():
                raise RuntimeErrorSafe(f"benchmark runtime input unavailable: {relative}")
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _host_identity(self) -> str:
        return hashlib.sha256(os.uname().nodename.encode("utf-8")).hexdigest()

    def _checkout_identity(self) -> str:
        return hashlib.sha256(str(self.checkout_root).encode("utf-8")).hexdigest()

    def inspect_benchmark_live(
        self, protocol: Mapping[str, Any], *, evaluator_root: Path
    ) -> Mapping[str, Any]:
        # Import lazily: benchmark imports the adapter through the CLI.
        from . import benchmark

        evaluator = evaluator_root.expanduser().resolve()
        if not evaluator.is_dir() or evaluator.is_symlink() or stat.S_IMODE(evaluator.stat().st_mode) != 0o700:
            raise RuntimeErrorSafe("benchmark evaluator root must be a private directory")
        families = protocol.get("required_provider_families")
        if not isinstance(families, list) or not families:
            raise RuntimeErrorSafe("benchmark protocol has no provider family set")
        # The legacy/doctor-shaped probe is allowed to attest capabilities but
        # can never establish a launch binding.  A real launch requires the
        # complete frozen protocol and private evaluator manifest below.
        try:
            frozen = benchmark.validate_executable_protocol(protocol)
            verified = benchmark.verify_evaluator_root(frozen, evaluator_root)
        except benchmark.BenchmarkProtocolError:
            frozen = None
            verified = None
        # Missing provider-health evidence is intentional. Login presence or a
        # successful canary never silently authorizes a live benchmark block.
        evidence = {
            str(family): {
                "auth_ok": False,
                "host_healthy": False,
                "provider_incident": False,
                "evidence_status": "unknown-blocked",
            }
            for family in families
        }
        if self._evidence_factory is not None:
            supplied = self._evidence_factory(frozen, evaluator)
            if isinstance(supplied, Mapping):
                evidence = dict(supplied)
        elif self._evidence_path is not None:
            if frozen is None:
                raise RuntimeErrorSafe("attested preflight evidence requires a frozen executable protocol")
            try:
                attested = benchmark.load_attested_evidence(
                    self._evidence_path,
                    frozen,
                    expected_host_identity=self._host_identity(),
                    expected_checkout_identity=self._checkout_identity(),
                    expected_config_fingerprint=self._config_fingerprint,
                )
            except benchmark.BenchmarkProtocolError as exc:
                raise RuntimeErrorSafe("attested preflight evidence was rejected") from exc
            supplied = attested.get("preflight_evidence") if isinstance(attested, Mapping) else None
            if not isinstance(supplied, Mapping):
                raise RuntimeErrorSafe("attested preflight evidence has no provider observations")
            evidence = dict(supplied)
        entrypoint = self.checkout_root / "scripts" / "agent_orchestrate.py"
        observed = {
            "capabilities": {
                "producer_review_reference_propagation": True,
                "post_integration_review": True,
                "read_only_review": True,
                "manual_event_lifecycle": True,
                "cancel_and_replacement": True,
            },
            "preflight_evidence": evidence,
            "config_fingerprint": self._config_fingerprint,
            "orchestrator_entrypoint": str(entrypoint),
            "orchestrator_entrypoint_sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
        }
        if frozen is not None and verified is not None:
            self._inspection = {
                "protocol": frozen,
                "protocol_sha256": benchmark.sha256_value(frozen),
                "evaluator_root": evaluator,
                "manifest_sha256": verified["manifest_sha256"],
                "config_fingerprint": self._config_fingerprint,
            }
        return observed

    def launch_benchmark_arm(
        self,
        contract: Any,
        *,
        cell_root: Path,
        reviewer: Mapping[str, Any],
        block_id: str,
    ) -> Mapping[str, Any]:
        from . import benchmark
        from .benchmark_lifecycle import cleanup_terminal, run_manual_ready_sets

        binding = self._inspection
        if binding is None:
            raise RuntimeErrorSafe("benchmark launch requires a compiler-produced lifecycle plan and prior verified inspection")
        protocol = binding["protocol"]
        evaluator = binding["evaluator_root"]
        if benchmark.sha256_value(protocol) != binding["protocol_sha256"]:
            raise RuntimeErrorSafe("benchmark protocol binding drift")
        if self._fingerprint() != binding["config_fingerprint"]:
            raise RuntimeErrorSafe("benchmark runtime config drift")
        if not isinstance(block_id, str) or not block_id or not isinstance(reviewer, Mapping):
            raise RuntimeErrorSafe("benchmark block/reviewer identity is invalid")
        root = cell_root.expanduser().resolve()
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise RuntimeErrorSafe("benchmark cell root must be a private directory")
        cell_key = hashlib.sha256((binding["protocol_sha256"] + "\0" + contract.task_id + "\0" + contract.arm + "\0" + str(root)).encode()).hexdigest()
        if cell_key in self._launched_cells:
            raise RuntimeErrorSafe("benchmark cell was already launched")
        self._launched_cells.add(cell_key)
        events: list[dict[str, Any]] = []

        def now(event: str, **extra: Any) -> None:
            events.append({"event": event, "at": time.time(), **extra})

        now("task_handoff")
        launch = benchmark.compile_governed_lifecycle(
            protocol, evaluator, contract, reviewer=reviewer, cell_root=root
        )
        plan = dict(launch.plan)
        plan["run_id"] = "benchmark-cell-" + cell_key[:24]
        launch = replace(launch, plan=plan)
        now("graph_ready")
        artifact_root = root / "runtime-artifacts"
        worktree_root = (
            Path(str(plan["repo_root"])).resolve().parent / ".agent-run-worktrees"
        ).resolve()
        artifact_root.mkdir(mode=0o700)
        os.chmod(artifact_root, 0o700)
        runtime = self._make_runtime(plan, artifact_root, worktree_root, launch.evaluator_root)
        recorded_runtime = _BenchmarkRuntimeRecorder(runtime, events.append)
        if contract.arm == "B":
            manual = ManualEventLifecycleAdapter(recorded_runtime)
            wrapper = _BenchmarkManualWrapper(manual, worktree_root)
            outcome = run_manual_ready_sets(
                launch, wrapper, event_sink=events.append
            )
            state = dict(outcome.get("state") or {})
            cleanup_terminal(wrapper, state, preserve=True)
        else:
            journal = EventJournal(root / "scheduler.jsonl", str(plan["run_id"]))
            scheduler = Scheduler(
                plan, recorded_runtime, journal, root / "scheduler.lock"
            )
            state = scheduler.run()
            outcome = {
                "status": (
                    "succeeded"
                    if state.get("status") == "completed"
                    else "partial-failure"
                ),
                "state": state,
            }
            self._events_from_journal(events, journal.read(), plan)
        events.sort(key=lambda row: float(row["at"]))
        accepted = outcome.get("status") == "succeeded"
        now(
            "trial_completed",
            accepted=accepted,
            failure_class=(
                "none" if accepted else self._benchmark_failure_class(state)
            ),
            attributions=self._attributions(state),
        )
        artifacts = self._private_artifacts(root)
        return {
            "launcher_kind": contract.launcher_kind,
            "graph_sha256": contract.graph_sha256,
            "manual_runbook_sha256": contract.manual_runbook_sha256,
            "block_id": block_id,
            "config_fingerprint": self._config_fingerprint,
            "review_binding": dict(reviewer),
            "events": events,
            "artifact_paths": [str(path) for path in artifacts],
        }

    def _make_runtime(self, plan: Mapping[str, Any], artifact_root: Path, worktree_root: Path, evaluator_root: Path) -> Any:
        if self._runtime_factory is not None:
            return self._runtime_factory(plan, artifact_root, worktree_root, evaluator_root)
        ledger = AgentLedgerCLI(slug=str(plan["ledger_slug"]), intent_ref="benchmark-live", repo_root=Path(str(plan["repo_root"])))
        return OrchestrationRuntime(plan, artifact_root=artifact_root, worktree_root=worktree_root, evaluator_root=evaluator_root, ledger=ledger, live=True)

    @staticmethod
    def _attributions(state: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task in state.get("tasks", {}).values():
            result = task.get("result") if isinstance(task, Mapping) else None
            if not isinstance(result, Mapping):
                continue
            duration_ms = result.get("provider_duration_ms")
            if (
                all(
                    isinstance(result.get(key), str) and bool(result.get(key))
                    for key in ("provider_run_id", "model_observed", "session_id")
                )
                and isinstance(duration_ms, (int, float))
                and not isinstance(duration_ms, bool)
                and duration_ms >= 0
            ):
                rows.append(
                    {
                        "run_id": result["provider_run_id"],
                        "model": result["model_observed"],
                        "session_id": result["session_id"],
                        "duration_seconds": float(duration_ms) / 1000.0,
                    }
                )
        return rows

    @staticmethod
    def _benchmark_failure_class(state: Mapping[str, Any]) -> str:
        """Map internal scheduler detail into the benchmark's public allowlist.

        The scheduler state deliberately retains precise failure detail for
        operators.  A benchmark receipt is public/evaluable and only permits
        its fixed failure-class vocabulary, so never forward that raw detail.
        """

        results: list[Mapping[str, Any]] = []
        tasks = state.get("tasks")
        if isinstance(tasks, Mapping):
            for task in tasks.values():
                result = task.get("result") if isinstance(task, Mapping) else None
                if isinstance(result, Mapping):
                    results.append(result)

        def failure_class(result: Mapping[str, Any]) -> str:
            value = result.get("failure_class")
            return value.lower() if isinstance(value, str) else ""

        classes = [failure_class(result) for result in results]
        statuses = {
            str(result.get("status") or "").lower()
            for result in results
        }

        # Fail closed before considering provider evidence.  A provider can
        # be named in a result which is nevertheless unsafe to trust.
        if "failed-unsafe" in statuses or any(
            "unsafe" in value or "unreconciled" in value or "safety" in value
            for value in classes
        ):
            return "failed-unsafe"

        if any(
            value == "task-quality-failure"
            or value.startswith("review-verdict-")
            or value.startswith("acceptance-")
            for value in classes
        ):
            return "task-quality-failure"

        provider_tokens = (
            "timeout",
            "rate-limit",
            "rate_limit",
            "auth",
            "upstream",
            "overload",
            "provider",
        )
        for result, value in zip(results, classes):
            has_provider_identity = any(
                isinstance(result.get(key), str) and bool(result.get(key))
                for key in ("provider", "provider_run_id", "model_observed")
            )
            if has_provider_identity and any(token in value for token in provider_tokens):
                return "provider-environment-failure"

        # A non-success with no explicit safety, quality, or provider signal
        # is a runtime/scheduler concern, not a made-up task verdict.
        return "orchestration-infrastructure-failure"

    @staticmethod
    def _private_artifacts(root: Path) -> list[Path]:
        return [path for path in root.rglob("*.json") if path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600]

    @staticmethod
    def _events_from_journal(
        events: list[dict[str, Any]],
        journal_events: Sequence[Mapping[str, Any]],
        plan: Mapping[str, Any],
    ) -> None:
        """Project only observed scheduler boundaries into benchmark events."""

        reviewers = {
            str(task["id"])
            for task in plan.get("tasks", [])
            if isinstance(task, Mapping) and task.get("reviewer_for")
        }
        producers = {
            str(task["id"])
            for task in plan.get("tasks", [])
            if isinstance(task, Mapping) and not task.get("reviewer_for")
        }
        producer_successes: dict[str, float] = {}
        started_reviewers: set[str] = set()
        terminal = {
            "task_succeeded",
            "task_failed",
            "task_timed_out",
            "task_canceled",
            "task_blocked",
            "task_failed_unsafe",
        }

        for row in journal_events:
            raw_timestamp = row.get("timestamp")
            if not isinstance(raw_timestamp, str):
                raise RuntimeErrorSafe("scheduler journal timestamp is unavailable")
            try:
                observed_at = dt.datetime.fromisoformat(
                    raw_timestamp.replace("Z", "+00:00")
                ).timestamp()
            except ValueError as exc:
                raise RuntimeErrorSafe("scheduler journal timestamp is invalid") from exc
            event_type = str(row.get("event_type") or "")
            task_id = str(row.get("task_id") or "")
            if event_type == "dispatch_claimed":
                if task_id in reviewers:
                    started_reviewers.add(task_id)
                events.append(
                    {
                        "event": (
                            "review_started"
                            if task_id in reviewers
                            else "producer_started"
                        ),
                        "at": observed_at,
                        "task_id": task_id,
                    }
                )
            elif event_type == "task_succeeded" and task_id in producers:
                producer_successes[task_id] = observed_at
            elif (
                event_type in terminal
                and task_id in reviewers
                and task_id in started_reviewers
            ):
                payload = row.get("payload")
                events.append(
                    {
                        "event": "review_completed",
                        "at": observed_at,
                        "task_id": task_id,
                        "status": (
                            payload.get("status")
                            if isinstance(payload, Mapping)
                            else None
                        ),
                    }
                )
        if producers and set(producer_successes) == producers:
            events.append(
                {
                    "event": "candidate_created",
                    "at": max(producer_successes.values()),
                }
            )


def benchmark_live_adapter(
    *,
    checkout_root: Path,
    evidence_path: Path | None = None,
) -> BenchmarkLiveRuntimeAdapter:
    return BenchmarkLiveRuntimeAdapter(checkout_root, evidence_path=evidence_path)


def _safe_input_path(repo_root: Path, ref: str) -> Path:
    if "\\" in ref or "\x00" in ref:
        raise RuntimeErrorSafe("unsafe input_ref")
    path = PurePosixPath(ref)
    if path.is_absolute() or not path.parts or any(piece in {"", ".", ".."} for piece in path.parts):
        raise RuntimeErrorSafe("input_ref must be a normalized repository-relative path")
    candidate = (repo_root / path.as_posix()).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:
        raise RuntimeErrorSafe("input_ref escaped repo root") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise RuntimeErrorSafe("input_ref must be a regular repository file")
    return candidate


class OrchestrationRuntime:
    """The controller adapter used by ``Scheduler`` in fake and live modes."""

    owns_deadline = True

    def __init__(
        self,
        plan: Mapping[str, Any],
        *,
        artifact_root: Path,
        worktree_root: Path,
        evaluator_root: Path | None = None,
        bridge: NativeAgentRunBridge | None = None,
        ledger: CheckpointLedger | None = None,
        live: bool = False,
    ) -> None:
        self.plan = dict(plan)
        self.repo_root = Path(str(plan["repo_root"])).resolve()
        self.artifact_root = artifact_root.expanduser().resolve()
        self.worktree_root = worktree_root.expanduser().resolve()
        self.evaluator_root = evaluator_root.expanduser().resolve() if evaluator_root else None
        self.live = live
        self.bridge = bridge or NativeAgentRunBridge(artifact_root=self.artifact_root)
        self.two_phase_process = bool(
            getattr(self.bridge, "two_phase_process", False)
        )
        self.owns_deadline = self.two_phase_process
        self.ledger = ledger
        self._ownership: dict[str, ResourceOwnership] = {}
        self._candidates: dict[str, CandidateCommit] = {}
        self._review_contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self._dependency_contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self._review_cwds: dict[str, Path] = {}
        self._integration_result: dict[str, Any] | None = None
        self.worktree_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.worktree_root, 0o700)
        self.manager = WorktreeManager(self.repo_root, self.worktree_root)

    def _manifest_path(self, run_id: str, category: str, identity: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", identity):
            raise RuntimeErrorSafe("unsafe controller manifest identity")
        allowed = (self.artifact_root / run_id / "controller-manifests").resolve()
        path = (allowed / category / f"{identity}.json").resolve()
        try:
            path.relative_to(allowed)
        except ValueError as exc:
            raise RuntimeErrorSafe("controller manifest escaped its private root") from exc
        return path

    def _write_controller_manifest(
        self, run_id: str, category: str, identity: str, payload: Mapping[str, Any]
    ) -> Path:
        normalized = dict(payload)
        body = json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        envelope = {
            "version": MANIFEST_VERSION,
            "payload": normalized,
            "payload_sha256": hashlib.sha256(body).hexdigest(),
        }
        path = self._manifest_path(run_id, category, identity)
        write_replaceable_manifest(path, envelope)
        return path

    def _read_controller_manifest(
        self, run_id: str, category: str, identity: str
    ) -> dict[str, Any]:
        path = self._manifest_path(run_id, category, identity)
        try:
            info = path.lstat()
            envelope = json.loads(path.read_bytes())
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeErrorSafe("controller manifest is unavailable or invalid") from exc
        if (
            path.is_symlink()
            or not path.is_file()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not isinstance(envelope, Mapping)
            or envelope.get("version") != MANIFEST_VERSION
            or not isinstance(envelope.get("payload"), Mapping)
        ):
            raise RuntimeErrorSafe("controller manifest type or mode drift")
        payload = dict(envelope["payload"])
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        if envelope.get("payload_sha256") != hashlib.sha256(body).hexdigest():
            raise RuntimeErrorSafe("controller manifest hash drift")
        return payload

    @staticmethod
    def _resource_payload(
        ownership: ResourceOwnership, *, generation: int, expected_head: str
    ) -> dict[str, Any]:
        return {
            "manifest_type": "resource-ownership",
            "run_id": ownership.created_by_run_id,
            "task_id": ownership.task_id,
            "kind": ownership.kind,
            "repo_root": str(ownership.repo_root),
            "path": str(ownership.path),
            "branch": ownership.branch,
            "base_sha": ownership.base_sha,
            "ledger_slug": ownership.ledger_slug,
            "generation": generation,
            "fencing_token": ownership.fencing_token,
            "state": ownership.state,
            "expected_head": expected_head,
        }

    def _resource_from_payload(self, payload: Mapping[str, Any]) -> ResourceOwnership:
        required = {
            "run_id", "kind", "repo_root", "path", "branch", "base_sha",
            "ledger_slug", "fencing_token", "state", "expected_head",
        }
        if payload.get("manifest_type") != "resource-ownership" or any(
            not isinstance(payload.get(key), str) or not payload.get(key)
            for key in required
        ):
            raise RuntimeErrorSafe("resource manifest identity is incomplete")
        task_id = payload.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise RuntimeErrorSafe("resource manifest task identity is invalid")
        return ResourceOwnership(
            created_by_run_id=str(payload["run_id"]),
            fencing_token=str(payload["fencing_token"]),
            repo_root=Path(str(payload["repo_root"])).resolve(),
            path=Path(str(payload["path"])).resolve(),
            branch=str(payload["branch"]),
            base_sha=str(payload["base_sha"]),
            ledger_slug=str(payload["ledger_slug"]),
            kind=str(payload["kind"]),
            task_id=task_id,
            state=str(payload["state"]),
        )

    def _persist_candidate(
        self,
        *,
        run_id: str,
        generation: int,
        ownership: ResourceOwnership,
        candidate: CandidateCommit,
    ) -> None:
        self._write_controller_manifest(
            run_id,
            "candidates",
            candidate.task_id,
            {
                "manifest_type": "writer-candidate",
                "run_id": run_id,
                "task_id": candidate.task_id,
                "generation": generation,
                "fencing_token": ownership.fencing_token,
                "repo_root": str(self.repo_root),
                "worktree_path": str(candidate.worktree_path),
                "branch": ownership.branch,
                "base_sha": candidate.base_sha,
                "ledger_slug": ownership.ledger_slug,
                "commit_sha": candidate.commit_sha,
                "parent_sha": candidate.parent_sha,
                "changed_paths": list(candidate.changed_paths),
                "diff_hash": candidate.diff_hash,
                "shared_interface_hits": list(candidate.shared_interface_hits),
                "acceptance": [asdict(item) for item in candidate.acceptance],
            },
        )

    def _prompt(self, task: Mapping[str, Any]) -> str:
        ref = task.get("input_ref")
        if not isinstance(ref, str) or not ref:
            raise RuntimeErrorSafe("compiled task requires input_ref")
        if ref.startswith("evaluator:"):
            if self.evaluator_root is None:
                raise RuntimeErrorSafe("evaluator input requires explicit private evaluator root")
            identifier = ref.removeprefix("evaluator:")
            if not identifier or "/" in identifier or "\\" in identifier or ".." in identifier:
                raise RuntimeErrorSafe("unsafe evaluator pointer")
            candidate = (self.evaluator_root / f"{identifier}.txt").resolve()
            try:
                candidate.relative_to(self.evaluator_root)
            except ValueError as exc:
                raise RuntimeErrorSafe("evaluator pointer escaped private root") from exc
            if not candidate.is_file() or candidate.is_symlink():
                raise RuntimeErrorSafe("evaluator input is unavailable")
        else:
            candidate = _safe_input_path(self.repo_root, ref)
        data = candidate.read_bytes()
        if len(data) > 1_000_000:
            raise RuntimeErrorSafe("input exceeds bounded prompt size")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeErrorSafe("input_ref must be UTF-8 text") from exc

    def _task_cwd(self, task: Mapping[str, Any]) -> Path:
        review_cwd = self._review_cwds.get(str(task["id"]))
        if review_cwd is not None:
            return review_cwd
        ownership = self._ownership.get(str(task["id"]))
        return ownership.path if ownership else self.repo_root

    @staticmethod
    def _verified_private_artifact(path_raw: Any, digest: Any) -> tuple[Path, str]:
        path = Path(str(path_raw)).expanduser()
        try:
            info = path.lstat()
            data = path.read_bytes()
        except OSError as exc:
            raise RuntimeErrorSafe("review source artifact is unavailable") from exc
        observed = hashlib.sha256(data).hexdigest()
        if (
            path.is_symlink()
            or not path.is_file()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not re.fullmatch(r"[0-9a-f]{64}", str(digest or ""))
            or observed != digest
        ):
            raise RuntimeErrorSafe("review source artifact type, mode, or hash drift")
        return path.resolve(), observed

    def prepare_dependencies(
        self,
        task: Mapping[str, Any],
        state: Mapping[str, Any],
        *,
        run_id: str,
        attempt_id: str,
        generation: int,
        fencing_token: str,
    ) -> Mapping[str, Any]:
        """Create a bounded private handoff for declared non-review dependencies.

        The bundle is deliberately not a transcript or mailbox.  It contains
        only terminal identity/status facts and verified controller-owned
        artifact/candidate pointers.  The journal receives only its path,
        digest and dependency count.
        """

        dependencies = list(task.get("depends_on") or [])
        if not dependencies or task.get("reviewer_for"):
            return {"status": "not-applicable", "dependency_count": 0}
        records: list[dict[str, Any]] = []
        for dependency_id in dependencies:
            dependency_state = state.get("tasks", {}).get(dependency_id)
            if (
                not isinstance(dependency_state, Mapping)
                or dependency_state.get("status") != "succeeded"
                or not isinstance(dependency_state.get("result"), Mapping)
            ):
                raise RuntimeErrorSafe("dependency result is missing or unsuccessful")
            result = dependency_state["result"]
            record: dict[str, Any] = {
                "task_id": str(dependency_id),
                "status": "succeeded",
                "attempt_id": str(dependency_state.get("current_attempt_id") or ""),
            }
            for source, target in (
                ("provider_run_id", "provider_run_id"),
                ("provider", "provider_id"),
                ("model_observed", "model_observed"),
                ("model_family", "model_family"),
                ("session_id", "session_id"),
                ("session_status", "session_status"),
                ("candidate_commit", "candidate_commit"),
            ):
                value = result.get(source)
                if isinstance(value, str) and value:
                    record[target] = value
            artifact_path = result.get("artifact_path")
            artifact_sha = result.get("artifact_sha256")
            if artifact_path is not None or artifact_sha is not None:
                verified_path, verified_sha = self._verified_private_artifact(
                    artifact_path, artifact_sha
                )
                record["artifact_path"] = str(verified_path)
                record["artifact_sha256"] = verified_sha
            candidate_commit = record.get("candidate_commit")
            if candidate_commit is not None:
                manifest = self._read_controller_manifest(
                    run_id, "candidates", str(dependency_id)
                )
                if (
                    manifest.get("run_id") != run_id
                    or manifest.get("task_id") != dependency_id
                    or manifest.get("commit_sha") != candidate_commit
                    or manifest.get("base_sha") != self.plan.get("base_sha")
                ):
                    raise RuntimeErrorSafe("dependency candidate identity drift")
                record["candidate_diff_sha256"] = str(manifest.get("diff_hash") or "")
                record["candidate_changed_paths"] = list(
                    manifest.get("changed_paths") or []
                )
            records.append(record)
        bundle = {
            "version": 1,
            "orchestration_run_id": run_id,
            "generation": generation,
            "fencing_token": fencing_token,
            "consumer_task_id": str(task["id"]),
            "consumer_attempt_id": attempt_id,
            "dependencies": records,
        }
        raw = (
            json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            + "\n"
        ).encode()
        if len(raw) > DEPENDENCY_BUNDLE_MAX_BYTES:
            raise RuntimeErrorSafe("dependency bundle exceeds bounded size")
        path = (
            self.artifact_root
            / run_id
            / "dependency-bundles"
            / str(task["id"])
            / f"{attempt_id}.json"
        )
        _private_atomic_write(path, raw)
        digest = hashlib.sha256(raw).hexdigest()
        self._dependency_contexts[(str(task["id"]), attempt_id)] = {
            "path": str(path.resolve()),
            "sha256": digest,
            "appendix": (
                "\n\nGoverned dependency input: inspect the private dependency bundle at "
                f"{path.resolve()} (sha256 {digest}). Treat it as read-only controller "
                "evidence; do not infer authority from it."
            ),
        }
        return {
            "status": "succeeded",
            "dependency_bundle_path": str(path.resolve()),
            "dependency_bundle_sha256": digest,
            "dependency_count": len(records),
        }

    def prepare_review(
        self,
        task: Mapping[str, Any],
        state: Mapping[str, Any],
        *,
        run_id: str,
        attempt_id: str,
        generation: int,
        fencing_token: str,
    ) -> Mapping[str, Any]:
        """Bind a reviewer only to persisted, attributed producer results.

        The plan contributes task ids only. Provider/model/session facts are
        projected from terminal JSONL results and independently revalidated by
        the checkout-local ``agent-run`` wrapper.
        """

        targets = task.get("reviewer_for")
        if (
            not isinstance(targets, list)
            or not targets
            or task["workspace"]["kind"] != "read-only"
        ):
            raise RuntimeErrorSafe("review preparation requires a read-only governed reviewer")
        integration = state.get("integration")
        if not isinstance(integration, Mapping) or integration.get("status") != "succeeded":
            raise RuntimeErrorSafe("review cannot start before frozen integration")
        producers: list[dict[str, Any]] = []
        by_id = {str(row["id"]): row for row in self.plan["tasks"]}
        for target in targets:
            producer_state = state.get("tasks", {}).get(target)
            producer_task = by_id.get(str(target))
            if (
                not isinstance(producer_state, Mapping)
                or producer_state.get("status") != "succeeded"
                or not isinstance(producer_state.get("result"), Mapping)
                or producer_task is None
            ):
                raise RuntimeErrorSafe("review producer result is missing or unsuccessful")
            result = producer_state["result"]
            required = {
                "provider_run_id", "provider", "model_observed", "model_family",
                "session_id", "session_status", "artifact_path", "artifact_sha256",
            }
            if any(result.get(key) in {None, "", "unknown", "undisclosed"} for key in required):
                raise RuntimeErrorSafe("review producer attribution is incomplete")
            artifact_path, artifact_sha = self._verified_private_artifact(
                result["artifact_path"], result["artifact_sha256"]
            )
            producers.append(
                {
                    "task_id": str(target),
                    "run_id": str(result["provider_run_id"]),
                    "provider_id": str(result["provider"]),
                    "model_observed": str(result["model_observed"]),
                    "model_family": str(result["model_family"]),
                    "session_id": str(result["session_id"]),
                    "session_status": str(result["session_status"]),
                    "mode": str(producer_task["permission_projection"]["execution_mode"]),
                    "artifact_path": str(artifact_path),
                    "artifact_sha256": artifact_sha,
                }
            )
        run_ids = [row["run_id"] for row in producers]
        sessions = [row["session_id"] for row in producers]
        identities = [
            (row["model_observed"], row["session_id"], row["model_family"])
            for row in producers
        ]
        if (
            len(run_ids) != len(set(run_ids))
            or len(sessions) != len(set(sessions))
            or len(identities) != len(set(identities))
        ):
            raise RuntimeErrorSafe("review producer identities are not unique")

        writers = [row for row in self.plan["tasks"] if row["workspace"]["kind"] == "isolated-writer"]
        if writers:
            candidate_path, candidate_sha = self._verified_private_artifact(
                integration.get("artifact_path"), integration.get("artifact_sha256")
            )
            candidate_kind = "controller-integration"
            integration_head = str(integration.get("integration_head") or "")
            integration_path = integration.get("integration_path")
            if not integration_head or not isinstance(integration_path, str):
                # The public journal omits the worktree path. Recover it only
                # from the controller-owned private integration artifact.
                try:
                    record = json.loads(candidate_path.read_bytes())
                except (UnicodeDecodeError, ValueError) as exc:
                    raise RuntimeErrorSafe("integration artifact is invalid") from exc
                if not isinstance(record, Mapping):
                    raise RuntimeErrorSafe("integration artifact is invalid")
                integration_head = str(record.get("integration_head") or "")
                integration_path = record.get("integration_path")
            review_cwd = Path(str(integration_path)).resolve()
            if not integration_head or not review_cwd.is_dir():
                raise RuntimeErrorSafe("frozen integration identity is incomplete")
        else:
            integration_head = str(integration.get("integration_head") or self.plan.get("base_sha") or "read-only")
            candidate_record = {
                "version": 1,
                "run_id": run_id,
                "integration_head": integration_head,
                "producer_artifacts": [
                    {"task_id": row["task_id"], "artifact_path": row["artifact_path"], "artifact_sha256": row["artifact_sha256"]}
                    for row in producers
                ],
            }
            candidate_path = self.artifact_root / run_id / "review-candidates" / f"{task['id']}-{attempt_id}.json"
            _private_atomic_write(
                candidate_path,
                (json.dumps(candidate_record, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            )
            candidate_sha = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            candidate_kind = "read-only-artifact-set"
            review_cwd = self.repo_root

        repo_slug = str(self.plan.get("ledger_slug") or self.repo_root.name)
        bundle = {
            "version": 1,
            "orchestration_run_id": run_id,
            "generation": generation,
            "fencing_token": fencing_token,
            "reviewer_task_id": str(task["id"]),
            "reviewer_attempt_id": attempt_id,
            "repo": repo_slug,
            "producers": producers,
            "candidate": {
                "kind": candidate_kind,
                "artifact_path": str(candidate_path),
                "artifact_sha256": candidate_sha,
                "integration_head": integration_head,
            },
        }
        bundle_path = self.artifact_root / run_id / "review-bundles" / str(task["id"]) / f"{attempt_id}.json"
        bundle_bytes = (json.dumps(bundle, sort_keys=True, separators=(",", ":")) + "\n").encode()
        _private_atomic_write(bundle_path, bundle_bytes)
        bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
        context = {
            "producer_review_bundle": str(bundle_path),
            "producer_review_bundle_sha256": bundle_sha,
            "orchestration_run_id": run_id,
            "orchestration_generation": generation,
            "orchestration_fencing_token": fencing_token,
            "orchestration_reviewer_task_id": str(task["id"]),
            "orchestration_reviewer_attempt_id": attempt_id,
            "review_appendix": (
                "\n\nGoverned review input: inspect the private review bundle at "
                f"{bundle_path} (sha256 {bundle_sha}). Review the frozen candidate only."
            ),
        }
        self._review_contexts[(str(task["id"]), attempt_id)] = context
        self._review_cwds[str(task["id"])] = review_cwd
        return {
            "status": "succeeded",
            "review_bundle_path": str(bundle_path),
            "review_bundle_sha256": bundle_sha,
            "producer_count": len(producers),
            "candidate_kind": candidate_kind,
            "integration_head": integration_head,
        }

    def prepare_resource(self, task: Mapping[str, Any], *, ownership: Mapping[str, Any]) -> Mapping[str, Any]:
        if task["workspace"]["kind"] != "isolated-writer":
            return {"status": "created"}
        path = Path(str(ownership["path"])).resolve()
        if path.parent != self.worktree_root / str(ownership["created_by_run_id"]):
            # Scheduler's path is its own predictable root.  Make that parent once,
            # then enforce it rather than accepting an arbitrary mkdir target.
            expected_parent = self.worktree_root / str(ownership["created_by_run_id"])
            if path.parent != expected_parent:
                raise RuntimeErrorSafe("scheduler worktree path is outside runtime root")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        resource = ResourceOwnership(
            created_by_run_id=str(ownership["created_by_run_id"]), fencing_token=str(ownership["fencing_token"]),
            repo_root=self.repo_root, path=path, branch=str(ownership["branch"]), base_sha=str(ownership["base_sha"]),
            ledger_slug=str(ownership["ledger_slug"]), kind="writer", task_id=str(task["id"]),
        )
        confirmed = self.manager.create(resource, current_run_id=resource.created_by_run_id, current_fencing_token=resource.fencing_token)
        self._ownership[str(task["id"])] = confirmed
        self._write_controller_manifest(
            resource.created_by_run_id,
            "resources",
            str(task["id"]),
            self._resource_payload(
                confirmed,
                generation=int(ownership["generation"]),
                expected_head=resource.base_sha,
            ),
        )
        return {"status": "created", "path": str(path), "branch": resource.branch, "base_sha": resource.base_sha, "ledger_slug": resource.ledger_slug}

    def reconcile_resource(
        self,
        task: Mapping[str, Any],
        *,
        ownership: Mapping[str, Any],
        current_ownership: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Adopt only an exactly matching, controller-manifested worktree."""

        run_id = str(ownership.get("created_by_run_id") or "")
        task_id = str(task["id"])
        try:
            payload = self._read_controller_manifest(run_id, "resources", task_id)
            previous = self._resource_from_payload(payload)
            current = dict(current_ownership or ownership)
            stable = {
                "created_by_run_id": previous.created_by_run_id,
                "repo_root": str(previous.repo_root),
                "path": str(previous.path),
                "branch": previous.branch,
                "base_sha": previous.base_sha,
                "ledger_slug": previous.ledger_slug,
                "task_id": previous.task_id,
            }
            if any(str(current.get(key)) != str(value) for key, value in stable.items()):
                raise RuntimeErrorSafe("resource adoption identity drift")
            if previous.kind != "writer" or previous.state != "created":
                raise RuntimeErrorSafe("resource adoption kind or state drift")
            expected_head = str(payload["expected_head"])
            expected_diff = None
            if expected_head != previous.base_sha:
                candidate = self._read_controller_manifest(run_id, "candidates", task_id)
                if candidate.get("commit_sha") != expected_head:
                    raise RuntimeErrorSafe("resource candidate head drift")
                expected_diff = str(candidate.get("diff_hash") or "")
            self.manager.reconcile(
                previous,
                current_run_id=run_id,
                current_fencing_token=previous.fencing_token,
                expected_head=expected_head,
                expected_ledger_slug=previous.ledger_slug,
                expected_diff_hash=expected_diff,
            )
            adopted = replace(previous, fencing_token=str(current["fencing_token"]))
            self._ownership[task_id] = adopted
            self._write_controller_manifest(
                run_id,
                "resources",
                task_id,
                self._resource_payload(
                    adopted,
                    generation=int(current["generation"]),
                    expected_head=expected_head,
                ),
            )
            return {
                "status": "created",
                "path": str(adopted.path),
                "branch": adopted.branch,
                "base_sha": adopted.base_sha,
                "ledger_slug": adopted.ledger_slug,
                "replayed": False,
            }
        except (RuntimeErrorSafe, WorktreeError, OSError, KeyError, TypeError, ValueError):
            return {
                "status": "failed-unsafe",
                "failure_class": "resource-resume-manifest-unreconciled",
                "replayed": False,
            }

    def prepare_retry(
        self,
        task: Mapping[str, Any],
        *,
        run_id: str,
        attempt_id: str,
        generation: int,
        fencing_token: str,
    ) -> Mapping[str, Any]:
        """Permit writer retry only when its exact owned worktree is pristine."""

        del attempt_id, generation
        if task["workspace"]["kind"] != "isolated-writer":
            return {"status": "succeeded"}
        ownership = self._ownership.get(str(task["id"]))
        if ownership is None:
            return {
                "status": "failed-unsafe",
                "failure_class": "writer-retry-safety-unverifiable",
                "resource_preserved": True,
            }
        try:
            self.manager.reconcile(
                ownership,
                current_run_id=run_id,
                current_fencing_token=fencing_token,
                expected_head=ownership.base_sha,
                expected_ledger_slug=ownership.ledger_slug,
                expected_diff_hash=None,
            )
        except (WorktreeError, OSError, ValueError, TypeError):
            return {
                "status": "failed-unsafe",
                "failure_class": "writer-retry-dirty-worktree",
                "resource_preserved": True,
            }
        return {"status": "succeeded"}

    def reconcile_task(
        self,
        task: Mapping[str, Any],
        *,
        run_id: str,
        attempt_id: str,
        generation: int,
        prior_state: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Drain a uniquely identified read-only wrapper; never replay it."""

        if task["workspace"]["kind"] == "isolated-writer":
            return {
                "status": "failed-unsafe",
                "failure_class": "writer-resume-manifest-unavailable",
                "replayed": False,
            }
        reconciler = getattr(self.bridge, "reconcile_task", None)
        manifest_reader = getattr(self.bridge, "attempt_manifest", None)
        if reconciler is None or manifest_reader is None:
            return {
                "status": "failed-unsafe",
                "failure_class": "live-resume-manifest-unavailable",
                "replayed": False,
            }
        if self.ledger is None:
            return {
                "status": "failed-unsafe",
                "failure_class": "checkpoint-ledger-unavailable-on-resume",
                "replayed": False,
            }
        try:
            manifest = manifest_reader(
                run_id=run_id, task_id=str(task["id"]), attempt_id=attempt_id
            )
            expected_seat = _compiled_execution_seat(task)
            checkpoint = manifest.get("checkpoint_event")
            if (
                manifest.get("run_id") != run_id
                or manifest.get("task_id") != str(task["id"])
                or manifest.get("attempt_id") != attempt_id
                or not isinstance(checkpoint, str)
                or not checkpoint.startswith("evt-")
                or manifest.get("compiled_seat") != expected_seat
            ):
                raise RuntimeErrorSafe("checkpoint identity drift in attempt manifest")
            prior_checkpoint = prior_state.get("checkpoint_event")
            prior_seat = prior_state.get("compiled_seat")
            if prior_checkpoint is not None and prior_checkpoint != checkpoint:
                raise RuntimeErrorSafe("journal checkpoint identity drift")
            if prior_seat is not None and prior_seat != expected_seat:
                raise RuntimeErrorSafe("journal checkpoint seat drift")
            binder = getattr(self.ledger, "bind_existing", None)
            if binder is None:
                raise RuntimeErrorSafe("checkpoint ledger cannot bind recovered events")
            binder(checkpoint, seat=expected_seat)
            result = dict(
                reconciler(
                    task,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    generation=generation,
                    prior_state=prior_state,
                )
            )
            if result.get("status") not in {
                "succeeded", "failed", "timed-out", "failed-unsafe"
            }:
                raise RuntimeErrorSafe("recovered process has no terminal status")
            self.ledger.close(
                checkpoint,
                outcome=(
                    "task succeeded"
                    if result.get("status") == "succeeded"
                    else "task failed"
                ),
            )
            return result
        except (BridgeError, RuntimeErrorSafe, OSError, ValueError, TypeError):
            return {
                "status": "failed-unsafe",
                "failure_class": "checkpoint-or-process-resume-unreconciled",
                "replayed": False,
            }

    def _project_task(
        self, task: Mapping[str, Any], *, cwd: Path, checkpoint: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        projected = {
            "id": task["id"],
            "task_id": task["id"],
            "task_shape": task["task_shape"],
            "deadline_seconds": task["deadline_seconds"],
            "cwd": str(cwd),
            "mode": task["permission_projection"]["execution_mode"],
            "checkpoint_event": checkpoint,
            "compiled_seat": _compiled_execution_seat(task),
            "prompt": self._prompt(task),
        }
        if task["binding"].get("managed_skills") == "disabled":
            projected["disable_managed_skills"] = True
        dependencies = list(task.get("depends_on") or [])
        if dependencies and not task.get("reviewer_for"):
            context = self._dependency_contexts.get((str(task["id"]), attempt_id))
            if context is None:
                raise RuntimeErrorSafe("dependency context was not prepared by the controller")
            path, digest = self._verified_private_artifact(
                context["path"], context["sha256"]
            )
            if path.stat().st_size > DEPENDENCY_BUNDLE_MAX_BYTES:
                raise RuntimeErrorSafe("dependency bundle exceeds bounded size at dispatch")
            projected["dependency_bundle_path"] = str(path)
            projected["dependency_bundle_sha256"] = digest
            projected["prompt"] += str(context["appendix"])
        if task.get("reviewer_for"):
            projected["reviewer_for"] = list(task["reviewer_for"])
            context = self._review_contexts.get((str(task["id"]), attempt_id))
            if context is None:
                raise RuntimeErrorSafe("review context was not prepared by the controller")
            projected.update({key: value for key, value in context.items() if key != "review_appendix"})
            projected["prompt"] += (
                str(context["review_appendix"])
                + "\n\nVerdict contract: after your findings, your final non-empty line "
                "MUST be exactly AGENT_RUN_REVIEW_VERDICT: PASS or "
                "AGENT_RUN_REVIEW_VERDICT: FAIL."
            )
        return projected

    def launch_task(
        self,
        task: Mapping[str, Any],
        *,
        run_id: str,
        attempt_id: str,
        generation: int,
        deadline_at: str | None = None,
    ) -> RuntimeLaunch:
        if not self.live:
            raise RuntimeErrorSafe("live provider dispatch is disabled")
        if self.ledger is None:
            raise RuntimeErrorSafe("checkpoint ledger is unavailable")
        launcher = getattr(self.bridge, "launch_task", None)
        if launcher is None:
            raise RuntimeErrorSafe("bridge has no recoverable launch seam")
        cwd = self._task_cwd(task)
        checkpoint = self.ledger.open(
            task=task, cwd=cwd, run_id=run_id, attempt_id=attempt_id
        )
        try:
            self.ledger.claim(checkpoint)
            projected = self._project_task(
                task, cwd=cwd, checkpoint=checkpoint, attempt_id=attempt_id
            )
            launched = launcher(
                projected,
                run_id=run_id,
                attempt_id=attempt_id,
                generation=generation,
                deadline_at=deadline_at,
            )
            if not isinstance(launched, BridgeLaunch):
                raise RuntimeErrorSafe("bridge launch returned an invalid handle")
            return RuntimeLaunch(
                bridge_launch=launched,
                task=dict(task),
                checkpoint_event=checkpoint,
            )
        except Exception:
            try:
                self.ledger.close(checkpoint, outcome="task launch failed")
            except Exception:
                pass
            raise

    def collect_task(self, launched: RuntimeLaunch) -> Mapping[str, Any]:
        checkpoint = launched.checkpoint_event
        task = launched.task
        try:
            result = dict(self.bridge.collect_task(launched.bridge_launch))
            if result.get("status") == "succeeded" and task["workspace"]["kind"] == "isolated-writer":
                resource = self._ownership[str(task["id"])]
                inspected = self.manager.inspect_writer(
                    resource,
                    current_run_id=launched.bridge_launch.run_id,
                    current_fencing_token=resource.fencing_token,
                    own=task["workspace"]["own"],
                    do_not_touch=task["workspace"]["do_not_touch"],
                    shared_interface_paths=task["workspace"]["shared_interface_paths"],
                    acceptance_commands=task["acceptance"],
                )
                candidate = self.manager.commit_candidate(
                    resource,
                    inspected,
                    current_run_id=launched.bridge_launch.run_id,
                    current_fencing_token=resource.fencing_token,
                    plan_id=str(self.plan.get("run_id") or launched.bridge_launch.run_id),
                )
                self._candidates[str(task["id"])] = candidate
                self._persist_candidate(
                    run_id=launched.bridge_launch.run_id,
                    generation=launched.bridge_launch.generation,
                    ownership=resource,
                    candidate=candidate,
                )
                self._write_controller_manifest(
                    launched.bridge_launch.run_id,
                    "resources",
                    str(task["id"]),
                    self._resource_payload(
                        resource,
                        generation=launched.bridge_launch.generation,
                        expected_head=candidate.commit_sha,
                    ),
                )
                result["candidate_commit"] = candidate.commit_sha
            self.ledger.close(
                checkpoint,
                outcome="task succeeded" if result.get("status") == "succeeded" else "task failed",
            )
            return result
        except (BridgeError, WorktreeError, RuntimeErrorSafe, OSError) as exc:
            try:
                self.ledger.close(checkpoint, outcome="task failed")
            except Exception:
                pass
            return {
                "status": "failed-unsafe",
                "failure_class": "runtime-safety-error",
                "detail": type(exc).__name__,
            }

    def run_task(self, task: Mapping[str, Any], *, run_id: str, attempt_id: str, generation: int) -> Mapping[str, Any]:
        if not self.live:
            return {"status": "failed-unsafe", "failure_class": "live-provider-disabled"}
        if hasattr(self.bridge, "launch_task"):
            try:
                launched = self.launch_task(
                    task,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    generation=generation,
                )
                return self.collect_task(launched)
            except (BridgeError, WorktreeError, RuntimeErrorSafe, OSError) as exc:
                return {
                    "status": "failed-unsafe",
                    "failure_class": "runtime-safety-error",
                    "detail": type(exc).__name__,
                }
        cwd = self._task_cwd(task)
        checkpoint = self.ledger.open(task=task, cwd=cwd, run_id=run_id, attempt_id=attempt_id) if self.ledger else None
        if not checkpoint:
            return {"status": "failed-unsafe", "failure_class": "checkpoint-ledger-unavailable"}
        try:
            self.ledger.claim(checkpoint)
            projected = self._project_task(
                task,
                cwd=cwd,
                checkpoint=checkpoint,
                attempt_id=attempt_id,
            )
            result = dict(self.bridge.run_task(projected, run_id=run_id, attempt_id=attempt_id, generation=generation))
            if result.get("status") == "succeeded" and task.get("reviewer_for"):
                try:
                    artifact, _digest = self._verified_private_artifact(
                        result.get("artifact_path"), result.get("artifact_sha256")
                    )
                    if artifact.stat().st_size > DEPENDENCY_BUNDLE_MAX_BYTES:
                        raise RuntimeErrorSafe("review result exceeds bounded size")
                    verdict_failure = review_verdict_failure(
                        artifact.read_text(encoding="utf-8")
                    )
                except (RuntimeErrorSafe, OSError, UnicodeError):
                    result.update(
                        status="failed-unsafe",
                        failure_class="review-verdict-unverifiable",
                    )
                else:
                    if verdict_failure is not None:
                        result.update(
                            status="failed", failure_class=verdict_failure
                        )
            if result.get("status") == "succeeded" and task["workspace"]["kind"] == "isolated-writer":
                resource = self._ownership[str(task["id"])]
                inspected = self.manager.inspect_writer(resource, current_run_id=run_id, current_fencing_token=resource.fencing_token, own=task["workspace"]["own"], do_not_touch=task["workspace"]["do_not_touch"], shared_interface_paths=task["workspace"]["shared_interface_paths"], acceptance_commands=task["acceptance"])
                candidate = self.manager.commit_candidate(resource, inspected, current_run_id=run_id, current_fencing_token=resource.fencing_token, plan_id=str(self.plan.get("run_id") or run_id))
                self._candidates[str(task["id"])] = candidate
                self._persist_candidate(
                    run_id=run_id,
                    generation=generation,
                    ownership=resource,
                    candidate=candidate,
                )
                self._write_controller_manifest(
                    run_id,
                    "resources",
                    str(task["id"]),
                    self._resource_payload(
                        resource,
                        generation=generation,
                        expected_head=candidate.commit_sha,
                    ),
                )
                result["candidate_commit"] = candidate.commit_sha
            self.ledger.close(checkpoint, outcome="task succeeded" if result.get("status") == "succeeded" else "task failed")
            return result
        except (BridgeError, WorktreeError, RuntimeErrorSafe, OSError) as exc:
            try:
                self.ledger.close(checkpoint, outcome="task failed")
            except Exception:
                pass
            return {"status": "failed-unsafe", "failure_class": "runtime-safety-error", "detail": type(exc).__name__}

    def _recover_candidates(
        self,
        writers: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        generation: int,
        fencing_token: str,
    ) -> None:
        for task in writers:
            task_id = str(task["id"])
            if task_id in self._candidates:
                continue
            resource_payload = self._read_controller_manifest(
                run_id, "resources", task_id
            )
            candidate_payload = self._read_controller_manifest(
                run_id, "candidates", task_id
            )
            previous = self._resource_from_payload(resource_payload)
            stable_checks = {
                "run_id": run_id,
                "task_id": task_id,
                "repo_root": str(self.repo_root),
                "worktree_path": str(previous.path),
                "branch": previous.branch,
                "base_sha": str(self.plan["base_sha"]),
                "ledger_slug": str(self.plan["ledger_slug"]),
            }
            if any(candidate_payload.get(key) != value for key, value in stable_checks.items()):
                raise RuntimeErrorSafe("candidate manifest identity drift")
            if (
                previous.kind != "writer"
                or previous.task_id != task_id
                or previous.state != "created"
                or resource_payload.get("expected_head")
                != candidate_payload.get("commit_sha")
                or candidate_payload.get("parent_sha") != previous.base_sha
            ):
                raise RuntimeErrorSafe("candidate/resource manifest chain drift")
            changed_paths = tuple(candidate_payload.get("changed_paths") or ())
            if not changed_paths or any(
                not isinstance(path, str) or path not in task["workspace"]["own"]
                for path in changed_paths
            ):
                # Exact literal ownership is sufficient here; glob ownership
                # was already frozen by the original inspect pass, so recover
                # through the Git diff plus the persisted hash below.
                from .worktree import validate_changed_scope

                validate_changed_scope(
                    changed_paths,
                    own=task["workspace"]["own"],
                    do_not_touch=task["workspace"]["do_not_touch"],
                    shared_interface_paths=task["workspace"]["shared_interface_paths"],
                )
            acceptance_raw = candidate_payload.get("acceptance")
            if not isinstance(acceptance_raw, list) or len(acceptance_raw) != len(
                task["acceptance"]
            ):
                raise RuntimeErrorSafe("candidate acceptance manifest drift")
            acceptance: list[AcceptanceResult] = []
            for index, (raw, command) in enumerate(
                zip(acceptance_raw, task["acceptance"], strict=True)
            ):
                expected_command_hash = hashlib.sha256(
                    b"\0".join(str(part).encode() for part in command)
                ).hexdigest()
                if (
                    not isinstance(raw, Mapping)
                    or raw.get("command_index") != index
                    or raw.get("command_sha256") != expected_command_hash
                    or raw.get("exit_code") != 0
                ):
                    raise RuntimeErrorSafe("candidate acceptance identity drift")
                acceptance.append(AcceptanceResult(**dict(raw)))
            commit_sha = str(candidate_payload.get("commit_sha") or "")
            diff_hash = str(candidate_payload.get("diff_hash") or "")
            observation = self.manager.reconcile(
                previous,
                current_run_id=run_id,
                current_fencing_token=previous.fencing_token,
                expected_head=commit_sha,
                expected_ledger_slug=previous.ledger_slug,
                expected_diff_hash=diff_hash,
            )
            if observation.head_sha != commit_sha:
                raise RuntimeErrorSafe("candidate Git head drift")
            adopted = replace(previous, fencing_token=fencing_token)
            candidate = CandidateCommit(
                task_id=task_id,
                commit_sha=commit_sha,
                parent_sha=str(candidate_payload["parent_sha"]),
                base_sha=str(candidate_payload["base_sha"]),
                worktree_path=previous.path,
                changed_paths=changed_paths,
                diff_hash=diff_hash,
                shared_interface_hits=tuple(
                    candidate_payload.get("shared_interface_hits") or ()
                ),
                acceptance=tuple(acceptance),
            )
            self._ownership[task_id] = adopted
            self._candidates[task_id] = candidate
            self._write_controller_manifest(
                run_id,
                "resources",
                task_id,
                self._resource_payload(
                    adopted, generation=generation, expected_head=commit_sha
                ),
            )
            self._persist_candidate(
                run_id=run_id,
                generation=generation,
                ownership=adopted,
                candidate=candidate,
            )

    def _recover_integration_resource(
        self, *, run_id: str, generation: int, fencing_token: str
    ) -> ResourceOwnership:
        payload = self._read_controller_manifest(run_id, "resources", "integration")
        integration_payload = self._read_controller_manifest(
            run_id, "integrations", "integration"
        )
        previous = self._resource_from_payload(payload)
        if (
            previous.kind != "integration"
            or previous.task_id is not None
            or previous.created_by_run_id != run_id
            or previous.repo_root != self.repo_root
            or previous.base_sha != self.plan.get("base_sha")
            or previous.ledger_slug != self.plan.get("ledger_slug")
        ):
            raise RuntimeErrorSafe("integration resource manifest identity drift")
        expected_head = str(payload["expected_head"])
        if (
            integration_payload.get("manifest_type") != "integration-result"
            or integration_payload.get("run_id") != run_id
            or integration_payload.get("repo_root") != str(self.repo_root)
            or integration_payload.get("path") != str(previous.path)
            or integration_payload.get("branch") != previous.branch
            or integration_payload.get("base_sha") != previous.base_sha
            or integration_payload.get("ledger_slug") != previous.ledger_slug
            or integration_payload.get("integration_head") != expected_head
            or not isinstance(integration_payload.get("changed_paths"), list)
        ):
            raise RuntimeErrorSafe("integration result manifest identity drift")
        changed_paths = tuple(integration_payload["changed_paths"])
        expected_diff = str(integration_payload.get("diff_hash") or "")
        if self.manager.commit_diff_hash(
            expected_head, previous.base_sha, changed_paths
        ) != expected_diff:
            raise RuntimeErrorSafe("integration commit diff manifest drift")
        self.manager.reconcile(
            previous,
            current_run_id=run_id,
            current_fencing_token=previous.fencing_token,
            expected_head=expected_head,
            expected_ledger_slug=previous.ledger_slug,
            expected_diff_hash=expected_diff,
            expected_integration_head=expected_head,
        )
        adopted = replace(previous, fencing_token=fencing_token)
        self._ownership["__integration__"] = adopted
        self._write_controller_manifest(
            run_id,
            "resources",
            "integration",
            self._resource_payload(
                adopted, generation=generation, expected_head=expected_head
            ),
        )
        return adopted

    def finalize_run(self, plan: Mapping[str, Any], state: Mapping[str, Any], *, run_id: str, generation: int, fencing_token: str) -> Mapping[str, Any]:
        if self._integration_result is not None:
            return dict(self._integration_result)
        writers = [task for task in plan["tasks"] if task["workspace"]["kind"] == "isolated-writer"]
        if not writers:
            record = {
                "version": 1,
                "run_id": run_id,
                "integration_head": str(plan.get("base_sha") or "read-only"),
                "integration_path": str(self.repo_root),
                "applied_task_ids": [],
                "changed_paths": [],
                "acceptance": [],
                "acceptance_argv": [
                    list(command) for command in plan["integrated_acceptance"]
                ],
                "producer_acceptance_argv": {
                    str(task["id"]): [list(command) for command in task["acceptance"]]
                    for task in plan["tasks"]
                    if not task.get("reviewer_for")
                },
            }
            artifact = self.artifact_root / run_id / "integration.json"
            _private_atomic_write(
                artifact, (json.dumps(record, sort_keys=True) + "\n").encode()
            )
            self._integration_result = {
                "status": "succeeded",
                "integration_head": record["integration_head"],
                "integration_path": str(self.repo_root),
                "artifact_path": str(artifact),
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
            return dict(self._integration_result)
        if set(self._candidates) != {task["id"] for task in writers}:
            try:
                self._recover_candidates(
                    writers,
                    run_id=run_id,
                    generation=generation,
                    fencing_token=fencing_token,
                )
            except (RuntimeErrorSafe, WorktreeError, OSError, KeyError, TypeError, ValueError):
                return {
                    "status": "failed-unsafe",
                    "failure_class": "writer-candidate-resume-unreconciled",
                }
        path = (self.worktree_root / run_id / "integration").resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        resource = ResourceOwnership(created_by_run_id=run_id, fencing_token=fencing_token, repo_root=self.repo_root, path=path, branch=f"agent-run/{run_id}/integration", base_sha=str(plan["base_sha"]), ledger_slug=str(plan["ledger_slug"]), kind="integration")
        try:
            confirmed = self.manager.create(resource, current_run_id=run_id, current_fencing_token=fencing_token)
            self._ownership["__integration__"] = confirmed
            self._write_controller_manifest(
                run_id,
                "resources",
                "integration",
                self._resource_payload(
                    confirmed, generation=generation, expected_head=resource.base_sha
                ),
            )
            joined = join_candidates(self.manager, confirmed, current_run_id=run_id, current_fencing_token=fencing_token, base_sha=str(plan["base_sha"]), candidates=[self._candidates[task_id] for task_id in sorted(self._candidates)], integrated_acceptance=plan["integrated_acceptance"])
            integration_diff_hash = self.manager.commit_diff_hash(
                joined.integration_head,
                str(plan["base_sha"]),
                joined.changed_paths,
            )
            self._write_controller_manifest(
                run_id,
                "resources",
                "integration",
                self._resource_payload(
                    confirmed,
                    generation=generation,
                    expected_head=joined.integration_head,
                ),
            )
            self._write_controller_manifest(
                run_id,
                "integrations",
                "integration",
                {
                    "manifest_type": "integration-result",
                    "run_id": run_id,
                    "generation": generation,
                    "fencing_token": fencing_token,
                    "repo_root": str(self.repo_root),
                    "path": str(joined.integration_path),
                    "branch": confirmed.branch,
                    "base_sha": str(plan["base_sha"]),
                    "ledger_slug": confirmed.ledger_slug,
                    "integration_head": joined.integration_head,
                    "applied_task_ids": list(joined.applied_task_ids),
                    "changed_paths": list(joined.changed_paths),
                    "diff_hash": integration_diff_hash,
                    "acceptance": [asdict(item) for item in joined.integrated_acceptance],
                },
            )
            record = {
                "run_id": run_id,
                "integration_head": joined.integration_head,
                "integration_path": str(joined.integration_path),
                "applied_task_ids": list(joined.applied_task_ids),
                "changed_paths": list(joined.changed_paths),
                "acceptance": [asdict(item) for item in joined.integrated_acceptance],
                # This artifact is private (0600) and hash-bound into the
                # review bundle.  Reviewers need the exact frozen argv to
                # interpret the otherwise opaque command hashes.
                "acceptance_argv": [
                    list(command) for command in plan["integrated_acceptance"]
                ],
                "producer_acceptance_argv": {
                    str(task["id"]): [
                        list(command) for command in task["acceptance"]
                    ]
                    for task in writers
                },
            }
            artifact = self.artifact_root / run_id / "integration.json"
            _private_atomic_write(artifact, (json.dumps(record, sort_keys=True) + "\n").encode())
            self._integration_result = {
                "status": "succeeded",
                "integration_head": joined.integration_head,
                "integration_path": str(joined.integration_path),
                "artifact_path": str(artifact),
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
            return dict(self._integration_result)
        except (WorktreeError, JoinDispute, OSError) as exc:
            return {"status": "failed-unsafe", "failure_class": "deterministic-join-failed", "detail": type(exc).__name__}

    def terminal_cleanup(
        self,
        plan: Mapping[str, Any],
        state: Mapping[str, Any],
        *,
        run_id: str,
        generation: int,
        fencing_token: str,
    ) -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
        """Clean only exact, successful, reviewed generated worktrees.

        Branches are always preserved.  A cleanup failure is an independently
        recorded residual and never changes the already established run result.
        """

        reviewers = [task for task in plan["tasks"] if task.get("reviewer_for")]
        tasks_state = state.get("tasks", {})
        eligible = bool(reviewers) and all(
            tasks_state.get(str(task["id"]), {}).get("status") == "succeeded"
            for task in plan["tasks"]
        ) and (state.get("integration") or {}).get("status") == "succeeded"
        outcomes: dict[str, dict[str, Mapping[str, Any]]] = {}
        writers = [
            task for task in plan["tasks"]
            if task["workspace"]["kind"] == "isolated-writer"
        ]
        if eligible:
            try:
                self._recover_candidates(
                    writers,
                    run_id=run_id,
                    generation=generation,
                    fencing_token=fencing_token,
                )
            except (RuntimeErrorSafe, WorktreeError, OSError, KeyError, TypeError, ValueError):
                eligible = False
        for task in writers:
            task_id = str(task["id"])
            resource = self._ownership.get(task_id)
            process = {"status": "succeeded"}
            branch = {"status": "preserved", "reason": "branches-are-never-auto-deleted"}
            if not eligible or resource is None:
                worktree = {
                    "status": "preserved",
                    "reason": "run-not-fully-joined-and-reviewed-or-manifest-unreconciled",
                }
            else:
                try:
                    self.manager.cleanup(
                        resource,
                        current_run_id=run_id,
                        current_fencing_token=fencing_token,
                    )
                    worktree = {"status": "succeeded"}
                except WorktreeError as exc:
                    worktree = {
                        "status": "failed",
                        "reason": type(exc).__name__,
                    }
            outcomes[task_id] = {
                "process": process,
                "worktree": worktree,
                "branch": branch,
            }
        if writers:
            try:
                integration = self._ownership.get("__integration__") or (
                    self._recover_integration_resource(
                        run_id=run_id,
                        generation=generation,
                        fencing_token=fencing_token,
                    )
                    if eligible
                    else None
                )
                if not eligible or integration is None:
                    integration_worktree = {
                        "status": "preserved",
                        "reason": "run-not-fully-joined-and-reviewed-or-manifest-unreconciled",
                    }
                else:
                    self.manager.cleanup(
                        integration,
                        current_run_id=run_id,
                        current_fencing_token=fencing_token,
                    )
                    integration_worktree = {"status": "succeeded"}
            except (RuntimeErrorSafe, WorktreeError, OSError, KeyError, TypeError, ValueError) as exc:
                integration_worktree = {"status": "failed", "reason": type(exc).__name__}
            outcomes["integration"] = {
                "process": {"status": "not-applicable"},
                "worktree": integration_worktree,
                "branch": {
                    "status": "preserved",
                    "reason": "branches-are-never-auto-deleted",
                },
            }
        return outcomes


__all__ = [
    "AgentLedgerCLI",
    "BenchmarkLiveRuntimeAdapter",
    "CheckpointLedger",
    "InMemoryLedger",
    "ManualEventLifecycleAdapter",
    "OrchestrationRuntime",
    "RuntimeErrorSafe",
    "benchmark_live_adapter",
]
