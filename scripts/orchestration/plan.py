"""Versioned orchestration plan loader and fail-closed static compiler.

Plans select only names from ``routing-policy.yaml``.  Provider, model, effort,
seat and permissions are projections added by this compiler; a plan cannot
spell or override them.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

import yaml

try:
    from scripts.routing_runtime import (
        load_routing_canon,
        resolve_binding,
        resolve_model_family,
    )
except ModuleNotFoundError:  # direct imports with scripts/ on sys.path
    from routing_runtime import (
        load_routing_canon,
        resolve_binding,
        resolve_model_family,
    )


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANON = ROOT / "routing-policy.yaml"
DEFAULT_PROVIDER_MANIFEST = ROOT / "agent-providers.yaml"
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FORBIDDEN_AUTHORITY_KEYS = {
    "provider",
    "model",
    "effort",
    "governance_effort",
    "seat",
    "execution_mode",
    "mode",
    "permission",
    "permission_profile",
    "review_independence",
    "serial_group",
    "route",
    "command",
    "argv",
    "environment",
    "init_hook",
    "account",
    "profile",
    "session",
    "session_id",
}
UNSAFE_NATIVE_FLAGS = {
    "--yolo",
    "--dangerously-skip-permissions",
    "--skip-permissions",
    "--allow-all",
    "--trust",
    "--force",
    "-f",
}
SHELL_COMMAND_STRING_INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
ALLOWED_TOP_LEVEL = {
    "version",
    "run_id",
    "repo_root",
    "base_sha",
    "ledger_slug",
    "tasks",
    "budgets",
    "integrated_acceptance",
    "config_fingerprint",
    "metadata",
}
ALLOWED_TASK_KEYS = {
    "id",
    "task_shape",
    "depends_on",
    "workspace",
    "deadline_seconds",
    "retry",
    "input_ref",
    "acceptance",
    "reviewer_for",
    "metadata",
}
ALLOWED_WORKSPACE_KEYS = {
    "kind",
    "own",
    "do_not_touch",
    "shared_interface_paths",
}
ALLOWED_RETRY_CLASSES = {
    "provider-transient",
    "provider-rate-limit",
    "provider-preflight-transient",
    "adapter-transient",
}
METADATA_DENY_KEYS = FORBIDDEN_AUTHORITY_KEYS | {
    "prompt",
    "response",
    "transcript",
    "credential",
    "credentials",
    "cookie",
    "cookies",
    "token",
    "base_url",
    "config",
    "config_body",
}


class PlanValidationError(ValueError):
    """The supplied plan would add authority or cannot execute safely."""


def _fail(message: str) -> None:
    raise PlanValidationError(message)


def _strict_keys(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(f"{where} has unknown key(s): {', '.join(unknown)}")


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{where} must be a mapping")
    return value


def _string_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        _fail(f"{where} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        _fail(f"{where} contains duplicates")
    return list(value)


def _safe_relative(raw: str, where: str, *, glob_ok: bool = True) -> str:
    if "\\" in raw or "\x00" in raw:
        _fail(f"{where} contains an unsafe path")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or any(p in {"", ".", ".."} for p in path.parts)
    ):
        _fail(f"{where} must be a normalized repository-relative path")
    if path.parts[0] == ".git" or any(part == ".git" for part in path.parts):
        _fail(f"{where} may not target Git metadata")
    if not glob_ok and any(char in raw for char in "*?["):
        _fail(f"{where} may not contain a glob")
    normalized = path.as_posix().rstrip("/")
    if not normalized:
        _fail(f"{where} may not be empty")
    return normalized


def _covers(owner: str, path: str) -> bool:
    owner_prefix = owner.rstrip("*").rstrip("/")
    if any(char in owner for char in "*?["):
        import fnmatch

        return fnmatch.fnmatchcase(path, owner) or path.startswith(owner_prefix + "/")
    return path == owner or path.startswith(owner + "/")


def _paths_overlap(left: str, right: str) -> bool:
    lp = left.rstrip("*").rstrip("/")
    rp = right.rstrip("*").rstrip("/")
    return (
        left == right or lp == rp or lp.startswith(rp + "/") or rp.startswith(lp + "/")
    )


def _validate_argv_list(value: Any, where: str) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        _fail(f"{where} must be a list of argv arrays")
    output: list[list[str]] = []
    for index, command in enumerate(value):
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(arg, str) or not arg for arg in command)
        ):
            _fail(f"{where}[{index}] must be a non-empty argv array")
        lowered = {arg.lower() for arg in command}
        shell_command_string = False
        python_command_string = False
        # Wrappers such as ``env`` and ``timeout`` must not turn a forbidden
        # command string into an apparently safe argv-native invocation.
        for position, raw_arg in enumerate(command):
            executable = PurePosixPath(raw_arg).name.lower()
            trailing = command[position + 1 :]
            if executable in SHELL_COMMAND_STRING_INTERPRETERS and any(
                arg == "-c"
                or (
                    arg.startswith("-")
                    and not arg.startswith("--")
                    and "c" in arg[1:]
                )
                for arg in trailing
            ):
                shell_command_string = True
            if (
                re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable)
                and "-c" in trailing
            ):
                python_command_string = True
        if lowered & UNSAFE_NATIVE_FLAGS or shell_command_string or python_command_string:
            _fail(f"{where}[{index}] contains a forbidden bypass/force invocation")
        output.append(list(command))
    return output


def _validate_metadata(value: Any, where: str = "metadata") -> Any:
    """Allow annotations, never a shadow authority or body/secret channel."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        output = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                _fail(f"{where} keys must be non-empty strings")
            normalized = key.lower().replace("-", "_")
            if normalized in METADATA_DENY_KEYS or normalized.endswith("_token"):
                _fail(f"{where}.{key} is forbidden metadata")
            output[key] = _validate_metadata(child, f"{where}.{key}")
        return output
    if isinstance(value, list):
        return [_validate_metadata(child, f"{where}[]") for child in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if "\n" in value or "\r" in value or len(value) > 512:
            _fail(f"{where} may not contain an inline body")
        return value
    _fail(f"{where} contains an unsupported value")


def _load_canon(canon: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return canon if canon is not None else load_routing_canon(DEFAULT_CANON)


def _load_provider_capabilities() -> Mapping[str, Any]:
    """Load model-family capability facts, never routes, from the manifest."""
    try:
        manifest = yaml.safe_load(DEFAULT_PROVIDER_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PlanValidationError(f"cannot load provider capabilities: {exc}") from exc
    if not isinstance(manifest, Mapping) or manifest.get("version") != 1:
        _fail("provider capability manifest must be version 1")
    if "routes" in manifest:
        _fail("provider capability manifest may not define a second routes source")
    providers = manifest.get("providers")
    if not isinstance(providers, Mapping) or not providers:
        _fail("provider capability manifest needs providers")
    return providers


def _canon_family_limits(canon: Mapping[str, Any]) -> dict[str, int]:
    """Compile provider limits plus hard serial-group limits from the canon."""
    concurrency = _mapping(
        canon.get("concurrency_policy"), "routing canon concurrency_policy"
    )
    providers = _mapping(
        concurrency.get("provider_family_limits"),
        "routing canon provider_family_limits",
    )
    limits: dict[str, int] = {}
    for family, limit in providers.items():
        if (
            not isinstance(family, str)
            or not family
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 3
        ):
            _fail("routing canon provider family limits must be integers in 1..3")
        limits[family] = limit
    routes = _mapping(canon.get("runtime_routes"), "routing canon runtime_routes")
    for route_name, route_raw in routes.items():
        route = _mapping(route_raw, f"routing canon route {route_name}")
        serial_group = route.get("serial_group")
        concurrency_mode = route.get("concurrency")
        if concurrency_mode == "family_serial" and not serial_group:
            _fail(f"routing canon serial route {route_name} lacks serial_group")
        if serial_group:
            if not isinstance(serial_group, str) or not serial_group:
                _fail(f"routing canon route {route_name} has invalid serial_group")
            limits[serial_group] = 1
        elif route.get("route_policy", "enabled") == "enabled":
            provider = route.get("provider")
            if not isinstance(provider, str) or provider not in limits:
                _fail(
                    f"routing canon parallel route {route_name} lacks a provider family limit"
                )
    return limits


def load_plan(path: Path, *, canon: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Load JSON/YAML and return the normalized, validated plan."""
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            raw = yaml.safe_load(text)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise PlanValidationError(f"cannot load orchestration plan: {exc}") from exc
    return validate_plan(_mapping(raw, "plan"), canon=canon)


def validate_plan(
    raw: Mapping[str, Any], *, canon: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and compile a plan without mutating *raw*."""
    plan = copy.deepcopy(dict(raw))
    _strict_keys(plan, ALLOWED_TOP_LEVEL, "plan")
    if plan.get("version") != 1:
        _fail("plan.version must be 1")
    run_id = plan.get("run_id")
    if run_id is not None and (
        not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id)
    ):
        _fail("plan.run_id is invalid")
    repo_root_raw = plan.get("repo_root")
    if not isinstance(repo_root_raw, str) or not os.path.isabs(repo_root_raw):
        _fail("plan.repo_root must be an absolute path")
    repo_root = str(Path(repo_root_raw).resolve())
    base_sha = plan.get("base_sha")
    ledger_slug = plan.get("ledger_slug")
    if base_sha is not None and (
        not isinstance(base_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", base_sha)
    ):
        _fail("plan.base_sha must be a 40- or 64-character lowercase hex digest")
    if ledger_slug is not None and (
        not isinstance(ledger_slug, str) or not ID_RE.fullmatch(ledger_slug)
    ):
        _fail("plan.ledger_slug is invalid")
    plan["metadata"] = _validate_metadata(plan.get("metadata"), "plan.metadata")
    tasks_raw = plan.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        _fail("plan.tasks must be a non-empty list")

    budgets = dict(_mapping(plan.get("budgets", {}), "plan.budgets"))
    _strict_keys(
        budgets,
        {"total_concurrency", "writer_concurrency", "family_concurrency"},
        "plan.budgets",
    )
    total = budgets.get("total_concurrency", 3)
    writers = budgets.get("writer_concurrency", 1)
    if not isinstance(total, int) or isinstance(total, bool) or not 1 <= total <= 3:
        _fail("total_concurrency must be between 1 and the V1 hard maximum 3")
    if (
        not isinstance(writers, int)
        or isinstance(writers, bool)
        or not 1 <= writers <= 2
    ):
        _fail("writer_concurrency must be between 1 and the V1 hard maximum 2")
    if writers > total:
        _fail("writer_concurrency may not exceed total_concurrency")
    routing = _load_canon(canon)
    providers = _load_provider_capabilities()
    canon_family_limits = _canon_family_limits(routing)
    requested_family_limits = dict(
        _mapping(budgets.get("family_concurrency", {}), "family_concurrency")
    )
    for family, limit in requested_family_limits.items():
        if (
            not isinstance(family, str)
            or not family
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= total
        ):
            _fail(
                "family_concurrency must map non-empty family names to positive bounded integers"
            )
        if family not in canon_family_limits:
            _fail(f"family_concurrency names unknown canon family {family!r}")
        if limit > canon_family_limits[family]:
            _fail(
                f"family_concurrency for {family!r} exceeds canon limit {canon_family_limits[family]}"
            )
    family_limits = {
        family: min(requested_family_limits.get(family, limit), limit, total)
        for family, limit in canon_family_limits.items()
    }
    compiled_tasks: list[dict[str, Any]] = []
    ids: set[str] = set()
    writer_owners: list[tuple[str, str]] = []
    review_targets: dict[str, str] = {}
    for index, task_raw in enumerate(tasks_raw):
        task = dict(_mapping(task_raw, f"tasks[{index}]"))
        authority = sorted(set(task) & FORBIDDEN_AUTHORITY_KEYS)
        if authority:
            _fail(
                f"tasks[{index}] attempts to override governed authority: {', '.join(authority)}"
            )
        _strict_keys(task, ALLOWED_TASK_KEYS, f"tasks[{index}]")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not ID_RE.fullmatch(task_id):
            _fail(f"tasks[{index}].id is invalid")
        if task_id in ids:
            _fail(f"duplicate task id: {task_id}")
        ids.add(task_id)
        shape = task.get("task_shape")
        if not isinstance(shape, str):
            _fail(f"task {task_id} needs task_shape")
        try:
            binding = resolve_binding(dict(routing), shape)
        except Exception as exc:
            _fail(f"task {task_id} has unknown or invalid task_shape {shape!r}: {exc}")
        if binding.get("route_policy", "enabled") != "enabled":
            _fail(f"task {task_id} selects disabled task_shape {shape!r}")
        provider_id = binding["provider"]
        provider = providers.get(provider_id)
        if not isinstance(provider, Mapping):
            _fail(f"task {task_id} route provider {provider_id!r} lacks capabilities")
        model_family = resolve_model_family(dict(provider), binding["model"])
        if not isinstance(model_family, str) or not model_family:
            _fail(f"task {task_id} model family is not disclosed")

        dependencies = _string_list(
            task.get("depends_on"), f"task {task_id}.depends_on"
        )
        if task_id in dependencies:
            _fail(f"task {task_id} cannot depend on itself")
        workspace = dict(
            _mapping(
                task.get("workspace", {"kind": "read-only"}),
                f"task {task_id}.workspace",
            )
        )
        _strict_keys(workspace, ALLOWED_WORKSPACE_KEYS, f"task {task_id}.workspace")
        kind = workspace.get("kind", "read-only")
        if kind not in {"read-only", "isolated-writer"}:
            _fail(f"task {task_id}.workspace.kind must be read-only or isolated-writer")
        own = [
            _safe_relative(p, f"task {task_id}.workspace.own")
            for p in _string_list(workspace.get("own"), f"task {task_id}.workspace.own")
        ]
        blocked = [
            _safe_relative(p, f"task {task_id}.workspace.do_not_touch")
            for p in _string_list(
                workspace.get("do_not_touch"), f"task {task_id}.workspace.do_not_touch"
            )
        ]
        shared = [
            _safe_relative(
                p, f"task {task_id}.workspace.shared_interface_paths", glob_ok=False
            )
            for p in _string_list(
                workspace.get("shared_interface_paths"),
                f"task {task_id}.workspace.shared_interface_paths",
            )
        ]
        if kind == "read-only" and (own or shared):
            _fail(f"read-only task {task_id} may not claim writer ownership")
        if kind == "isolated-writer" and not own:
            _fail(f"writer task {task_id} must declare workspace.own")
        for path in shared:
            if not any(_covers(owner, path) for owner in own):
                _fail(
                    f"task {task_id} shared interface {path!r} is outside workspace.own"
                )
        for owner in own:
            if any(_paths_overlap(owner, denied) for denied in blocked):
                _fail(f"task {task_id} ownership overlaps do_not_touch: {owner!r}")
            for other_task, other_owner in writer_owners:
                if _paths_overlap(owner, other_owner):
                    _fail(
                        f"writer ownership overlaps between {other_task} and {task_id}: {owner!r}"
                    )
            writer_owners.append((task_id, owner))

        timeout = binding.get("timeout_seconds", 300)
        deadline = task.get("deadline_seconds", timeout)
        if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline <= 0:
            _fail(f"task {task_id}.deadline_seconds must be positive")
        if deadline > timeout:
            _fail(
                f"task {task_id}.deadline_seconds exceeds governed route timeout {timeout}"
            )
        retry = dict(_mapping(task.get("retry", {}), f"task {task_id}.retry"))
        _strict_keys(retry, {"max_attempts", "retry_on"}, f"task {task_id}.retry")
        max_attempts = retry.get("max_attempts", 1)
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= 3
        ):
            _fail(f"task {task_id}.retry.max_attempts must be 1..3")
        retry_on = _string_list(retry.get("retry_on"), f"task {task_id}.retry.retry_on")
        unknown_retry = sorted(set(retry_on) - ALLOWED_RETRY_CLASSES)
        if unknown_retry:
            _fail(
                f"task {task_id} has unsupported retry class(es): {', '.join(unknown_retry)}"
            )
        acceptance = _validate_argv_list(
            task.get("acceptance"), f"task {task_id}.acceptance"
        )
        if kind != "isolated-writer" and acceptance:
            _fail(f"read-only task {task_id} may not declare inert acceptance")
        input_ref = task.get("input_ref")
        if input_ref is not None:
            if not isinstance(input_ref, str) or not input_ref:
                _fail(f"task {task_id}.input_ref must be a non-empty string")
            if input_ref.startswith("evaluator:"):
                evaluator_id = input_ref.removeprefix("evaluator:")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", evaluator_id):
                    _fail(f"task {task_id}.input_ref has an invalid evaluator pointer")
            else:
                input_ref = _safe_relative(
                    input_ref, f"task {task_id}.input_ref", glob_ok=False
                )
                candidate = (Path(repo_root) / input_ref).resolve()
                try:
                    candidate.relative_to(Path(repo_root))
                except ValueError:
                    _fail(f"task {task_id}.input_ref escapes repo_root")
                if not candidate.is_file():
                    _fail(
                        f"task {task_id}.input_ref must name an existing repository file or evaluator pointer"
                    )
            task["input_ref"] = input_ref
        task["metadata"] = _validate_metadata(
            task.get("metadata"), f"task {task_id}.metadata"
        )
        reviewers = _string_list(
            task.get("reviewer_for"), f"task {task_id}.reviewer_for"
        )
        if reviewers and "review" not in shape:
            _fail(
                f"task {task_id} declares reviewer_for but is not a governed review shape"
            )
        for target in reviewers:
            if target in review_targets:
                _fail(
                    f"task {target} reuses reviewers {review_targets[target]} and {task_id}"
                )
            review_targets[target] = task_id

        family = str(binding.get("serial_group") or binding["provider"])
        if family not in family_limits:
            _fail(
                f"task {task_id} route family {family!r} has no canon admission limit"
            )
        effective_family_limit = (
            1 if binding.get("serial_group") else family_limits[family]
        )
        workspace_normalized = {
            "kind": kind,
            "own": own,
            "do_not_touch": blocked,
            "shared_interface_paths": shared,
        }
        task.update(
            {
                "depends_on": dependencies,
                "workspace": workspace_normalized,
                "deadline_seconds": deadline,
                "retry": {"max_attempts": max_attempts, "retry_on": retry_on},
                "acceptance": acceptance,
                "reviewer_for": reviewers,
                "binding": binding,
                "model_family": model_family,
                "family": family,
                "family_limit": effective_family_limit,
                "permission_projection": {
                    "execution_mode": "execute"
                    if kind == "isolated-writer"
                    else "read-only",
                    "permission_profile": "workspace-write"
                    if kind == "isolated-writer"
                    else "read-only",
                },
            }
        )
        compiled_tasks.append(task)

    by_id = {task["id"]: task for task in compiled_tasks}
    for task in compiled_tasks:
        missing = sorted(set(task["depends_on"]) - set(by_id))
        if missing:
            _fail(f"task {task['id']} has missing dependencies: {', '.join(missing)}")
        for target in task["reviewer_for"]:
            if target not in by_id:
                _fail(f"task {task['id']} reviews unknown task {target}")
            if target not in task["depends_on"]:
                _fail(f"review task {task['id']} must depend on reviewed task {target}")
            producer = by_id[target]
            reviewer_family = task["model_family"]
            producer_family = producer["model_family"]
            independence = task["binding"].get("review_independence")
            if independence == "cross-family":
                disclosed = {
                    reviewer_family.lower(),
                    producer_family.lower(),
                }.isdisjoint({"undisclosed", "unknown", "opaque"})
                if not disclosed:
                    _fail(f"review task {task['id']} requires disclosed model families")
                if reviewer_family == producer_family:
                    _fail(
                        f"review task {task['id']} and producer {target} share model family {reviewer_family!r}"
                    )
            elif independence == "independent-supplement":
                eligible = task["binding"].get("eligible_producer_routes", [])
                if producer["task_shape"] not in eligible:
                    _fail(
                        f"review task {task['id']} is not eligible for producer route {producer['task_shape']!r}"
                    )
            else:
                _fail(
                    f"review task {task['id']} lacks a governed independence contract"
                )
        if task["reviewer_for"]:
            task["reviewer_independence_projection"] = {
                "kind": task["binding"]["review_independence"],
                "reviewer_task_id": task["id"],
                "producer_task_ids": list(task["reviewer_for"]),
                "require_distinct_attempt_id": True,
                "require_fresh_session": True,
            }

    # Kahn's algorithm also produces the deterministic task order used by the scheduler.
    indegree = {task_id: 0 for task_id in by_id}
    children = {task_id: [] for task_id in by_id}
    for task in compiled_tasks:
        for dependency in task["depends_on"]:
            indegree[task["id"]] += 1
            children[dependency].append(task["id"])
    ready = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
    topo: list[str] = []
    while ready:
        current = ready.pop(0)
        topo.append(current)
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(topo) != len(by_id):
        cycle_nodes = sorted(task_id for task_id, degree in indegree.items() if degree)
        _fail("task graph contains a cycle involving: " + ", ".join(cycle_nodes))

    integrated = _validate_argv_list(
        plan.get("integrated_acceptance"), "integrated_acceptance"
    )
    if not writer_owners and integrated:
        _fail("integrated_acceptance requires at least one writer task")
    if writer_owners and (base_sha is None or ledger_slug is None):
        _fail("writer plans require base_sha and ledger_slug for write-ahead ownership")
    fingerprint = plan.get("config_fingerprint")
    if fingerprint is not None and (
        not isinstance(fingerprint, Mapping)
        or set(fingerprint) - {"digest", "provider_category"}
        or not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint.get("digest", "")))
        or fingerprint.get("provider_category") not in {"official", "proxy"}
    ):
        _fail(
            "config_fingerprint may contain only a SHA-256 digest and official|proxy category"
        )

    plan.update(
        {
            "repo_root": repo_root,
            "tasks": compiled_tasks,
            "budgets": {
                "total_concurrency": total,
                "writer_concurrency": writers,
                "family_concurrency": family_limits,
            },
            "integrated_acceptance": integrated,
            "topological_order": topo,
        }
    )
    return plan
