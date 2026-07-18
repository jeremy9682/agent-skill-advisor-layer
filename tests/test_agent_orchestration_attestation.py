from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.orchestration import benchmark
from scripts.orchestration.attestation import build_attested_evidence, write_new_private_evidence
from scripts.orchestration.runtime import BenchmarkLiveRuntimeAdapter


REPO = Path(__file__).parents[1]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _task(task_id: str, task_class: str) -> dict:
    return {
        "task_id": task_id, "task_class": task_class,
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "intent_sha256": _sha("intent:" + task_id), "prompt_sha256": _sha("prompt:" + task_id),
        "route_policy_sha256": _sha("route"), "manual_runbook_sha256": _sha("runbook:" + task_id),
        "graph_sha256": _sha("graph:" + task_id), "private_task_sha256": _sha("private:" + task_id),
        "single_producer_task_shape": "ordinary_bug_fix", "acceptance_commands": ["python -m pytest -q"],
        "deadline_seconds": 600, "writer_limit": 1 if task_class == "negative_control" else 2,
        "producer_families": ["openai", "cursor"],
        "reviewer": {"route": "claude_final_review", "model": "fable-fast", "effort": "high", "family": "anthropic", "independence": "cross_family", "prompt_sha256": _sha("review:" + task_id), "timeout_seconds": 900},
    }


def _protocol() -> dict:
    tasks = [_task("sep", "separable"), _task("neg", "negative_control"), _task("read", "read_only")]
    reserves = [_task("r-sep", "separable"), _task("r-neg", "negative_control"), _task("r-read", "read_only")]
    families = ["openai", "cursor", "anthropic"]
    return {
        "version": 1, "stage": "pilot", "order_seed": 2, "hidden_manifest_sha256": _sha("manifest"),
        "thresholds": benchmark.expected_thresholds(), "arm_contract_version": 1,
        "arm_order_strategy": "seeded-balanced-latin-square-v1",
        "invalid_trial_rules": benchmark.expected_invalid_trial_rules(),
        "review_warning_rule": benchmark.expected_review_warning_rule(),
        "required_provider_families": families,
        "provider_preflight_policy": {
            "mode": "auth-host-incident-v1",
            "quota_monitoring": False,
            "inside_block_rate_limit": "treatment-outcome",
            "evidence_schema_version": 2,
            "max_freshness_seconds": 3600,
            "future_skew_seconds": 30.0,
        },
        "tasks": tasks, "reserve_tasks": reserves,
    }


def _rows() -> dict[str, dict]:
    return {family: {"auth_ok": True, "host_healthy": True, "provider_incident": False} for family in ("openai", "cursor", "anthropic")}


def _bindings() -> tuple[BenchmarkLiveRuntimeAdapter, dict[str, str]]:
    adapter = BenchmarkLiveRuntimeAdapter(REPO)
    return adapter, {"expected_host_identity": adapter._host_identity(), "expected_checkout_identity": adapter._checkout_identity(), "expected_config_fingerprint": adapter._config_fingerprint}


def test_builder_round_trips_through_strict_loader(tmp_path: Path):
    protocol = _protocol()
    bundle = build_attested_evidence(protocol, checkout_root=REPO, observations=_rows(), attested_by="operator-1", provider_category="official", ttl_seconds=300)
    output = write_new_private_evidence(tmp_path / "evidence.json", bundle)
    _adapter, bindings = _bindings()
    loaded = benchmark.load_attested_evidence(output, protocol, **bindings)
    assert set(loaded["preflight_evidence"]) == {"openai", "cursor", "anthropic"}
    assert os.stat(output).st_mode & 0o777 == 0o600


def test_builder_refuses_missing_family_and_identity_drift(tmp_path: Path):
    protocol = _protocol()
    rows = _rows()
    rows.pop("cursor")
    with pytest.raises(benchmark.BenchmarkProtocolError, match="exactly match"):
        build_attested_evidence(protocol, checkout_root=REPO, observations=rows, attested_by="operator-1", provider_category="official", ttl_seconds=300)
    bundle = build_attested_evidence(protocol, checkout_root=REPO, observations=_rows(), attested_by="operator-1", provider_category="official", ttl_seconds=300)
    output = write_new_private_evidence(tmp_path / "evidence.json", bundle)
    _adapter, bindings = _bindings()
    bindings["expected_checkout_identity"] = _sha("other-checkout")
    with pytest.raises(benchmark.BenchmarkProtocolError, match="checkout identity"):
        benchmark.load_attested_evidence(output, protocol, **bindings)


def test_builder_accepts_one_hour_evidence_and_rejects_longer_ttl():
    protocol = _protocol()
    bundle = build_attested_evidence(
        protocol,
        checkout_root=REPO,
        observations=_rows(),
        attested_by="operator-1",
        provider_category="official",
        ttl_seconds=3600,
    )
    assert bundle["freshness_window_seconds"] == 3600
    with pytest.raises(benchmark.BenchmarkProtocolError, match="3600"):
        build_attested_evidence(
            protocol,
            checkout_root=REPO,
            observations=_rows(),
            attested_by="operator-1",
            provider_category="official",
            ttl_seconds=3601,
        )


def test_writer_refuses_overwrite_and_symlink(tmp_path: Path):
    bundle = {"safe": "value"}
    output = tmp_path / "evidence.json"
    write_new_private_evidence(output, bundle)
    with pytest.raises(benchmark.BenchmarkProtocolError, match="already exists"):
        write_new_private_evidence(output, bundle)
    link = tmp_path / "link.json"
    link.symlink_to(output)
    with pytest.raises(benchmark.BenchmarkProtocolError, match="symlink"):
        write_new_private_evidence(link, bundle)


def test_writer_refuses_symlink_or_non_private_parent_without_chmod(tmp_path: Path):
    bundle = {"safe": "value"}
    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(private_parent, target_is_directory=True)
    with pytest.raises(benchmark.BenchmarkProtocolError, match="parent must not be a symlink"):
        write_new_private_evidence(linked_parent / "evidence.json", bundle)

    public_parent = tmp_path / "public"
    public_parent.mkdir(mode=0o755)
    with pytest.raises(benchmark.BenchmarkProtocolError, match="exact mode 0700"):
        write_new_private_evidence(public_parent / "evidence.json", bundle)
    assert os.stat(public_parent).st_mode & 0o777 == 0o755


def test_cli_is_privacy_preserving_and_requires_explicit_rows(tmp_path: Path):
    protocol = _protocol()
    prereg = tmp_path / "prereg.json"
    benchmark.preregister(protocol, prereg)
    output = tmp_path / "evidence.json"
    command = [
        sys.executable, str(REPO / "scripts" / "build_agent_orchestration_evidence.py"),
        "--prereg", str(prereg), "--output", str(output), "--attested-by", "operator-1",
        "--provider-observation", "openai:true:true:false",
        "--provider-observation", "cursor:true:true:false",
        "--provider-observation", "anthropic:true:true:false",
    ]
    completed = subprocess.run(command, cwd=REPO, text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    emitted = json.loads(completed.stdout)
    assert set(emitted) == {"status", "bundle_path", "bundle_sha256", "expires_at", "required_provider_families"}
    assert "operator-1" not in completed.stdout
    assert "token" not in output.read_text().lower()
