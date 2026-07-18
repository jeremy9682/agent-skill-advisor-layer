from __future__ import annotations

import importlib.util
import hashlib
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


H = hashlib.sha256(b"benchmark-test-fixture").hexdigest()
BASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def task(task_id: str, task_class: str) -> dict:
    return {
        "task_id": task_id,
        "task_class": task_class,
        "base_commit": BASE_COMMIT,
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
        "provider_preflight_policy": {
            "mode": "auth-host-incident-v1",
            "quota_monitoring": False,
            "inside_block_rate_limit": "treatment-outcome",
            "evidence_schema_version": 2,
            "max_freshness_seconds": 3600,
            "future_skew_seconds": 30.0,
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


def test_failed_provider_before_candidate_produces_a_real_trial_receipt():
    contract = benchmark.LaunchContract(
        task_id="read-1",
        arm="A",
        launcher_kind="single-native-producer",
        payload={},
        graph_sha256=H,
        manual_runbook_sha256=H,
    )
    receipt = benchmark.derive_trial_receipt(
        contract,
        [
            {"event": "task_handoff", "at": 0},
            {"event": "producer_started", "at": 1},
            {
                "event": "trial_completed",
                "at": 10,
                "accepted": False,
                "failure_class": "provider-environment-failure",
                "attributions": [
                    {
                        "run_id": "failed-provider-run",
                        "model": "gpt-5.6-terra",
                        "session_id": "failed-provider-session",
                        "duration_seconds": 9,
                    }
                ],
            },
        ],
        block_id="block-read-1",
        config_fingerprint_value="f" * 64,
        artifact_paths=[],
    )
    assert receipt["accepted"] is False
    assert receipt["failure_class"] == "provider-environment-failure"
    assert receipt["producer_seconds"] == 10
    assert receipt["review_seconds"] == 0
    assert receipt["artifact_sha256"] is None
    assert receipt["artifacts"] == []


def test_success_receipt_still_requires_the_full_quality_chain(tmp_path: Path):
    contract = benchmark.LaunchContract(
        task_id="read-1",
        arm="A",
        launcher_kind="single-native-producer",
        payload={},
        graph_sha256=H,
        manual_runbook_sha256=H,
    )
    artifact = tmp_path / "candidate.txt"
    artifact.write_text("candidate", encoding="utf-8")
    events = [
        {"event": "task_handoff", "at": 0},
        {"event": "acceptance_completed", "at": 2, "accepted": True},
        {"event": "review_started", "at": 3},
        {"event": "review_completed", "at": 4},
        {
            "event": "trial_completed",
            "at": 5,
            "accepted": True,
            "failure_class": "none",
            "attributions": [],
        },
    ]
    with pytest.raises(benchmark.BenchmarkProtocolError, match="candidate_created"):
        benchmark.derive_trial_receipt(
            contract,
            events,
            block_id="block-read-1",
            config_fingerprint_value="f" * 64,
            artifact_paths=[artifact],
        )


def test_task_quality_failure_cannot_masquerade_as_a_pre_candidate_failure():
    contract = benchmark.LaunchContract(
        task_id="read-1",
        arm="A",
        launcher_kind="single-native-producer",
        payload={},
        graph_sha256=H,
        manual_runbook_sha256=H,
    )
    with pytest.raises(benchmark.BenchmarkProtocolError, match="requires a candidate"):
        benchmark.derive_trial_receipt(
            contract,
            [
                {"event": "task_handoff", "at": 0},
                {
                    "event": "trial_completed",
                    "at": 5,
                    "accepted": False,
                    "failure_class": "task-quality-failure",
                    "attributions": [],
                },
            ],
            block_id="block-read-1",
            config_fingerprint_value="f" * 64,
            artifact_paths=[],
        )


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
