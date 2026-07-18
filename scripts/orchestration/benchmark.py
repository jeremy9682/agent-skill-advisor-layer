"""Offline preregistration and reporting for the orchestration A/B/C benchmark.

This module deliberately does not launch agents.  It freezes the protocol,
normalizes already-observed trial receipts, checks paired-block validity, and
evaluates the preregistered thresholds.  Live dispatch stays in the governed
CLI bridge so this file cannot become a second provider router.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import stat
import statistics
from collections import Counter, defaultdict
import datetime as dt
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Protocol


ARMS = ("A", "B", "C")
TASK_CLASSES = ("separable", "negative_control", "read_only")
FAILURE_CLASSES = {
    "none",
    "task-quality-failure",
    "orchestration-infrastructure-failure",
    "provider-environment-failure",
    "protocol-invalid",
    "failed-unsafe",
}
INVALID_TRIAL_REASONS = {
    "auth-failure-before-block",
    "base-drift",
    "config-drift",
    "corrupted-fixture",
    "host-outage-before-block",
    "operator-deviation",
    "provider-incident-before-block",
    "reviewer-drift",
    "wrong-acceptance",
    "wrong-prompt",
}
PRE_BLOCK_POSTPONE_REASONS = {
    "auth-failure",
    "config-unavailable",
    "host-unhealthy",
    "provider-evidence-missing",
    "provider-incident",
}
REVIEW_SLOW_ABSOLUTE_SECONDS = 300.0
REVIEW_SLOW_PRODUCER_FRACTION = 0.5
EVENT_NAMES = {
    "task_handoff",
    "graph_ready",
    "producer_started",
    "candidate_created",
    "acceptance_started",
    "acceptance_completed",
    "review_started",
    "review_completed",
    "rework_started",
    "rework_completed",
    "coordination_started",
    "coordination_completed",
    "trial_completed",
}
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "auth",
    "base_url",
    "cookie",
    "credential",
    "email",
    "endpoint",
    "password",
    "secret",
    "session",
    "token",
)
ATTESTED_EVIDENCE_VERSION = 2
ATTESTED_EVIDENCE_MAX_FRESHNESS_SECONDS = 3600.0
ATTESTED_EVIDENCE_DEFAULT_FUTURE_SKEW_SECONDS = 30.0
_ATTESTED_EVIDENCE_KEYS = frozenset(
    {
        "version",
        "observed_at",
        "expires_at",
        "freshness_window_seconds",
        "attested_by",
        "required_provider_families",
        "provider_families",
        "config_fingerprint",
        "provider_category",
        "host_identity",
        "checkout_identity",
        "route_policy_sha256",
    }
)
_ATTESTED_FAMILY_KEYS = frozenset(
    {
        "auth_ok",
        "host_healthy",
        "provider_incident",
    }
)
_ATTESTED_EVIDENCE_ALL_KEYS = _ATTESTED_EVIDENCE_KEYS | _ATTESTED_FAMILY_KEYS
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?:\b(?:api[_-]?key|authorization|bearer|cookie|credential|password|secret|session|token)\b|sk-[a-z0-9_-]{8,}|[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
    re.IGNORECASE,
)


class BenchmarkProtocolError(ValueError):
    """The preregistration or observed trial set violates the protocol."""


class LiveLauncherUnavailable(BenchmarkProtocolError):
    """The safe live process-lifecycle adapter has not been installed."""


class LiveBenchmarkAdapter(Protocol):
    """Narrow lifecycle boundary owned by the governed runtime.

    This protocol deliberately takes an already-compiled ``LaunchContract``.
    The benchmark never supplies provider/model/effort authority and the
    adapter must use the existing Agent Run/orchestrator route resolution.
    """

    def inspect_benchmark_live(
        self, protocol: Mapping[str, Any], *, evaluator_root: Path
    ) -> Mapping[str, Any]: ...

    def launch_benchmark_arm(
        self,
        contract: "LaunchContract",
        *,
        cell_root: Path,
        reviewer: Mapping[str, Any],
        block_id: str,
    ) -> Mapping[str, Any]: ...


class LaunchContract(NamedTuple):
    """An arm launch description with no provider-routing authority.

    ``payload`` is suitable for a fake launcher or for an independently
    reviewed lifecycle adapter.  It contains the current task only and never
    includes reserve/future tasks, hidden assertions, arm labels in producer
    prompts, or reviewer identity.
    """

    task_id: str
    arm: str
    launcher_kind: str
    payload: Mapping[str, Any]
    graph_sha256: str
    manual_runbook_sha256: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _is_nonplaceholder_sha256(value: Any) -> bool:
    """Recognise a digest that cannot be a repeated-character test placeholder."""

    text = str(value or "")
    return _is_sha256(text) and len(set(text)) > 1


def _is_full_git_commit(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 40 and all(char in "0123456789abcdef" for char in text) and len(set(text)) > 1


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BenchmarkProtocolError(message)


def _require_number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool),
        f"{label} must be numeric",
    )
    result = float(value)
    _require(math.isfinite(result) and result >= minimum, f"{label} is invalid")
    return result


def _sensitive_key(key: str) -> bool:
    folded = key.lower().replace("-", "_")
    return any(part in folded for part in SENSITIVE_KEY_PARTS)


def _contains_sensitive_material(value: Any) -> bool:
    """Reject credential-shaped evidence before it is returned to a caller."""

    if isinstance(value, Mapping):
        return any(
            (_sensitive_key(str(key)) and str(key) not in _ATTESTED_EVIDENCE_ALL_KEYS)
            or _contains_sensitive_material(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_material(item) for item in value)
    return isinstance(value, str) and bool(_SENSITIVE_VALUE_PATTERN.search(value))


def _parse_attested_timestamp(value: Any, label: str) -> dt.datetime:
    _require(isinstance(value, str) and value.endswith("Z"), f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BenchmarkProtocolError(f"{label} must be an RFC3339 UTC timestamp") from exc
    _require(parsed.tzinfo is not None, f"{label} must be timezone-aware")
    return parsed.astimezone(dt.timezone.utc)


def _frozen_route_policy_digest(protocol: Mapping[str, Any]) -> str:
    digests = {str(task.get("route_policy_sha256") or "") for task in protocol["tasks"]}
    _require(len(digests) == 1, "executable protocol must pin one route policy digest")
    digest = next(iter(digests))
    _require(_is_nonplaceholder_sha256(digest), "route_policy_sha256 must be a non-placeholder SHA-256")
    return digest


def validate_attested_evidence_bundle(
    raw: Mapping[str, Any],
    protocol: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
    expected_host_identity: str | None = None,
    expected_checkout_identity: str | None = None,
    expected_config_fingerprint: str | None = None,
    future_skew_seconds: float | None = None,
) -> dict[str, Any]:
    """Validate one credential-free, local-only live-pilot evidence bundle.

    This is intentionally a pure validator.  ``load_attested_evidence`` adds
    the file-system trust boundary.  Callers must supply the current host,
    checkout, and stripped-config fingerprints; omitting any of those bindings
    fails closed instead of allowing evidence to travel between runs.
    """

    frozen = validate_executable_protocol(protocol)
    _require(isinstance(raw, Mapping), "attested evidence must be an object")
    _require(not _contains_sensitive_material(raw), "attested evidence contains sensitive key or value")
    _require(set(raw) == _ATTESTED_EVIDENCE_KEYS, "attested evidence has unexpected or missing fields")
    preflight_policy = frozen["provider_preflight_policy"]
    _require(
        raw.get("version") == preflight_policy["evidence_schema_version"],
        "attested evidence version is unsupported",
    )
    _require(isinstance(raw.get("attested_by"), str) and raw["attested_by"].strip(), "attested evidence attested_by is required")
    _require(_is_sha256(raw.get("host_identity")), "host identity must be a SHA-256")
    _require(_is_sha256(raw.get("checkout_identity")), "checkout identity must be a SHA-256")
    _require(_is_sha256(raw.get("config_fingerprint")), "config fingerprint must be a SHA-256")
    _require(raw.get("provider_category") in {"official", "proxy"}, "provider category is invalid")
    _require(_is_nonplaceholder_sha256(raw.get("route_policy_sha256")), "route policy hash must be a non-placeholder SHA-256")

    current = now or dt.datetime.now(tz=dt.timezone.utc)
    _require(current.tzinfo is not None, "evidence validation clock must be timezone-aware")
    current = current.astimezone(dt.timezone.utc)
    frozen_skew = float(preflight_policy["future_skew_seconds"])
    if future_skew_seconds is None:
        future_skew_seconds = frozen_skew
    skew = _require_number(future_skew_seconds, "future_skew_seconds")
    _require(skew == frozen_skew, "future_skew_seconds must match frozen provider preflight policy")
    observed = _parse_attested_timestamp(raw.get("observed_at"), "observed_at")
    expires = _parse_attested_timestamp(raw.get("expires_at"), "expires_at")
    freshness = _require_number(raw.get("freshness_window_seconds"), "freshness_window_seconds", minimum=1)
    _require(
        freshness <= float(preflight_policy["max_freshness_seconds"]),
        "freshness_window_seconds exceeds maximum",
    )
    _require(expires > observed, "evidence expiry must follow observation")
    _require(abs((expires - observed).total_seconds() - freshness) < 1e-6, "evidence freshness window does not match expiry")
    _require(observed <= current + dt.timedelta(seconds=skew), "evidence observation is future-dated")
    _require(expires >= current, "attested evidence is expired")

    families = raw.get("required_provider_families")
    _require(isinstance(families, list) and families == frozen["required_provider_families"], "attested evidence provider families do not exactly match protocol")
    rows = raw.get("provider_families")
    _require(isinstance(rows, Mapping) and set(rows) == set(families), "attested evidence provider family set mismatch")
    preflight_evidence: dict[str, dict[str, Any]] = {}
    for family in families:
        row = rows[family]
        _require(isinstance(row, Mapping) and set(row) == _ATTESTED_FAMILY_KEYS, f"{family}: attested provider family schema is invalid")
        _require(row.get("auth_ok") is True, f"{family}: auth evidence is not healthy")
        _require(row.get("host_healthy") is True, f"{family}: host evidence is not healthy")
        _require(row.get("provider_incident") is False, f"{family}: provider incident is present")
        preflight_evidence[family] = dict(row)

    _require(isinstance(expected_host_identity, str) and _is_sha256(expected_host_identity), "expected host identity is required")
    _require(isinstance(expected_checkout_identity, str) and _is_sha256(expected_checkout_identity), "expected checkout identity is required")
    _require(isinstance(expected_config_fingerprint, str) and _is_sha256(expected_config_fingerprint), "expected config fingerprint is required")
    _require(raw["host_identity"] == expected_host_identity, "attested evidence host identity mismatch")
    _require(raw["checkout_identity"] == expected_checkout_identity, "attested evidence checkout identity mismatch")
    _require(raw["config_fingerprint"] == expected_config_fingerprint, "attested evidence config fingerprint mismatch")
    _require(raw["route_policy_sha256"] == _frozen_route_policy_digest(frozen), "attested evidence route policy drift")
    return {
        "observed_at": raw["observed_at"],
        "expires_at": raw["expires_at"],
        "attested_by": raw["attested_by"],
        "preflight_evidence": preflight_evidence,
        "config_fingerprint": raw["config_fingerprint"],
        "provider_category": raw["provider_category"],
        "host_identity": raw["host_identity"],
        "checkout_identity": raw["checkout_identity"],
        "route_policy_sha256": raw["route_policy_sha256"],
    }


def load_attested_evidence(
    path: Path | None,
    protocol: Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """Load only a strict mode-0600 regular evidence file, then validate it."""

    _require(path is not None, "attested evidence is unavailable")
    candidate = path.expanduser()
    _require(not candidate.is_symlink(), "attested evidence must not be a symlink")
    try:
        fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            _require(stat.S_ISREG(metadata.st_mode), "attested evidence must be a regular file")
            _require(stat.S_IMODE(metadata.st_mode) == 0o600, "attested evidence must have exact mode 0600")
            raw = json.load(handle)
    except BenchmarkProtocolError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkProtocolError("attested evidence is invalid JSON") from exc
    return validate_attested_evidence_bundle(raw, protocol, **kwargs)


def attested_pre_block_gate(
    path: Path | None,
    protocol: Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """Reload and gate evidence for exactly one paired block.

    Call this immediately before every block.  It deliberately performs no
    caching: a bundle that expires between two blocks is rejected on the next
    call rather than being carried forward from initial preflight.
    """

    evidence = load_attested_evidence(path, protocol, **kwargs)
    gate = pre_block_gate(protocol, evidence["preflight_evidence"])
    _require(gate["eligible"], "attested evidence did not satisfy paired-block gate")
    return {"evidence": evidence, "pre_block_gate": gate}


def credential_stripped_config(value: Any) -> Any:
    """Return a canonical, credential-free structural projection.

    Values under sensitive keys are removed rather than masked so callers can
    never reconstruct endpoint, account, or credential material from the
    benchmark artifact.
    """

    if isinstance(value, Mapping):
        return {
            str(key): credential_stripped_config(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not _sensitive_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [credential_stripped_config(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(type(value).__name__)


def config_fingerprint(config: Mapping[str, Any], provider_category: str) -> dict[str, str]:
    _require(
        provider_category in {"official", "proxy"},
        "provider_category must be official or proxy",
    )
    stripped = credential_stripped_config(config)
    return {
        "sha256": sha256_value(stripped),
        "provider_category": provider_category,
    }


def expected_thresholds() -> dict[str, float]:
    return {
        "h1_c_vs_b_min_speedup": 1.20,
        "h2_c_vs_a_separable_min_speedup": 1.30,
        "h2_quality_min_extra_first_pass": 2,
        "h2_quality_max_time_penalty": 0.10,
        "negative_control_max_overhead": 0.10,
        "max_first_pass_drop_tasks": 1,
        "max_c_agent_minutes_ratio_median": 2.20,
        "max_c_agent_minutes_ratio_per_task": 3.00,
    }


def expected_invalid_trial_rules() -> dict[str, Any]:
    return {
        "replacement_max_per_task": 1,
        "allowed_reasons": sorted(INVALID_TRIAL_REASONS),
        "near_threshold_repeat_allowed": False,
        "original_retained": True,
        "inside_block_rate_limit_is_treatment_outcome": True,
    }


def expected_review_warning_rule() -> dict[str, float]:
    return {
        "absolute_seconds": REVIEW_SLOW_ABSOLUTE_SECONDS,
        "producer_fraction": REVIEW_SLOW_PRODUCER_FRACTION,
    }


def expected_provider_preflight_policy() -> dict[str, Any]:
    """Return the exact quota-independent policy frozen by preregistration."""

    return {
        "mode": "auth-host-incident-v1",
        "quota_monitoring": False,
        "inside_block_rate_limit": "treatment-outcome",
        "evidence_schema_version": ATTESTED_EVIDENCE_VERSION,
        "max_freshness_seconds": int(ATTESTED_EVIDENCE_MAX_FRESHNESS_SECONDS),
        "future_skew_seconds": ATTESTED_EVIDENCE_DEFAULT_FUTURE_SKEW_SECONDS,
    }


def _validate_execution_protocol(protocol: Mapping[str, Any]) -> None:
    """Validate fields needed before a frozen protocol can execute.

    ``validate_protocol`` remains useful for normalizing historical fixtures;
    preregistration and launch always call this stricter layer.
    """

    _require(
        protocol.get("arm_contract_version") == 1,
        "arm_contract_version must be 1",
    )
    _require(
        protocol.get("arm_order_strategy")
        == "seeded-balanced-latin-square-v1",
        "arm_order_strategy must be seeded-balanced-latin-square-v1",
    )
    _require(
        protocol.get("invalid_trial_rules") == expected_invalid_trial_rules(),
        "invalid_trial_rules must exactly match the frozen V1 rules",
    )
    _require(
        protocol.get("review_warning_rule") == expected_review_warning_rule(),
        "review_warning_rule must exactly match the frozen V1 rule",
    )
    families = protocol.get("required_provider_families")
    _require(
        isinstance(families, list)
        and families
        and all(isinstance(item, str) and item for item in families)
        and len(families) == len(set(families)),
        "required_provider_families must be unique non-empty strings",
    )
    _require(
        protocol.get("provider_preflight_policy") == expected_provider_preflight_policy(),
        "provider_preflight_policy must exactly match the frozen quota-independent policy",
    )
    reserves = protocol.get("reserve_tasks")
    _require(
        isinstance(reserves, list) and reserves,
        "reserve_tasks must be frozen before preregistration",
    )
    reserve_ids: set[str] = set()
    reserve_classes: Counter[str] = Counter()
    active_ids = {str(task["task_id"]) for task in protocol["tasks"]}
    for reserve in reserves:
        _require(isinstance(reserve, Mapping), "reserve task must be an object")
        reserve_id = reserve.get("task_id")
        _require(
            isinstance(reserve_id, str) and reserve_id,
            "reserve task_id is required",
        )
        _require(
            reserve_id not in active_ids and reserve_id not in reserve_ids,
            "reserve task_id must be unique across active and reserve tasks",
        )
        reserve_ids.add(reserve_id)
        task_class = reserve.get("task_class")
        _require(task_class in TASK_CLASSES, f"{reserve_id}: invalid reserve task_class")
        reserve_classes[str(task_class)] += 1
        for field in (
            "private_task_sha256",
            "intent_sha256",
            "prompt_sha256",
            "manual_runbook_sha256",
            "graph_sha256",
        ):
            _require(_is_sha256(reserve.get(field)), f"{reserve_id}: {field} must be SHA-256")
    _require(
        set(reserve_classes) == set(TASK_CLASSES),
        "reserve_tasks must include every task class",
    )
    for task in protocol["tasks"]:
        task_id = str(task["task_id"])
        _require(
            _is_full_git_commit(task.get("base_commit")),
            f"{task_id}: base_commit must be a full non-placeholder Git SHA-1",
        )
        _require(
            _is_nonplaceholder_sha256(task.get("route_policy_sha256")),
            f"{task_id}: route_policy_sha256 must be a non-placeholder SHA-256",
        )
        _require(
            _is_sha256(task.get("private_task_sha256")),
            f"{task_id}: private_task_sha256 must be SHA-256",
        )
        _require(
            isinstance(task.get("single_producer_task_shape"), str)
            and bool(task["single_producer_task_shape"]),
            f"{task_id}: single_producer_task_shape is required",
        )


def validate_executable_protocol(raw: Mapping[str, Any]) -> dict[str, Any]:
    protocol = validate_protocol(raw)
    _validate_execution_protocol(protocol)
    return protocol


def validate_protocol(raw: Mapping[str, Any], *, strict_counts: bool = True) -> dict[str, Any]:
    _require(isinstance(raw, Mapping), "benchmark protocol must be an object")
    protocol = json.loads(json.dumps(raw))
    _require(protocol.get("version") == 1, "benchmark protocol version must be 1")
    stage = protocol.get("stage")
    _require(stage in {"pilot", "confirmation"}, "stage must be pilot or confirmation")
    _require(
        isinstance(protocol.get("order_seed"), int)
        and not isinstance(protocol.get("order_seed"), bool),
        "order_seed must be an integer",
    )
    _require(
        _is_sha256(protocol.get("hidden_manifest_sha256")),
        "hidden_manifest_sha256 must be a lowercase SHA-256",
    )
    _require(
        protocol.get("thresholds") == expected_thresholds(),
        "thresholds must exactly match the preregistered V1 constants",
    )

    _require("quota_rules" not in protocol, "quota_rules are not supported by the quota-independent pilot")

    tasks = protocol.get("tasks")
    _require(isinstance(tasks, list) and tasks, "tasks must be a non-empty list")
    ids = [str(task.get("task_id") or "") for task in tasks if isinstance(task, Mapping)]
    _require(len(ids) == len(tasks) and all(ids), "every task needs task_id")
    _require(len(ids) == len(set(ids)), "task_id values must be unique")
    counts = Counter(str(task.get("task_class")) for task in tasks)
    if strict_counts:
        expected = (
            {"separable": 1, "negative_control": 1, "read_only": 1}
            if stage == "pilot"
            else {"separable": 6, "negative_control": 3, "read_only": 3}
        )
        _require(counts == expected, f"{stage} task-class counts must be {expected}")

    for task in tasks:
        _require(isinstance(task, Mapping), "task entry must be an object")
        task_id = str(task["task_id"])
        _require(task.get("task_class") in TASK_CLASSES, f"{task_id}: invalid task_class")
        _require(
            isinstance(task.get("base_commit"), str) and len(task["base_commit"]) >= 7,
            f"{task_id}: base_commit is required",
        )
        for field in (
            "intent_sha256",
            "prompt_sha256",
            "route_policy_sha256",
            "manual_runbook_sha256",
            "graph_sha256",
        ):
            _require(_is_sha256(task.get(field)), f"{task_id}: {field} must be SHA-256")
        commands = task.get("acceptance_commands")
        _require(
            isinstance(commands, list)
            and commands
            and all(isinstance(command, str) and command.strip() for command in commands),
            f"{task_id}: acceptance_commands must be non-empty strings",
        )
        _require_number(task.get("deadline_seconds"), f"{task_id}.deadline_seconds", minimum=1)
        writer_limit = _require_number(task.get("writer_limit"), f"{task_id}.writer_limit", minimum=1)
        _require(writer_limit in {1.0, 2.0}, f"{task_id}: writer_limit must be 1 or 2")
        if task.get("task_class") == "negative_control":
            _require(writer_limit == 1.0, f"{task_id}: negative control must use one writer")
        families = task.get("producer_families")
        _require(
            isinstance(families, list)
            and families
            and all(isinstance(item, str) and item for item in families),
            f"{task_id}: producer_families are required",
        )
        reviewer = task.get("reviewer")
        _require(isinstance(reviewer, Mapping), f"{task_id}: reviewer is required")
        for field in ("route", "model", "effort", "family", "independence"):
            _require(
                isinstance(reviewer.get(field), str) and reviewer[field],
                f"{task_id}: reviewer.{field} is required",
            )
        _require(
            reviewer.get("independence") in {"cross_family", "same_family_independent"},
            f"{task_id}: reviewer independence is invalid",
        )
        _require(_is_sha256(reviewer.get("prompt_sha256")), f"{task_id}: reviewer prompt hash required")
        _require_number(reviewer.get("timeout_seconds"), f"{task_id}.reviewer.timeout_seconds", minimum=1)
        _require(
            reviewer.get("family") not in set(families),
            f"{task_id}: reviewer family cannot be a producer family",
        )
    return protocol


def write_private_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = canonical_json(value) + "\n"
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, mode)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _require_private_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    _require(root.is_dir(), "evaluator root must be an existing directory")
    _require(
        stat.S_IMODE(root.stat().st_mode) == 0o700,
        "evaluator root must have exact mode 0700",
    )
    return root


def verify_evaluator_root(protocol: Mapping[str, Any], evaluator_root: Path) -> dict[str, Any]:
    """Verify the private manifest without exposing task bodies.

    The manifest and task files must be ordinary, non-symlink files below an
    explicit mode-0700 root.  Only hashes and relative paths are returned.
    """

    frozen = validate_executable_protocol(protocol)
    root = _require_private_root(evaluator_root)
    manifest_path = root / "private-manifest.json"
    _require(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "private evaluator manifest is unavailable",
    )
    raw_bytes = manifest_path.read_bytes()
    _require(
        hashlib.sha256(raw_bytes).hexdigest() == frozen["hidden_manifest_sha256"],
        "private evaluator manifest hash mismatch",
    )
    try:
        manifest = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkProtocolError("private evaluator manifest is invalid JSON") from exc
    _require(
        isinstance(manifest, Mapping) and manifest.get("version") == 1,
        "private evaluator manifest version must be 1",
    )
    entries = manifest.get("tasks")
    _require(isinstance(entries, Mapping), "private evaluator manifest tasks are required")
    expected = {
        str(task["task_id"]): str(task["private_task_sha256"])
        for task in [*frozen["tasks"], *frozen["reserve_tasks"]]
    }
    _require(set(entries) == set(expected), "private evaluator task set mismatch")
    verified: dict[str, dict[str, str]] = {}
    for task_id, digest in expected.items():
        entry = entries[task_id]
        _require(isinstance(entry, Mapping), f"{task_id}: private manifest entry is invalid")
        relative = entry.get("path")
        _require(
            isinstance(relative, str)
            and relative == f"tasks/{task_id}.json",
            f"{task_id}: private task path is not canonical",
        )
        _require(entry.get("sha256") == digest, f"{task_id}: private task digest drift")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise BenchmarkProtocolError(f"{task_id}: private task escaped evaluator root") from exc
        _require(candidate.is_file() and not candidate.is_symlink(), f"{task_id}: private task unavailable")
        _require(
            hashlib.sha256(candidate.read_bytes()).hexdigest() == digest,
            f"{task_id}: private task file hash mismatch",
        )
        verified[task_id] = {"path": relative, "sha256": digest}
    return {"manifest_sha256": frozen["hidden_manifest_sha256"], "tasks": verified}


def _public_task(protocol: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
    for task in [*protocol["tasks"], *protocol.get("reserve_tasks", [])]:
        if task.get("task_id") == task_id:
            return task
    raise BenchmarkProtocolError(f"unknown benchmark task: {task_id}")


def load_private_task(
    protocol: Mapping[str, Any], evaluator_root: Path, task_id: str
) -> dict[str, Any]:
    """Load one verified private task, never future or reserve siblings."""

    frozen = validate_executable_protocol(protocol)
    verified = verify_evaluator_root(frozen, evaluator_root)
    public = _public_task(frozen, task_id)
    entry = verified["tasks"][task_id]
    candidate = evaluator_root.expanduser().resolve() / entry["path"]
    private = json.loads(candidate.read_text(encoding="utf-8"))
    _require(isinstance(private, Mapping), f"{task_id}: private task must be an object")
    _require(private.get("version") == 1 and private.get("task_id") == task_id, f"{task_id}: private identity mismatch")
    for field, expected_field in (
        ("intent", "intent_sha256"),
        ("task_input", "prompt_sha256"),
        ("graph", "graph_sha256"),
        ("manual_runbook", "manual_runbook_sha256"),
    ):
        _require(
            sha256_value(private.get(field)) == public[expected_field],
            f"{task_id}: private {field} hash mismatch",
        )
    hidden = private.get("hidden_assertions")
    _require(isinstance(hidden, list), f"{task_id}: hidden_assertions must be a list")
    return dict(private)


def counterbalanced_order(protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    frozen = validate_executable_protocol(protocol)
    task_ids = [str(task["task_id"]) for task in frozen["tasks"]]
    random.Random(int(frozen["order_seed"])).shuffle(task_ids)
    latin = (ARMS, ("B", "C", "A"), ("C", "A", "B"))
    output: list[dict[str, Any]] = []
    for block_index, task_id in enumerate(task_ids):
        for position, arm in enumerate(latin[block_index % len(latin)]):
            output.append(
                {
                    "block_index": block_index,
                    "task_id": task_id,
                    "position": position,
                    "arm": arm,
                }
            )
    return output


def pre_block_gate(
    protocol: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Decide whether an entire paired block may start.

    Missing evidence or an unhealthy provider postpones all arms. This function
    is never called after an arm has started; inside-block rate limits remain
    observed treatment outcomes and are not a launch gate.
    """

    frozen = validate_executable_protocol(protocol)
    reasons: list[dict[str, str]] = []
    for family in frozen["required_provider_families"]:
        row = evidence.get(family)
        if not isinstance(row, Mapping):
            reasons.append({"provider_family": family, "reason": "provider-evidence-missing"})
            continue
        if row.get("auth_ok") is not True:
            reasons.append({"provider_family": family, "reason": "auth-failure"})
        if row.get("host_healthy") is not True:
            reasons.append({"provider_family": family, "reason": "host-unhealthy"})
        if row.get("provider_incident") is not False:
            reasons.append({"provider_family": family, "reason": "provider-incident"})
    deduped = sorted(
        {tuple(sorted(item.items())) for item in reasons},
        key=lambda item: tuple(item),
    )
    normalized = [dict(item) for item in deduped]
    return {
        "eligible": not normalized,
        "action": "start-whole-block" if not normalized else "postpone-whole-block",
        "reasons": normalized,
    }


def review_slowness_warning(review_seconds: float, producer_seconds: float) -> dict[str, Any]:
    review = _require_number(review_seconds, "review_seconds")
    producer = _require_number(producer_seconds, "producer_seconds")
    threshold = min(REVIEW_SLOW_ABSOLUTE_SECONDS, REVIEW_SLOW_PRODUCER_FRACTION * producer)
    return {
        "warning": review > threshold,
        "threshold_seconds": threshold,
        "review_seconds": review,
        "invalidates_trial": False,
    }


_PRODUCER_FORBIDDEN_KEYS = {
    "arm",
    "future_task",
    "future_tasks",
    "hidden_assertions",
    "reserve",
    "reserve_tasks",
    "reviewer",
    "reviewer_identity",
    "provider",
    "model",
    "effort",
    "seat",
    "permission_profile",
}


def _reject_private_authority(value: Any, where: str = "producer payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            _require(
                normalized not in _PRODUCER_FORBIDDEN_KEYS,
                f"{where} contains forbidden key {key}",
            )
            _reject_private_authority(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_private_authority(child, f"{where}[{index}]")


def build_launch_contract(
    protocol: Mapping[str, Any], evaluator_root: Path, task_id: str, arm: str
) -> LaunchContract:
    """Build a route-name-only contract for one arm and one current task."""

    _require(arm in ARMS, "arm must be A, B, or C")
    frozen = validate_executable_protocol(protocol)
    public = _public_task(frozen, task_id)
    _require(
        task_id in {str(task["task_id"]) for task in frozen["tasks"]},
        "reserve tasks cannot be launched before a declared replacement",
    )
    private = load_private_task(frozen, evaluator_root, task_id)
    task_input = private["task_input"]
    _require(
        isinstance(task_input, str) and task_input.strip(),
        f"{task_id}: task_input must be non-empty text",
    )
    common = {
        "task_id": task_id,
        "base_commit": public["base_commit"],
        "intent_sha256": public["intent_sha256"],
        "prompt_sha256": public["prompt_sha256"],
        "route_policy_sha256": public["route_policy_sha256"],
        "acceptance_commands": public["acceptance_commands"],
        "deadline_seconds": public["deadline_seconds"],
        "task_input": task_input,
    }
    if arm == "A":
        payload: dict[str, Any] = {
            **common,
            "task_shape": public["single_producer_task_shape"],
            "concurrency": 1,
            "launcher": "governed-agent-run-auto",
        }
        launcher_kind = "single-native-producer"
    else:
        graph = private["graph"]
        runbook = private["manual_runbook"]
        _require(isinstance(graph, Mapping), f"{task_id}: graph must be an object")
        _require(isinstance(runbook, Mapping), f"{task_id}: manual_runbook must be an object")
        _reject_private_authority(graph, "private graph")
        _reject_private_authority(runbook, "manual runbook")
        nodes = graph.get("nodes")
        _require(isinstance(nodes, list) and nodes, f"{task_id}: graph nodes are required")
        payload = {
            **common,
            "graph": graph,
            "writer_limit": public["writer_limit"],
        }
        if arm == "B":
            payload["manual_runbook"] = runbook
            payload["launcher"] = "manual-event-driven-governed-agent-run"
            launcher_kind = "manual-event-fanout"
        else:
            payload["launcher"] = "agent-orchestrate"
            payload["orchestration_cli"] = "scripts/agent_orchestrate.py"
            launcher_kind = "automatic-orchestrator"
    _reject_private_authority(payload)
    return LaunchContract(
        task_id=task_id,
        arm=arm,
        launcher_kind=launcher_kind,
        payload=payload,
        graph_sha256=str(public["graph_sha256"]),
        manual_runbook_sha256=str(public["manual_runbook_sha256"]),
    )


def compile_governed_lifecycle(
    protocol: Mapping[str, Any],
    evaluator_root: Path,
    contract: LaunchContract,
    *,
    reviewer: Mapping[str, Any],
    cell_root: Path,
) -> Any:
    """Translate one verified benchmark contract through the plan compiler.

    Kept here as the public benchmark-to-runtime seam so the CLI and runtime
    adapter share the exact verifier.  The import is local to avoid a module
    cycle: ``benchmark_lifecycle`` deliberately reuses this module's frozen
    hash and contract definitions.
    """

    expected = build_launch_contract(protocol, evaluator_root, contract.task_id, contract.arm)
    _require(expected == contract, "launch contract drift before lifecycle compilation")
    private = load_private_task(protocol, evaluator_root, contract.task_id)
    from .benchmark_lifecycle import compile_lifecycle_launch

    return compile_lifecycle_launch(contract, private, reviewer=reviewer, cell_root=cell_root)


def preregister(protocol: Mapping[str, Any], output_path: Path) -> dict[str, Any]:
    normalized = validate_executable_protocol(protocol)
    envelope = {
        "protocol": normalized,
        "protocol_sha256": sha256_value(normalized),
        "frozen": True,
    }
    write_private_json(output_path, envelope)
    return envelope


def normalize_trial(raw: Mapping[str, Any]) -> dict[str, Any]:
    _require(isinstance(raw, Mapping), "trial must be an object")
    trial = json.loads(json.dumps(raw))
    for field in ("task_id", "arm", "block_id", "failure_class", "config_fingerprint"):
        _require(isinstance(trial.get(field), str) and trial[field], f"trial.{field} is required")
    _require(trial["arm"] in ARMS, "trial.arm must be A, B, or C")
    _require(trial["failure_class"] in FAILURE_CLASSES, "trial.failure_class is invalid")
    for field in (
        "time_to_accepted_seconds",
        "producer_seconds",
        "review_seconds",
        "agent_minutes",
        "context_construction_ms",
        "delivered_prompt_bytes",
        "coordination_seconds",
    ):
        trial[field] = _require_number(trial.get(field, 0), f"trial.{field}")
    for field in (
        "accepted",
        "first_pass_accepted",
        "attribution_complete",
        "scope_violation",
    ):
        _require(isinstance(trial.get(field), bool), f"trial.{field} must be boolean")
    for field in ("rework_rounds", "review_severity_points", "unresolved_disputes"):
        value = _require_number(trial.get(field, 0), f"trial.{field}")
        _require(value.is_integer(), f"trial.{field} must be an integer")
        trial[field] = int(value)
    trial["task_quality_failure"] = trial["failure_class"] == "task-quality-failure"
    trial["infrastructure_failure"] = (
        trial["failure_class"] == "orchestration-infrastructure-failure"
    )
    trial["unsafe"] = trial["failure_class"] == "failed-unsafe"
    replacement_of = trial.get("replacement_of")
    _require(
        replacement_of is None or isinstance(replacement_of, str),
        "trial.replacement_of must be a string or null",
    )
    invalid_reason = trial.get("invalid_reason")
    _require(
        invalid_reason is None or invalid_reason in INVALID_TRIAL_REASONS,
        "trial.invalid_reason is not predeclared",
    )
    if replacement_of is not None:
        _require(bool(invalid_reason), "replacement trial requires invalid_reason")
    artifact_sha = trial.get("artifact_sha256")
    _require(
        artifact_sha is None or _is_sha256(artifact_sha),
        "trial.artifact_sha256 must be SHA-256 when present",
    )
    return trial


def normalize_trials(trials: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [normalize_trial(trial) for trial in trials]
    keys = [(trial["task_id"], trial["arm"]) for trial in normalized]
    _require(len(keys) == len(set(keys)), "duplicate task/arm trial is not allowed")
    return normalized


def _event_time(raw: Any) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise BenchmarkProtocolError("event timestamp is invalid") from exc
    else:
        raise BenchmarkProtocolError("event timestamp is required")
    _require(math.isfinite(value) and value >= 0, "event timestamp is invalid")
    return value


def _artifact_digest(paths: Iterable[Path]) -> tuple[str, list[dict[str, Any]]]:
    entries = []
    for path in sorted((item.expanduser().resolve() for item in paths), key=str):
        _require(path.is_file() and not path.is_symlink(), "trial artifact must be a regular file")
        payload = path.read_bytes()
        entries.append(
            {"name": path.name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        )
    _require(bool(entries), "at least one trial artifact is required")
    return sha256_value(entries), entries


def derive_trial_receipt(
    contract: LaunchContract,
    events: Iterable[Mapping[str, Any]],
    *,
    block_id: str,
    config_fingerprint_value: str,
    artifact_paths: Iterable[Path],
) -> dict[str, Any]:
    """Derive metrics from immutable events rather than operator diaries."""

    _require(_is_sha256(config_fingerprint_value), "config fingerprint must be SHA-256")
    rows = [dict(event) for event in events]
    _require(rows, "trial events are required")
    previous = -1.0
    for row in rows:
        _require(row.get("event") in EVENT_NAMES, "unknown benchmark event")
        observed = _event_time(row.get("at"))
        _require(observed >= previous, "trial events must be time ordered")
        previous = observed
        row["_at"] = observed
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_name[str(row["event"])].append(row)
    for name in (
        "task_handoff",
        "candidate_created",
        "acceptance_completed",
        "review_started",
        "review_completed",
        "trial_completed",
    ):
        _require(by_name[name], f"trial event {name} is required")
    handoff = by_name["task_handoff"][0]["_at"]
    candidate = by_name["candidate_created"][0]["_at"]
    trial_end = by_name["trial_completed"][-1]["_at"]
    accepted_events = [
        row for row in by_name["acceptance_completed"] if row.get("accepted") is True
    ]
    accepted = bool(accepted_events) and by_name["trial_completed"][-1].get("accepted") is True
    rework = len(by_name["rework_started"])
    first_acceptance = by_name["acceptance_completed"][0]
    first_pass = first_acceptance.get("accepted") is True and rework == 0
    review_seconds = 0.0
    review_pairs = zip(by_name["review_started"], by_name["review_completed"], strict=False)
    for start, end in review_pairs:
        _require(end["_at"] >= start["_at"], "review interval is negative")
        review_seconds += end["_at"] - start["_at"]
    starts: dict[str, float] = {}
    coordination_seconds = 0.0
    for row in rows:
        if row["event"] == "coordination_started":
            interval = row.get("interval_id")
            _require(isinstance(interval, str) and interval not in starts, "coordination interval start is invalid")
            starts[interval] = row["_at"]
        elif row["event"] == "coordination_completed":
            interval = row.get("interval_id")
            _require(isinstance(interval, str) and interval in starts, "coordination interval end has no start")
            coordination_seconds += row["_at"] - starts.pop(interval)
    _require(not starts, "coordination interval is incomplete")
    terminal = by_name["trial_completed"][-1]
    attributions = terminal.get("attributions", [])
    _require(isinstance(attributions, list), "terminal attributions must be a list")
    attribution_complete = bool(attributions) and all(
        isinstance(item, Mapping)
        and bool(item.get("run_id"))
        and bool(item.get("model"))
        and bool(item.get("session_id"))
        for item in attributions
    )
    duration_sum = sum(
        float(item.get("duration_seconds", 0))
        for item in attributions
        if isinstance(item, Mapping)
        and isinstance(item.get("duration_seconds", 0), (int, float))
    )
    artifact_sha, artifacts = _artifact_digest(artifact_paths)
    producer_seconds = max(0.0, candidate - handoff)
    warning = review_slowness_warning(review_seconds, producer_seconds)
    failure_class = str(terminal.get("failure_class") or ("none" if accepted else "task-quality-failure"))
    receipt = {
        "task_id": contract.task_id,
        "arm": contract.arm,
        "block_id": block_id,
        "failure_class": failure_class,
        "config_fingerprint": config_fingerprint_value,
        "time_to_accepted_seconds": max(0.0, trial_end - handoff),
        "producer_seconds": producer_seconds,
        "review_seconds": review_seconds,
        "agent_minutes": duration_sum / 60.0,
        "context_construction_ms": int(terminal.get("context_construction_ms", 0)),
        "delivered_prompt_bytes": int(terminal.get("delivered_prompt_bytes", 0)),
        "coordination_seconds": coordination_seconds,
        "accepted": accepted,
        "first_pass_accepted": first_pass,
        "attribution_complete": attribution_complete,
        "scope_violation": terminal.get("scope_violation") is True,
        "rework_rounds": rework,
        "review_severity_points": int(terminal.get("review_severity_points", 0)),
        "unresolved_disputes": int(terminal.get("unresolved_disputes", 0)),
        "artifact_sha256": artifact_sha,
        "artifacts": artifacts,
        "review_slowness_warning": warning["warning"],
        "review_warning_threshold_seconds": warning["threshold_seconds"],
        "event_count": len(rows),
    }
    return normalize_trial(receipt)


def validate_paired_blocks(
    protocol: Mapping[str, Any], trials: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    frozen = validate_protocol(protocol)
    normalized = normalize_trials(trials)
    task_ids = {task["task_id"] for task in frozen["tasks"]}
    _require(
        {trial["task_id"] for trial in normalized} <= task_ids,
        "trial references an unknown task",
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trial in normalized:
        grouped[trial["task_id"]].append(trial)
    for task_id, rows in grouped.items():
        _require({row["arm"] for row in rows} == set(ARMS), f"{task_id}: paired block needs A/B/C")
        _require(len({row["block_id"] for row in rows}) == 1, f"{task_id}: block_id drift")
        if len({row["config_fingerprint"] for row in rows}) != 1:
            for row in rows:
                row["failure_class"] = "protocol-invalid"
        if any(row["failure_class"] == "protocol-invalid" for row in rows):
            for row in rows:
                row["failure_class"] = "protocol-invalid"
    return normalized


def _median(values: Iterable[float]) -> float:
    rows = list(values)
    return statistics.median(rows) if rows else math.nan


def _ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return math.inf if numerator > 0 else 1.0
    return numerator / denominator


def evaluate_confirmation(
    protocol: Mapping[str, Any], trials: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    frozen = validate_protocol(protocol)
    _require(frozen["stage"] == "confirmation", "confirmation evaluation needs confirmation protocol")
    rows = validate_paired_blocks(frozen, trials)
    _require(len(rows) == 36, "confirmation requires 36 original A/B/C trials")
    by_key = {(row["task_id"], row["arm"]): row for row in rows}
    tasks = {task["task_id"]: task for task in frozen["tasks"]}
    invalid_tasks = sorted(
        task_id
        for task_id in tasks
        if any(by_key[(task_id, arm)]["failure_class"] == "protocol-invalid" for arm in ARMS)
    )
    unsafe = [row for row in rows if row["unsafe"] or row["scope_violation"]]
    attribution_incomplete = [row for row in rows if not row["attribution_complete"]]
    c_infra = sum(
        1 for row in rows if row["arm"] == "C" and row["infrastructure_failure"]
    )

    valid_task_ids = [task_id for task_id in tasks if task_id not in invalid_tasks]
    inclusive = {
        arm: _median(by_key[(task_id, arm)]["time_to_accepted_seconds"] for task_id in valid_task_ids)
        for arm in ARMS
    }
    h1_speedup = _ratio(inclusive["B"], inclusive["C"])
    failures = {
        arm: sum(
            int(by_key[(task_id, arm)]["task_quality_failure"])
            for task_id in valid_task_ids
        )
        for arm in ARMS
    }
    first_pass = {
        arm: sum(
            int(by_key[(task_id, arm)]["first_pass_accepted"])
            for task_id in valid_task_ids
        )
        for arm in ARMS
    }
    h1 = (
        h1_speedup >= 1.20
        and failures["C"] <= failures["B"]
        and first_pass["C"] >= first_pass["B"] - 1
    )

    separable = [
        task_id for task_id in valid_task_ids if tasks[task_id]["task_class"] == "separable"
    ]
    negative = [
        task_id
        for task_id in valid_task_ids
        if tasks[task_id]["task_class"] == "negative_control"
    ]
    sep_speedup = _median(
        _ratio(
            by_key[(task_id, "A")]["time_to_accepted_seconds"],
            by_key[(task_id, "C")]["time_to_accepted_seconds"],
        )
        for task_id in separable
    )
    h2_speed = (
        sep_speedup >= 1.30
        and failures["C"] <= failures["A"]
        and first_pass["C"] >= first_pass["A"] - 1
    )
    c_vs_a_penalty = _ratio(inclusive["C"], inclusive["A"]) - 1.0
    h2_quality = (
        first_pass["C"] >= first_pass["A"] + 2
        and failures["C"] <= failures["A"]
        and c_vs_a_penalty <= 0.10
    )
    negative_overhead = _median(
        _ratio(
            by_key[(task_id, "C")]["time_to_accepted_seconds"],
            by_key[(task_id, "A")]["time_to_accepted_seconds"],
        )
        - 1.0
        for task_id in negative
    )
    agent_ratios = [
        _ratio(
            by_key[(task_id, "C")]["agent_minutes"],
            by_key[(task_id, "A")]["agent_minutes"],
        )
        for task_id in separable
    ]
    agent_budget = bool(agent_ratios) and _median(agent_ratios) <= 2.20 and all(
        ratio <= 3.00 for ratio in agent_ratios
    )
    safety = not unsafe and not attribution_incomplete
    negative_control_gate = not math.isnan(negative_overhead) and negative_overhead <= 0.10
    h2 = (h2_speed or h2_quality) and negative_control_gate and agent_budget

    if invalid_tasks:
        decision = "inconclusive-protocol-invalid"
    elif not safety or c_infra >= 2:
        decision = "fail"
    elif c_infra == 1:
        decision = "blocked-pending-new-confirmation"
    elif h1 and h2:
        decision = "enable-passing-task-shapes"
    elif h1:
        decision = "manual-fanout-control-plane-only"
    elif h2:
        decision = "redesign-control-plane"
    else:
        decision = "keep-multi-producer-opt-in"

    return {
        "protocol_sha256": sha256_value(frozen),
        "raw_trial_count": len(rows),
        "invalid_tasks": invalid_tasks,
        "unsafe_trial_count": len(unsafe),
        "attribution_incomplete_count": len(attribution_incomplete),
        "c_infrastructure_failures": c_infra,
        "inclusive_median_seconds": inclusive,
        "task_quality_failures": failures,
        "first_pass_acceptances": first_pass,
        "h1_speedup_c_vs_b": h1_speedup,
        "h1_pass": h1,
        "h2_separable_speedup_c_vs_a": sep_speedup,
        "h2_speed_path_pass": h2_speed,
        "h2_quality_path_pass": h2_quality,
        "negative_control_overhead": negative_overhead,
        "agent_minutes_ratio_median": _median(agent_ratios),
        "h2_pass": h2,
        "provider_environment_failures": sum(
            1 for row in rows if row["failure_class"] == "provider-environment-failure"
        ),
        "orchestration_infrastructure_failures": sum(
            1 for row in rows if row["failure_class"] == "orchestration-infrastructure-failure"
        ),
        "slow_review_warnings": sum(
            1 for row in rows if row.get("review_slowness_warning") is True
        ),
        "decision": decision,
    }


def evaluate_pilot(
    protocol: Mapping[str, Any], trials: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    frozen = validate_executable_protocol(protocol)
    _require(frozen["stage"] == "pilot", "pilot evaluation needs pilot protocol")
    rows = validate_paired_blocks(frozen, trials)
    _require(len(rows) == 9, "pilot requires nine original A/B/C trials")
    unsafe = [row for row in rows if row["unsafe"] or row["scope_violation"]]
    attribution = [row for row in rows if not row["attribution_complete"]]
    invalid = [row for row in rows if row["failure_class"] == "protocol-invalid"]
    infrastructure = [
        row for row in rows if row["failure_class"] == "orchestration-infrastructure-failure"
    ]
    decision = "pilot-ready-for-separate-confirmation-approval"
    if unsafe:
        decision = "pilot-failed-unsafe"
    elif invalid or attribution:
        decision = "pilot-invalid-repair-harness"
    elif infrastructure:
        decision = "pilot-blocked-repair-control-plane"
    by_arm = {
        arm: {
            "median_seconds": _median(
                row["time_to_accepted_seconds"] for row in rows if row["arm"] == arm
            ),
            "first_pass": sum(
                int(row["first_pass_accepted"]) for row in rows if row["arm"] == arm
            ),
            "accepted": sum(int(row["accepted"]) for row in rows if row["arm"] == arm),
        }
        for arm in ARMS
    }
    return {
        "protocol_sha256": sha256_value(frozen),
        "raw_trial_count": len(rows),
        "by_arm": by_arm,
        "unsafe_trial_count": len(unsafe),
        "invalid_trial_count": len(invalid),
        "attribution_incomplete_count": len(attribution),
        "orchestration_infrastructure_failures": len(infrastructure),
        "slow_review_warnings": sum(
            1 for row in rows if row.get("review_slowness_warning") is True
        ),
        "decision": decision,
        "may_enable_default": False,
    }


def evaluate_with_replacements(
    protocol: Mapping[str, Any], trials: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Evaluate originals plus at most one predeclared whole-block replacement."""

    frozen = validate_executable_protocol(protocol)
    rows = normalize_trials(trials)
    originals = [row for row in rows if row.get("replacement_of") is None]
    replacements = [row for row in rows if row.get("replacement_of") is not None]
    active_rows = list(originals)
    replacement_audit: list[dict[str, Any]] = []
    if replacements:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in replacements:
            grouped[str(row["replacement_of"])].append(row)
        original_task_ids = {str(task["task_id"]) for task in frozen["tasks"]}
        reserve_map = {
            str(task["task_id"]): task for task in frozen["reserve_tasks"]
        }
        active_task_map = {
            str(task["task_id"]): task for task in frozen["tasks"]
        }
        for replaced_id, replacement_rows in grouped.items():
            _require(replaced_id in original_task_ids, "replacement targets unknown original task")
            _require(len(replacement_rows) == 3, "replacement must rerun the whole A/B/C block")
            _require(
                {row["arm"] for row in replacement_rows} == set(ARMS),
                "replacement block needs A/B/C",
            )
            reserve_ids = {row["task_id"] for row in replacement_rows}
            _require(len(reserve_ids) == 1, "replacement A/B/C must use one reserve task")
            reserve_id = next(iter(reserve_ids))
            _require(reserve_id in reserve_map, "replacement must use a frozen reserve task")
            _require(
                reserve_map[reserve_id]["task_class"] == active_task_map[replaced_id]["task_class"],
                "replacement task class must match original",
            )
            reasons = {row.get("invalid_reason") for row in replacement_rows}
            _require(len(reasons) == 1, "replacement block invalid reason drift")
            original_block = [row for row in originals if row["task_id"] == replaced_id]
            _require(
                len(original_block) == 3
                and all(row["failure_class"] == "protocol-invalid" for row in original_block),
                "replacement requires a retained protocol-invalid original block",
            )
            active_rows = [row for row in active_rows if row["task_id"] != replaced_id]
            active_rows.extend(replacement_rows)
            active_task_map.pop(replaced_id)
            active_task_map[reserve_id] = reserve_map[reserve_id]
            replacement_audit.append(
                {
                    "original_task_id": replaced_id,
                    "reserve_task_id": reserve_id,
                    "invalid_reason": next(iter(reasons)),
                }
            )
        evaluation_protocol = dict(frozen)
        evaluation_protocol["tasks"] = list(active_task_map.values())
    else:
        evaluation_protocol = frozen
    if frozen["stage"] == "pilot":
        evaluation = evaluate_pilot(evaluation_protocol, active_rows)
    else:
        evaluation = evaluate_confirmation(evaluation_protocol, active_rows)
    return {
        "evaluation": evaluation,
        "raw_original_trials": len(originals),
        "raw_replacement_trials": len(replacements),
        "replacements": sorted(replacement_audit, key=lambda row: row["original_task_id"]),
        "originals_retained": True,
    }


def blinded_export(
    trials: Iterable[Mapping[str, Any]], *, salt: str
) -> list[dict[str, Any]]:
    _require(bool(salt), "blinding salt is required")
    rows = normalize_trials(trials)
    exported = []
    for row in rows:
        blind_id = hashlib.sha256(
            f"{salt}\0{row['task_id']}\0{row['arm']}".encode("utf-8")
        ).hexdigest()[:20]
        exported.append(
            {
                "blind_id": blind_id,
                "artifact_sha256": row.get("artifact_sha256"),
                "accepted": row["accepted"],
                "first_pass_accepted": row["first_pass_accepted"],
                "review_severity_points": row["review_severity_points"],
                "unresolved_disputes": row["unresolved_disputes"],
            }
        )
    return sorted(exported, key=lambda row: row["blind_id"])


def load_preregistration(path: Path) -> dict[str, Any]:
    candidate = path.expanduser().resolve()
    _require(candidate.is_file() and not candidate.is_symlink(), "preregistration is unavailable")
    try:
        envelope = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkProtocolError("preregistration is invalid JSON") from exc
    _require(
        isinstance(envelope, Mapping) and envelope.get("frozen") is True,
        "live/dry execution requires a frozen preregistration envelope",
    )
    protocol = validate_executable_protocol(envelope.get("protocol", {}))
    _require(
        envelope.get("protocol_sha256") == sha256_value(protocol),
        "preregistration protocol hash mismatch",
    )
    return {"protocol": protocol, "protocol_sha256": envelope["protocol_sha256"], "frozen": True}


_LIVE_REQUIRED_CAPABILITIES = frozenset(
    {
        "producer_review_reference_propagation",
        "post_integration_review",
        "read_only_review",
        "manual_event_lifecycle",
        "cancel_and_replacement",
    }
)


def _current_orchestrator_entrypoint() -> tuple[Path, str]:
    """Bind live benchmark cells to this checkout, never PATH's agent-run."""

    path = Path(__file__).resolve().parents[1] / "agent_orchestrate.py"
    _require(path.is_file() and not path.is_symlink(), "current checkout orchestrator entrypoint unavailable")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def live_launch_preflight(
    protocol: Mapping[str, Any] | None = None,
    *,
    adapter: LiveBenchmarkAdapter | None = None,
    evaluator_root: Path | None = None,
) -> dict[str, Any]:
    """Fail closed on actual lifecycle and whole-block readiness evidence.

    It is intentionally *not* a provider probe.  The runtime adapter supplies
    a credential-free observation of its own seams plus provider auth, host,
    and incident evidence. An absent adapter or missing evidence remains a
    pre-block postponement; there is no synthetic fallback.
    """

    if adapter is None or protocol is None or evaluator_root is None:
        return {
            "eligible": False,
            "action": "block-live-before-first-cell",
            "blockers": [{"code": "governed-live-adapter-unavailable", "detail": "runtime live adapter is unavailable"}],
        }
    try:
        observed = adapter.inspect_benchmark_live(protocol, evaluator_root=evaluator_root)
    except Exception as exc:  # Adapter failures must never turn into a launch.
        return {
            "eligible": False,
            "action": "block-live-before-first-cell",
            "blockers": [{"code": "governed-live-adapter-inspection-failed", "detail": type(exc).__name__}],
        }
    if not isinstance(observed, Mapping):
        return {
            "eligible": False,
            "action": "block-live-before-first-cell",
            "blockers": [{"code": "governed-live-adapter-invalid", "detail": "inspection must return an object"}],
        }
    capabilities = observed.get("capabilities")
    if not isinstance(capabilities, Mapping):
        capabilities = {}
    blockers = [
        {"code": f"{name}-unavailable", "detail": "governed runtime did not attest required lifecycle capability"}
        for name in sorted(_LIVE_REQUIRED_CAPABILITIES)
        if capabilities.get(name) is not True
    ]
    evidence = observed.get("preflight_evidence")
    if not isinstance(evidence, Mapping):
        blockers.append({"code": "whole-block-preflight-unavailable", "detail": "credential-free provider health evidence is missing"})
        gate = {"eligible": False, "action": "postpone-whole-block", "reasons": [{"reason": "provider-evidence-missing"}]}
    else:
        gate = pre_block_gate(protocol, evidence)
        if not gate["eligible"]:
            blockers.extend(
                {"code": f"whole-block-{row['reason']}", "detail": "provider preflight did not satisfy frozen auth/host/incident policy"}
                for row in gate["reasons"]
            )
    fingerprint = observed.get("config_fingerprint")
    if not _is_sha256(fingerprint):
        blockers.append({"code": "config-fingerprint-unavailable", "detail": "runtime config fingerprint is missing or invalid"})
    try:
        entrypoint, entrypoint_sha = _current_orchestrator_entrypoint()
        if observed.get("orchestrator_entrypoint") != str(entrypoint) or observed.get("orchestrator_entrypoint_sha256") != entrypoint_sha:
            blockers.append({"code": "checkout-entrypoint-drift", "detail": "live adapter is not bound to this checkout's agent_orchestrate.py"})
    except BenchmarkProtocolError as exc:
        blockers.append({"code": "checkout-entrypoint-unavailable", "detail": str(exc)})
    return {
        "eligible": not blockers and gate["eligible"],
        "action": "launch-live-block" if not blockers and gate["eligible"] else "block-live-before-first-cell",
        "blockers": blockers,
        "pre_block_gate": gate,
        "config_fingerprint": fingerprint if _is_sha256(fingerprint) else None,
    }


def _live_outcome(
    contract: LaunchContract,
    outcome: Mapping[str, Any],
    *,
    public_task: Mapping[str, Any],
    expected_fingerprint: str,
    block_id: str,
) -> tuple[list[Mapping[str, Any]], list[Path]]:
    """Validate a runtime receipt without allowing it to redefine a cell."""

    _require(isinstance(outcome, Mapping), "live adapter outcome must be an object")
    _require(outcome.get("launcher_kind") == contract.launcher_kind, "manual control-plane deviation")
    _require(outcome.get("graph_sha256") == contract.graph_sha256, "live graph drift")
    _require(outcome.get("manual_runbook_sha256") == contract.manual_runbook_sha256, "manual runbook drift")
    _require(outcome.get("block_id") == block_id, "live block identity drift")
    _require(outcome.get("config_fingerprint") == expected_fingerprint, "live config drift")
    binding = outcome.get("review_binding")
    _require(isinstance(binding, Mapping), "frozen review binding receipt is required")
    reviewer = public_task["reviewer"]
    for key in ("route", "model", "effort", "family", "prompt_sha256"):
        _require(binding.get(key) == reviewer.get(key), f"reviewer drift: {key}")
    _require(binding.get("family") not in set(public_task["producer_families"]), "reviewer independence drift")
    events = outcome.get("events")
    paths = outcome.get("artifact_paths")
    _require(isinstance(events, list) and all(isinstance(row, Mapping) for row in events), "live events are required")
    _require(isinstance(paths, list) and all(isinstance(row, (str, Path)) for row in paths), "live artifact paths are required")
    if contract.arm == "B":
        _require(any(row.get("event") == "coordination_started" for row in events), "manual B coordination event missing")
        _require(any(row.get("event") == "coordination_completed" for row in events), "manual B coordination completion missing")
    return events, [Path(path) for path in paths]


def run_live_experiment(
    preregistration: Mapping[str, Any],
    evaluator_root: Path,
    output_root: Path,
    *,
    adapter: LiveBenchmarkAdapter,
) -> dict[str, Any]:
    """Execute one frozen A/B/C stage through the governed lifecycle adapter.

    The adapter performs all provider invocation.  This function only freezes
    order, checks every immutable receipt, and derives metrics from events.
    """

    _require(preregistration.get("frozen") is True, "live run requires frozen preregistration")
    protocol = validate_executable_protocol(preregistration.get("protocol", {}))
    _require(
        preregistration.get("protocol_sha256") == sha256_value(protocol),
        "preregistration protocol hash mismatch",
    )
    verify_evaluator_root(protocol, evaluator_root)
    preflight = live_launch_preflight(protocol, adapter=adapter, evaluator_root=evaluator_root)
    _require(preflight["eligible"], "live benchmark preflight is not eligible")
    root = output_root.expanduser().resolve()
    _require(not root.exists(), "live output root already exists")
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    trials: list[dict[str, Any]] = []
    block_preflights: dict[str, dict[str, Any]] = {}
    try:
        active_block_id: str | None = None
        active_preflight: dict[str, Any] | None = None
        for cell in counterbalanced_order(protocol):
            task_id, arm = str(cell["task_id"]), str(cell["arm"])
            block_id = f"block-{task_id}"
            if block_id != active_block_id:
                # Re-inspect at every paired-block boundary.  In the production
                # adapter this reloads the mode-0600 evidence file, so an
                # expired or changed attestation cannot be reused by later
                # blocks.  It is deliberately once per block, never per arm.
                active_preflight = live_launch_preflight(
                    protocol, adapter=adapter, evaluator_root=evaluator_root
                )
                _require(
                    active_preflight["eligible"],
                    f"live benchmark preflight is not eligible for {block_id}",
                )
                _require(
                    active_preflight["config_fingerprint"] == preflight["config_fingerprint"],
                    f"live config drift before {block_id}",
                )
                active_block_id = block_id
                block_preflights[block_id] = active_preflight
            contract = build_launch_contract(protocol, evaluator_root, task_id, arm)
            public = _public_task(protocol, task_id)
            cell_root = root / "cells" / task_id / arm
            cell_root.mkdir(parents=True, mode=0o700)
            os.chmod(cell_root, 0o700)
            outcome = adapter.launch_benchmark_arm(
                contract,
                cell_root=cell_root,
                reviewer=public["reviewer"],
                block_id=block_id,
            )
            events, artifacts = _live_outcome(
                contract, outcome, public_task=public,
                expected_fingerprint=str(active_preflight["config_fingerprint"]), block_id=block_id,
            )
            receipt = derive_trial_receipt(
                contract, events, block_id=block_id,
                config_fingerprint_value=str(active_preflight["config_fingerprint"]), artifact_paths=artifacts,
            )
            receipt["live"] = True
            write_private_json(cell_root / "trial-receipt.json", receipt)
            trials.append(receipt)
    except Exception:
        # Keep every receipt already derived, but never fabricate an aggregate.
        write_private_json(root / "raw-trials.partial.json", trials)
        raise
    write_private_json(root / "raw-trials.json", trials)
    evaluation = evaluate_with_replacements(protocol, trials)
    report = {
        "live": True,
        "synthetic": False,
        "cell_count": len(trials),
        "pre_block_gate": preflight["pre_block_gate"],
        "block_preflight_count": len(block_preflights),
        "config_fingerprint": preflight["config_fingerprint"],
        "evaluation": evaluation,
    }
    write_private_json(root / "report.json", report)
    return report


def _fake_events(
    contract: LaunchContract,
    *,
    scenario: str,
    base_time: float,
) -> list[dict[str, Any]]:
    default_duration = {"A": 100.0, "B": 90.0, "C": 72.0}[contract.arm]
    failure_class = "none"
    accepted = True
    scope_violation = False
    duration = default_duration
    if scenario == "misleading-fast-wrong":
        duration, accepted, failure_class = 12.0, False, "task-quality-failure"
    elif scenario == "inside-rate-limit" and contract.arm in {"B", "C"}:
        duration, accepted, failure_class = 35.0, False, "provider-environment-failure"
    elif scenario == "orchestration-failure" and contract.arm == "C":
        duration, accepted, failure_class = 18.0, False, "orchestration-infrastructure-failure"
    elif scenario == "failed-unsafe" and contract.arm == "C":
        duration, accepted, failure_class, scope_violation = 8.0, False, "failed-unsafe", True
    candidate = base_time + duration * 0.65
    acceptance = base_time + duration * 0.75
    review_start = base_time + duration * 0.78
    review_end = base_time + duration * 0.98
    return [
        {"event": "task_handoff", "at": base_time},
        {"event": "graph_ready", "at": base_time + duration * 0.05},
        {"event": "producer_started", "at": base_time + duration * 0.07},
        {"event": "candidate_created", "at": candidate},
        {"event": "acceptance_started", "at": candidate + 0.1},
        {"event": "acceptance_completed", "at": acceptance, "accepted": accepted},
        {"event": "review_started", "at": review_start},
        {"event": "review_completed", "at": review_end},
        {
            "event": "trial_completed",
            "at": base_time + duration,
            "accepted": accepted,
            "failure_class": failure_class,
            "scope_violation": scope_violation,
            "context_construction_ms": 5,
            "delivered_prompt_bytes": len(str(contract.payload["task_input"]).encode("utf-8")),
            "review_severity_points": 3 if not accepted else 0,
            "unresolved_disputes": 1 if scope_violation else 0,
            "attributions": [
                {
                    "run_id": f"dry-run-{contract.task_id}-{contract.arm}-producer",
                    "model": "fake-producer",
                    "session_id": f"dry-session-{contract.task_id}-{contract.arm}",
                    "duration_seconds": max(1.0, candidate - base_time),
                },
                {
                    "run_id": f"dry-run-{contract.task_id}-{contract.arm}-review",
                    "model": "fake-reviewer",
                    "session_id": f"dry-review-session-{contract.task_id}-{contract.arm}",
                    "duration_seconds": max(1.0, review_end - review_start),
                },
            ],
        },
    ]


def run_fake_experiment(
    preregistration: Mapping[str, Any],
    evaluator_root: Path,
    output_root: Path,
    *,
    preflight_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Exercise all harness paths without calling a provider or reviewer."""

    _require(preregistration.get("frozen") is True, "fake run still requires frozen preregistration")
    protocol = validate_executable_protocol(preregistration.get("protocol", {}))
    verify_evaluator_root(protocol, evaluator_root)
    root = output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(root, 0o700)
    if preflight_evidence is None:
        preflight_evidence = {
            family: {
                "auth_ok": True,
                "host_healthy": True,
                "provider_incident": False,
            }
            for family in protocol["required_provider_families"]
        }
    gate = pre_block_gate(protocol, preflight_evidence)
    _require(gate["eligible"], "fake paired block postponed by preflight evidence")
    default_fingerprint = "f" * 64
    trials: list[dict[str, Any]] = []
    for sequence, cell in enumerate(counterbalanced_order(protocol)):
        task_id, arm = cell["task_id"], cell["arm"]
        private = load_private_task(protocol, evaluator_root, task_id)
        scenario_spec = private.get("fixture", {})
        _require(isinstance(scenario_spec, Mapping), f"{task_id}: fixture must be an object")
        scenario = str(scenario_spec.get("scenario_by_arm", {}).get(arm, scenario_spec.get("scenario", "success")))
        contract = build_launch_contract(protocol, evaluator_root, task_id, arm)
        cell_dir = root / "cells" / task_id / arm
        cell_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        artifact = cell_dir / "candidate.txt"
        artifact.write_text(
            f"DRY-RUN ONLY\ntask={task_id}\narm={arm}\nscenario={scenario}\n",
            encoding="utf-8",
        )
        os.chmod(artifact, 0o600)
        fingerprint = "e" * 64 if scenario == "config-drift" and arm == "C" else default_fingerprint
        receipt = derive_trial_receipt(
            contract,
            _fake_events(contract, scenario=scenario, base_time=float(sequence * 1000)),
            block_id=f"block-{task_id}",
            config_fingerprint_value=fingerprint,
            artifact_paths=[artifact],
        )
        receipt["dry_run"] = True
        receipt["synthetic"] = True
        write_private_json(cell_dir / "trial-receipt.json", receipt)
        trials.append(receipt)
    write_private_json(root / "raw-trials.json", trials)
    evaluation = evaluate_with_replacements(protocol, trials)
    report = {
        "dry_run": True,
        "synthetic": True,
        "protocol_sha256": preregistration.get("protocol_sha256"),
        "cell_count": len(trials),
        "pre_block_gate": gate,
        "evaluation": evaluation,
    }
    write_private_json(root / "report.json", report)
    return report
