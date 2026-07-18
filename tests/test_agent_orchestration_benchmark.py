from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "orchestration_benchmark", ROOT / "scripts" / "orchestration" / "benchmark.py"
)
benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(benchmark)


H = "a" * 64


def task(task_id: str, task_class: str) -> dict:
    return {
        "task_id": task_id,
        "task_class": task_class,
        "base_commit": "abc1234",
        "intent_sha256": H,
        "prompt_sha256": H,
        "route_policy_sha256": H,
        "manual_runbook_sha256": H,
        "graph_sha256": H,
        "private_task_sha256": H,
        "single_producer_task_shape": "ordinary_bug_fix",
        "acceptance_commands": ["python -m pytest -q"],
        "deadline_seconds": 600,
        "writer_limit": 1 if task_class == "negative_control" else 2,
        "producer_families": ["openai", "cursor"],
        "reviewer": {
            "route": "claude_final_review",
            "model": "opus",
            "effort": "high",
            "family": "anthropic",
            "independence": "cross_family",
            "prompt_sha256": H,
            "timeout_seconds": 900,
        },
    }


def protocol(stage: str = "pilot") -> dict:
    tasks = [
        task("sep-1", "separable"),
        task("neg-1", "negative_control"),
        task("read-1", "read_only"),
    ]
    if stage == "confirmation":
        tasks = [task(f"sep-{i}", "separable") for i in range(6)]
        tasks += [task(f"neg-{i}", "negative_control") for i in range(3)]
        tasks += [task(f"read-{i}", "read_only") for i in range(3)]
    reserves = [
        task("reserve-sep", "separable"),
        task("reserve-neg", "negative_control"),
        task("reserve-read", "read_only"),
    ]
    return {
        "version": 1,
        "stage": stage,
        "order_seed": 42,
        "hidden_manifest_sha256": H,
        "thresholds": benchmark.expected_thresholds(),
        "arm_contract_version": 1,
        "arm_order_strategy": "seeded-balanced-latin-square-v1",
        "invalid_trial_rules": benchmark.expected_invalid_trial_rules(),
        "review_warning_rule": benchmark.expected_review_warning_rule(),
        "required_provider_families": ["openai", "cursor", "anthropic"],
        "quota_rules": {
            provider: {
                "min_headroom_fraction": 0.25,
                "minimum_cooldown_seconds": 60,
                "retry_after_formula": "max(retry_after,minimum_cooldown)",
            }
            for provider in ("openai", "cursor", "anthropic")
        },
        "tasks": tasks,
        "reserve_tasks": reserves,
    }


def trial(task_id: str, arm: str, seconds: float, *, first_pass: bool = True) -> dict:
    return {
        "task_id": task_id,
        "arm": arm,
        "block_id": f"block-{task_id}",
        "failure_class": "none",
        "config_fingerprint": "f" * 64,
        "time_to_accepted_seconds": seconds,
        "producer_seconds": seconds - 5,
        "review_seconds": 5,
        "agent_minutes": 2 if arm == "C" else 1,
        "context_construction_ms": 10,
        "delivered_prompt_bytes": 100,
        "coordination_seconds": 0,
        "accepted": True,
        "first_pass_accepted": first_pass,
        "attribution_complete": True,
        "scope_violation": False,
        "rework_rounds": 0,
        "review_severity_points": 0,
        "unresolved_disputes": 0,
    }


def test_validate_protocol_requires_exact_stage_counts_and_reviewer_independence():
    assert benchmark.validate_protocol(protocol())["stage"] == "pilot"
    bad = protocol()
    bad["tasks"][0]["reviewer"]["family"] = "openai"
    with pytest.raises(benchmark.BenchmarkProtocolError, match="reviewer family"):
        benchmark.validate_protocol(bad)


def test_preregister_writes_private_canonical_envelope(tmp_path):
    path = tmp_path / "protocol.json"
    envelope = benchmark.preregister(protocol(), path)
    assert json.loads(path.read_text()) == envelope
    assert path.stat().st_mode & 0o777 == 0o600
    assert envelope["protocol_sha256"] == benchmark.sha256_value(envelope["protocol"])


def test_config_fingerprint_removes_credentials_and_rejects_unknown_category():
    one = benchmark.config_fingerprint(
        {"model": "x", "api_key": "secret", "nested": {"token": "no", "mode": "ok"}},
        "official",
    )
    two = benchmark.config_fingerprint(
        {"model": "x", "api_key": "different", "nested": {"token": "x", "mode": "ok"}},
        "official",
    )
    assert one == two
    with pytest.raises(benchmark.BenchmarkProtocolError, match="official or proxy"):
        benchmark.config_fingerprint({}, "unknown")


def test_paired_config_drift_invalidates_whole_block():
    rows = [trial("sep-1", arm, 10) for arm in benchmark.ARMS]
    rows[-1]["config_fingerprint"] = "e" * 64
    normalized = benchmark.validate_paired_blocks(protocol(), rows)
    assert {row["failure_class"] for row in normalized} == {"protocol-invalid"}


def test_confirmation_thresholds_pass_for_fast_safe_c():
    frozen = protocol("confirmation")
    rows = []
    for item in frozen["tasks"]:
        task_id = item["task_id"]
        rows.extend(
            [
                trial(task_id, "A", 100),
                trial(task_id, "B", 120),
                trial(task_id, "C", 75 if item["task_class"] == "separable" else 105),
            ]
        )
    report = benchmark.evaluate_confirmation(frozen, rows)
    assert report["h1_pass"] is True
    assert report["h2_pass"] is True
    assert report["decision"] == "enable-passing-task-shapes"


def test_failed_unsafe_forces_fail_and_blinding_hides_arm_and_task():
    frozen = protocol("confirmation")
    rows = []
    for item in frozen["tasks"]:
        task_id = item["task_id"]
        rows.extend([trial(task_id, "A", 100), trial(task_id, "B", 120), trial(task_id, "C", 80)])
    rows[2]["failure_class"] = "failed-unsafe"
    report = benchmark.evaluate_confirmation(frozen, rows)
    assert report["decision"] == "fail"
    blinded = benchmark.blinded_export(rows[:3], salt="fixed")
    assert all("arm" not in row and "task_id" not in row for row in blinded)
