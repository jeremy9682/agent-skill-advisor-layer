"""Compile a frozen benchmark cell into the existing governed lifecycle.

This module is deliberately a *translation seam*: benchmark material supplies
only task bodies, repository scope and acceptance argv.  ``plan.validate_plan``
remains the sole authority for routes, models, effort, seats and permissions.
It also contains the explicit B-arm ready-set driver; importing this module
must never make the manual baseline a disguised ``Scheduler`` invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import os
from pathlib import Path
import subprocess
import time
import re
import shlex
import uuid
from typing import Any, Callable, Mapping, Protocol

from .benchmark import BenchmarkProtocolError, LaunchContract, canonical_json, sha256_value
from .plan import PlanValidationError, validate_plan


class BenchmarkLifecycleError(BenchmarkProtocolError):
    """A benchmark cell cannot be translated without adding authority."""


_AUTHORITY = frozenset({
    "provider", "model", "effort", "seat", "permission", "permission_profile",
    "session", "session_id", "credential", "credentials", "token", "secret",
    "route", "approval_mode", "trust_workspace", "command", "argv",
})
_NODE_KEYS = frozenset({"id", "task_shape", "depends_on", "workspace", "prompt_body", "acceptance_argv"})
_LIFECYCLE_KEYS = frozenset({"fixture_repo_root", "single_producer", "nodes", "review", "integrated_acceptance"})
_REVIEW_KEYS = frozenset({"id", "prompt_body"})


def _fail(message: str) -> None:
    raise BenchmarkLifecycleError(message)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _strict(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(f"{label} has unknown key(s): {', '.join(unknown)}")


def _reject_authority(value: Any, label: str = "lifecycle") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _AUTHORITY or normalized.endswith("_token"):
                _fail(f"{label} attempts to supply forbidden authority: {key}")
            _reject_authority(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_authority(child, f"{label}[{index}]")


def _private_write(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    encoded = body.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)
    return hashlib.sha256(encoded).hexdigest()


def _git_head(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail(f"fixture repository is unavailable: {type(exc).__name__}")
    head = result.stdout.strip()
    if result.returncode != 0 or len(head) != 40 or any(char not in "0123456789abcdef" for char in head):
        _fail("fixture repository has no resolved HEAD")
    return head


def _fixture_identity(repo: Path) -> tuple[str, str]:
    """Return an exact clean top-level repository identity and ledger slug."""

    def git(*args: str) -> str:
        try:
            result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            _fail(f"fixture repository inspection failed: {type(exc).__name__}")
        if result.returncode != 0:
            _fail("fixture repository inspection failed")
        return result.stdout.strip()

    top = Path(git("rev-parse", "--show-toplevel")).resolve()
    if top != repo:
        _fail("fixture_repo_root must be the exact git top-level")
    if git("status", "--porcelain=v1"):
        _fail("fixture repository must have a clean worktree")
    tracked_slug = repo / ".agents" / "ledger-slug"
    if tracked_slug.exists():
        tracked = git("ls-files", "--error-unmatch", ".agents/ledger-slug")
        if tracked != ".agents/ledger-slug":
            _fail("fixture ledger slug must be tracked")
        slug = tracked_slug.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", slug):
            _fail("fixture tracked ledger slug is invalid")
    else:
        slug = re.sub(r"[^a-z0-9-]+", "-", repo.name.lower()).strip("-") or "benchmark"
    return _git_head(repo), slug


def _argv(value: Any, label: str) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        _fail(f"{label} must be non-empty argv arrays")
    output: list[list[str]] = []
    for index, row in enumerate(value):
        if not isinstance(row, list) or not row or any(not isinstance(item, str) or not item for item in row):
            _fail(f"{label}[{index}] must be a non-empty argv array")
        output.append(list(row))
    return output


@dataclass(frozen=True)
class LifecycleLaunch:
    """Compiler result consumed by the current runtime or a fake lifecycle."""

    plan: Mapping[str, Any]
    evaluator_root: Path
    input_manifest_sha256: str
    manual_runbook: Mapping[str, Any] | None
    graph_sha256: str
    manual_runbook_sha256: str


def compile_lifecycle_launch(
    contract: LaunchContract,
    private_task: Mapping[str, Any],
    *,
    reviewer: Mapping[str, Any],
    cell_root: Path,
) -> LifecycleLaunch:
    """Materialize bounded private inputs then compile a governed plan.

    The caller must have already hash-verified ``private_task`` through the
    evaluator manifest.  We still re-check every public/private binding here,
    making this seam safe to call independently from the benchmark harness.
    """

    lifecycle = _mapping(private_task.get("lifecycle"), "private lifecycle")
    _reject_authority(lifecycle)
    _strict(lifecycle, _LIFECYCLE_KEYS, "private lifecycle")
    fixture_raw = lifecycle.get("fixture_repo_root")
    if not isinstance(fixture_raw, str) or not fixture_raw:
        _fail("private lifecycle.fixture_repo_root is required")
    fixture = Path(fixture_raw).expanduser().resolve()
    if not fixture.is_dir() or fixture.is_symlink():
        _fail("fixture_repo_root must be a non-symlink directory")
    base_sha = str(contract.payload.get("base_commit") or "")
    if len(base_sha) < 7:
        _fail("launch contract base_commit is unavailable")
    head, ledger_slug = _fixture_identity(fixture)
    route_policy = Path(__file__).resolve().parents[2] / "routing-policy.yaml"
    if not route_policy.is_file() or hashlib.sha256(route_policy.read_bytes()).hexdigest() != contract.payload.get("route_policy_sha256"):
        _fail("frozen route policy hash drift")
    try:
        resolved = subprocess.run(["git", "-C", str(fixture), "rev-parse", base_sha], capture_output=True, text=True, check=False, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        _fail(f"base SHA verification failed: {type(exc).__name__}")
    if resolved != head:
        _fail("fixture HEAD does not exactly match frozen base SHA")

    public_graph = private_task.get("graph")
    public_runbook = private_task.get("manual_runbook")
    if sha256_value(public_graph) != contract.graph_sha256:
        _fail("graph hash drift before lifecycle compilation")
    if sha256_value(public_runbook) != contract.manual_runbook_sha256:
        _fail("manual runbook hash drift before lifecycle compilation")
    review_prompt = reviewer.get("prompt_sha256")
    review_spec = _mapping(lifecycle.get("review"), "private lifecycle.review")
    _strict(review_spec, _REVIEW_KEYS, "private lifecycle.review")
    if not isinstance(review_spec.get("prompt_body"), str) or not review_spec["prompt_body"].strip():
        _fail("private lifecycle.review.prompt_body is required")
    if sha256_value(review_spec["prompt_body"]) != review_prompt:
        _fail("review prompt hash drift")

    if contract.arm == "A":
        source_nodes = [_mapping(lifecycle.get("single_producer"), "private lifecycle.single_producer")]
        expected_shape = contract.payload.get("task_shape")
        if source_nodes[0].get("task_shape") != expected_shape:
            _fail("A producer shape drift")
    else:
        source_nodes_raw = lifecycle.get("nodes")
        if not isinstance(source_nodes_raw, list) or not source_nodes_raw:
            _fail("private lifecycle.nodes are required for B/C")
        source_nodes = [_mapping(item, "private lifecycle.nodes[]") for item in source_nodes_raw]
    if contract.arm == "B" and not isinstance(public_runbook, Mapping):
        _fail("B requires frozen manual runbook")

    frozen_nodes = public_graph.get("nodes") if isinstance(public_graph, Mapping) else None
    if not isinstance(frozen_nodes, list) or not frozen_nodes:
        _fail("frozen graph nodes are required")
    frozen_by_id = {str(node.get("id")): node for node in frozen_nodes if isinstance(node, Mapping)}
    if len(frozen_by_id) != len(frozen_nodes):
        _fail("frozen graph node identities are invalid")
    if contract.arm == "A":
        prompt = source_nodes[0].get("prompt_body")
        if sha256_value(prompt) != contract.payload.get("prompt_sha256"):
            _fail("A producer prompt hash drift")
    else:
        if set(frozen_by_id) != {str(node.get("id")) for node in source_nodes}:
            _fail("lifecycle nodes are not the frozen graph")
        for node in source_nodes:
            frozen = frozen_by_id[str(node.get("id"))]
            observed = {
                "id": node.get("id"),
                "task_shape": node.get("task_shape"),
                "depends_on": node.get("depends_on", []),
                "prompt_sha256": sha256_value(node.get("prompt_body")),
                "acceptance_sha256": sha256_value(node.get("acceptance_argv")),
            }
            expected = {
                "id": frozen.get("id"),
                "task_shape": frozen.get("task_shape"),
                "depends_on": frozen.get("depends_on", []),
                "prompt_sha256": frozen.get("prompt_sha256"),
                "acceptance_sha256": frozen.get("acceptance_sha256"),
            }
            if observed != expected:
                _fail("lifecycle graph projection drift")

    public_acceptance = contract.payload.get("acceptance_commands")
    if not isinstance(public_acceptance, list) or not all(isinstance(item, str) and item for item in public_acceptance):
        _fail("frozen public acceptance commands are required")
    frozen_deadline = contract.payload.get("deadline_seconds")
    if not isinstance(frozen_deadline, int) or isinstance(frozen_deadline, bool) or frozen_deadline <= 0:
        _fail("frozen producer deadline is required")

    input_root = (cell_root.expanduser().resolve() / "evaluator-inputs").resolve()
    if input_root.exists():
        _fail("cell evaluator input root already exists")
    input_root.mkdir(parents=True, mode=0o700)
    os.chmod(input_root, 0o700)
    manifest: dict[str, str] = {}
    plan_tasks: list[dict[str, Any]] = []
    node_ids: list[str] = []
    for node in source_nodes:
        _strict(node, _NODE_KEYS, "private lifecycle node")
        _reject_authority(node, "private lifecycle node")
        node_id, shape, prompt = node.get("id"), node.get("task_shape"), node.get("prompt_body")
        if not isinstance(node_id, str) or not node_id or not isinstance(shape, str) or not shape or not isinstance(prompt, str) or not prompt.strip():
            _fail("lifecycle node id, task_shape and prompt_body are required")
        if node_id in node_ids:
            _fail("lifecycle node ids must be unique")
        node_ids.append(node_id)
        digest = _private_write(input_root / f"{node_id}.txt", prompt)
        manifest[node_id] = digest
        depends = node.get("depends_on", [])
        if not isinstance(depends, list) or any(not isinstance(item, str) or not item for item in depends):
            _fail(f"{node_id}.depends_on must be string ids")
        plan_tasks.append({
            "id": node_id,
            "task_shape": shape,
            "depends_on": list(depends),
            "workspace": dict(_mapping(node.get("workspace"), f"{node_id}.workspace")),
            "input_ref": f"evaluator:{node_id}",
            "acceptance": _argv(node.get("acceptance_argv"), f"{node_id}.acceptance_argv"),
            "deadline_seconds": frozen_deadline,
        })
        if (
            contract.arm == "A"
            and [shlex.join(argv) for argv in plan_tasks[-1]["acceptance"]]
            != public_acceptance
        ):
            _fail("producer acceptance differs from frozen public acceptance")
    review_id = review_spec.get("id", "review")
    if not isinstance(review_id, str) or not review_id or review_id in node_ids:
        _fail("private lifecycle review id is invalid")
    review_digest = _private_write(input_root / f"{review_id}.txt", str(review_spec["prompt_body"]))
    manifest[review_id] = review_digest
    review_route = reviewer.get("route")
    if not isinstance(review_route, str) or not review_route:
        _fail("public reviewer route is unavailable")
    plan_tasks.append({
        "id": review_id,
        "task_shape": review_route,
        "depends_on": list(node_ids),
        "reviewer_for": list(node_ids),
        "workspace": {"kind": "read-only"},
        "input_ref": f"evaluator:{review_id}",
        "deadline_seconds": reviewer.get("timeout_seconds"),
    })
    if not isinstance(reviewer.get("timeout_seconds"), int) or reviewer["timeout_seconds"] <= 0:
        _fail("frozen reviewer timeout is required")
    writers = sum(1 for task in plan_tasks if task["workspace"].get("kind") == "isolated-writer")
    manual = dict(public_runbook) if contract.arm == "B" else None
    if manual is not None:
        ready_sets = manual.get("ready_sets")
        if not isinstance(ready_sets, list) or not ready_sets:
            _fail("manual runbook must declare explicit ready_sets")
        flattened = [item for group in ready_sets if isinstance(group, list) for item in group]
        if any(not isinstance(item, str) for item in flattened) or set(flattened) != set(node_ids):
            _fail("manual runbook ready_sets must cover exactly producer node ids")
        complete: set[str] = set()
        for group in ready_sets:
            if not isinstance(group, list) or not group:
                _fail("manual runbook ready_sets must be non-empty lists")
            for task_id in group:
                dependencies = next(task["depends_on"] for task in plan_tasks if task["id"] == task_id)
                if any(dependency not in complete for dependency in dependencies):
                    _fail("manual runbook ready_sets violate frozen graph topology")
            complete.update(group)
    raw_plan = {
        "version": 1,
        "run_id": f"benchmark-{contract.task_id}-{contract.arm}",
        "repo_root": str(fixture),
        "base_sha": head,
        "ledger_slug": ledger_slug[:64],
        "tasks": plan_tasks,
        "budgets": {"total_concurrency": min(3, max(1, len(plan_tasks))), "writer_concurrency": 1 if contract.arm == "A" else min(2, max(1, writers))},
        "integrated_acceptance": _argv(lifecycle.get("integrated_acceptance"), "integrated_acceptance"),
        "metadata": {"benchmark_graph_sha256": contract.graph_sha256, "benchmark_manual_runbook_sha256": contract.manual_runbook_sha256},
    }
    if [shlex.join(argv) for argv in raw_plan["integrated_acceptance"]] != public_acceptance:
        _fail("integrated acceptance differs from frozen public acceptance")
    try:
        compiled = validate_plan(raw_plan)
    except PlanValidationError as exc:
        _fail(f"governed lifecycle compiler rejected benchmark cell: {exc}")
    compiled_reviewer = next(task for task in compiled["tasks"] if task["id"] == review_id)
    public_independence = reviewer.get("independence")
    independence_map = {
        "cross_family": "cross-family",
        "same_family_independent": "independent-supplement",
    }
    if public_independence not in independence_map:
        _fail("public reviewer independence is not a frozen protocol value")
    expected_reviewer = {
        "route": reviewer.get("route"),
        "model": reviewer.get("model"),
        "effort": reviewer.get("effort"),
        "family": reviewer.get("family"),
        "independence": independence_map[public_independence],
    }
    observed_reviewer = {
        "route": compiled_reviewer["task_shape"],
        "model": compiled_reviewer["binding"].get("model"),
        "effort": compiled_reviewer["binding"].get("effort"),
        "family": compiled_reviewer.get("model_family"),
        "independence": compiled_reviewer["binding"].get("review_independence"),
    }
    if expected_reviewer != observed_reviewer:
        _fail("compiled reviewer binding differs from frozen public reviewer")
    manifest_sha = sha256_value(manifest)
    _private_write(input_root / "manifest.json", canonical_json({"version": 1, "inputs": manifest}) + "\n")
    return LifecycleLaunch(compiled, input_root, manifest_sha, manual, contract.graph_sha256, contract.manual_runbook_sha256)


class ManualLifecycle(Protocol):
    """Narrow B-arm seam; it intentionally is not ``Scheduler``.

    A production adapter exposes the two-phase methods and the resource/review
    preparation methods.  A test double may use ``run_task`` only when it
    explicitly advertises ``allow_fake_single_phase = True``.
    """

    allow_fake_single_phase: bool

    def prepare_resource(self, task: Mapping[str, Any], *, ownership: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def launch_task(self, task: Mapping[str, Any], **kwargs: Any) -> Any: ...
    def collect_task(self, launched: Any) -> Mapping[str, Any]: ...
    def prepare_review(self, task: Mapping[str, Any], state: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]: ...
    def run_task(self, task: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]: ...
    def finalize_run(self, plan: Mapping[str, Any], state: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]: ...
    def cleanup_terminal(self, state: Mapping[str, Any], *, preserve: bool) -> Mapping[str, Any]: ...


def _attempt_id(run_id: str, task_id: str) -> str:
    return "attempt-" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"benchmark-manual:{run_id}:{task_id}"))


def _fence(run_id: str) -> str:
    return "fence-" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"benchmark-manual:{run_id}:1"))


def _interval_id(run_id: str, ready_set: list[str]) -> str:
    return "interval-" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"benchmark-manual:{run_id}:{','.join(sorted(ready_set))}"))


def _admitted_batches(tasks: list[Mapping[str, Any]], budgets: Mapping[str, Any]) -> list[list[Mapping[str, Any]]]:
    """Partition one frozen ready set without exceeding compiled admission."""

    total = int(budgets["total_concurrency"])
    writers = int(budgets["writer_concurrency"])
    family_limits = dict(budgets["family_concurrency"])
    pending = list(tasks)
    batches: list[list[Mapping[str, Any]]] = []
    while pending:
        batch: list[Mapping[str, Any]] = []
        family_counts: dict[str, int] = {}
        writer_count = 0
        for task in list(pending):
            is_writer = task["workspace"]["kind"] == "isolated-writer"
            family = str(task["family"])
            if len(batch) >= total or (is_writer and writer_count >= writers):
                continue
            if family_counts.get(family, 0) >= int(family_limits[family]):
                continue
            batch.append(task)
            pending.remove(task)
            family_counts[family] = family_counts.get(family, 0) + 1
            writer_count += int(is_writer)
        if not batch:
            _fail("compiled admission cannot admit a frozen manual ready set")
        batches.append(batch)
    return batches


def _resource_intent(launch: LifecycleLaunch, task: Mapping[str, Any], *, fence: str) -> dict[str, Any]:
    return {
        "created_by_run_id": launch.plan["run_id"],
        "fencing_token": fence,
        "repo_root": launch.plan["repo_root"],
        "base_sha": launch.plan["base_sha"],
        "ledger_slug": launch.plan["ledger_slug"],
        "task_id": task["id"],
        "generation": 1,
        # An adapter owns the actual worktree root/path, never evaluator input.
        "branch": f"agent-run/{launch.plan['run_id']}/{task['id']}",
    }


def run_manual_ready_sets(
    launch: LifecycleLaunch,
    lifecycle: ManualLifecycle,
    *,
    event_sink: Callable[[Mapping[str, Any]], None] | None = None,
) -> Mapping[str, Any]:
    """Run B's frozen ready sets without importing or instantiating Scheduler.

    Events record actual lifecycle operations.  This function does not invent
    attribution, provider results, or cleanup: all those remain runtime facts.
    """

    if launch.manual_runbook is None:
        _fail("manual ready-set runner is valid only for B")
    emit = event_sink or (lambda _event: None)
    tasks = {str(task["id"]): task for task in launch.plan["tasks"]}
    review = next((task for task in tasks.values() if task.get("reviewer_for")), None)
    if review is None:
        _fail("manual plan has no governed reviewer")
    state: dict[str, Any] = {"tasks": {}, "integration": {"status": "pending"}}
    run_id = str(launch.plan["run_id"])
    fence = _fence(run_id)
    two_phase = callable(getattr(lifecycle, "launch_task", None)) and callable(getattr(lifecycle, "collect_task", None))
    fake_single = getattr(lifecycle, "allow_fake_single_phase", False) is True
    if not two_phase and not fake_single:
        _fail("manual lifecycle requires two-phase launch/collect or explicit fake seam")
    for ready_set in launch.manual_runbook["ready_sets"]:
        ids = list(ready_set)
        interval = _interval_id(run_id, ids)
        emit({"event": "coordination_started", "at": time.time(), "interval_id": interval, "ready_set": ids})
        ready_tasks = [tasks[task_id] for task_id in ids]
        for task in ready_tasks:
            if any(state["tasks"].get(dep, {}).get("status") != "succeeded" for dep in task["depends_on"]):
                _fail(f"manual ready-set dependency is not satisfied: {task['id']}")
        for batch in _admitted_batches(ready_tasks, launch.plan["budgets"]):
            for task in batch:
                if task["workspace"]["kind"] == "isolated-writer":
                    prepared = lifecycle.prepare_resource(task, ownership=_resource_intent(launch, task, fence=fence))
                    if not isinstance(prepared, Mapping) or prepared.get("status") not in {"created", "adopted"}:
                        _fail(f"writer resource intent failed: {task['id']}")
            def execute(task: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
                attempt = _attempt_id(run_id, str(task["id"]))
                emit(
                    {
                        "event": "producer_started",
                        "at": time.time(),
                        "task_id": str(task["id"]),
                    }
                )
                if two_phase:
                    launched = lifecycle.launch_task(
                        task,
                        run_id=run_id,
                        attempt_id=attempt,
                        generation=1,
                        state=state,
                        fencing_token=fence,
                    )
                    return str(task["id"]), dict(lifecycle.collect_task(launched))
                return str(task["id"]), dict(lifecycle.run_task(task, run_id=run_id, attempt_id=attempt, generation=1))
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = [executor.submit(execute, task) for task in batch]
                for future in as_completed(futures):
                    task_id, result = future.result()
                    state["tasks"][task_id] = {
                        "status": result.get("status"),
                        "result": result,
                        "current_attempt_id": _attempt_id(run_id, task_id),
                    }
                    if (
                        result.get("status") == "failed"
                        and result.get("failure_class") == "acceptance-failed"
                    ):
                        # Collect returned a concrete controller acceptance
                        # result. It proves only failed acceptance, never a
                        # candidate commit or integration success.
                        emit(
                            {
                                "event": "acceptance_completed",
                                "at": time.time(),
                                "task_id": task_id,
                                "accepted": False,
                                "failure_class": "acceptance-failed",
                            }
                        )
        failed = [task_id for task_id in ids if state["tasks"].get(task_id, {}).get("status") != "succeeded"]
        status = "succeeded" if not failed else "partial-failure"
        emit({"event": "coordination_completed", "at": time.time(), "interval_id": interval, "ready_set": ids, "status": status})
        if failed:
            return {"status": "partial-failure", "state": state, "events_complete": True}
    emit({"event": "candidate_created", "at": time.time()})
    integration = dict(lifecycle.finalize_run(launch.plan, state, run_id=run_id, generation=1, fencing_token=fence))
    state["integration"] = integration
    if integration.get("status") != "succeeded":
        return {"status": "partial-failure", "state": state}
    review_attempt = _attempt_id(run_id, str(review["id"]))
    prepared_review = lifecycle.prepare_review(review, state, run_id=run_id, attempt_id=review_attempt, generation=1, fencing_token=fence)
    if not isinstance(prepared_review, Mapping) or prepared_review.get("status") != "succeeded":
        return {"status": "partial-failure", "state": state}
    emit({"event": "review_started", "at": time.time(), "interval_id": _interval_id(run_id, [str(review["id"])])})
    if two_phase:
        review_launch = lifecycle.launch_task(
            review,
            run_id=run_id,
            attempt_id=review_attempt,
            generation=1,
            state=state,
            fencing_token=fence,
        )
        review_result = dict(lifecycle.collect_task(review_launch))
    else:
        review_result = dict(lifecycle.run_task(review, run_id=run_id, attempt_id=review_attempt, generation=1))
    state["tasks"][review["id"]] = {"status": review_result.get("status"), "result": review_result}
    emit({"event": "review_completed", "at": time.time(), "interval_id": _interval_id(run_id, [str(review["id"])]), "status": review_result.get("status")})
    return {"status": "succeeded" if review_result.get("status") == "succeeded" else "partial-failure", "state": state}


def cleanup_terminal(lifecycle: Any, state: Mapping[str, Any], *, preserve: bool = True) -> Mapping[str, Any]:
    """Optional terminal seam; benchmark default preserves all resources."""

    cleaner = getattr(lifecycle, "cleanup_terminal", None)
    if not callable(cleaner):
        return {"status": "preserved", "reason": "runtime-cleanup-seam-unavailable"}
    return dict(cleaner(state, preserve=preserve))
