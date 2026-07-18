from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.orchestration import benchmark


NOW = dt.datetime(2026, 7, 18, 18, 0, tzinfo=dt.timezone.utc)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _task(task_id: str, task_class: str) -> dict:
    return {
        "task_id": task_id,
        "task_class": task_class,
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "intent_sha256": _sha(f"intent:{task_id}"),
        "prompt_sha256": _sha(f"prompt:{task_id}"),
        "route_policy_sha256": _sha("routing-policy-v1"),
        "manual_runbook_sha256": _sha(f"runbook:{task_id}"),
        "graph_sha256": _sha(f"graph:{task_id}"),
        "private_task_sha256": _sha(f"private:{task_id}"),
        "single_producer_task_shape": "ordinary_bug_fix",
        "acceptance_commands": ["python -m pytest -q"],
        "deadline_seconds": 600,
        "writer_limit": 1 if task_class == "negative_control" else 2,
        "producer_families": ["openai", "cursor"],
        "reviewer": {
            "route": "claude_final_review",
            "model": "fable-fast",
            "effort": "high",
            "family": "anthropic",
            "independence": "cross_family",
            "prompt_sha256": _sha(f"review:{task_id}"),
            "timeout_seconds": 900,
        },
    }


def _protocol() -> dict:
    return {
        "version": 1,
        "stage": "pilot",
        "order_seed": 1,
        "hidden_manifest_sha256": _sha("private-manifest"),
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
        "tasks": [_task("sep", "separable"), _task("neg", "negative_control"), _task("read", "read_only")],
        "reserve_tasks": [_task("reserve-sep", "separable"), _task("reserve-neg", "negative_control"), _task("reserve-read", "read_only")],
    }


def _bundle(protocol: dict, **overrides: object) -> dict:
    observed = NOW.isoformat().replace("+00:00", "Z")
    expires = (NOW + dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    policy = protocol["tasks"][0]["route_policy_sha256"]
    bundle = {
        "version": 2,
        "observed_at": observed,
        "expires_at": expires,
        "freshness_window_seconds": 300,
        "attested_by": "local-operator",
        "required_provider_families": list(protocol["required_provider_families"]),
        "provider_families": {
            family: {
                "auth_ok": True,
                "host_healthy": True,
                "provider_incident": False,
            }
            for family in protocol["required_provider_families"]
        },
        "config_fingerprint": _sha("credential-stripped-config"),
        "provider_category": "official",
        "host_identity": _sha("host"),
        "checkout_identity": _sha("checkout"),
        "route_policy_sha256": policy,
    }
    bundle.update(overrides)
    return bundle


def _write(path: Path, bundle: dict, mode: int = 0o600) -> None:
    benchmark.write_private_json(path, bundle, mode=mode)
    os.chmod(path, mode)


def test_attested_evidence_accepts_exact_private_bundle_and_returns_gate_rows(tmp_path: Path):
    protocol = _protocol()
    path = tmp_path / "attested-evidence.json"
    _write(path, _bundle(protocol))

    loaded = benchmark.load_attested_evidence(
        path,
        protocol,
        now=NOW,
        expected_host_identity=_sha("host"),
        expected_checkout_identity=_sha("checkout"),
        expected_config_fingerprint=_sha("credential-stripped-config"),
    )

    assert set(loaded["preflight_evidence"]) == set(protocol["required_provider_families"])
    assert benchmark.pre_block_gate(protocol, loaded["preflight_evidence"])["eligible"] is True


def test_attested_evidence_freshness_accepts_one_hour_and_rejects_one_second_more(tmp_path: Path):
    protocol = _protocol()
    path = tmp_path / "attested-evidence.json"
    one_hour = _bundle(
        protocol,
        expires_at=(NOW + dt.timedelta(seconds=3600)).isoformat().replace("+00:00", "Z"),
        freshness_window_seconds=3600,
    )
    _write(path, one_hour)
    bindings = {
        "expected_host_identity": _sha("host"),
        "expected_checkout_identity": _sha("checkout"),
        "expected_config_fingerprint": _sha("credential-stripped-config"),
    }
    assert benchmark.load_attested_evidence(path, protocol, now=NOW, **bindings)["expires_at"] == one_hour["expires_at"]

    too_long = _bundle(
        protocol,
        expires_at=(NOW + dt.timedelta(seconds=3601)).isoformat().replace("+00:00", "Z"),
        freshness_window_seconds=3601,
    )
    _write(path, too_long)
    with pytest.raises(benchmark.BenchmarkProtocolError, match="exceeds maximum"):
        benchmark.load_attested_evidence(path, protocol, now=NOW, **bindings)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda b: b.update({"unexpected": True}), "unexpected"),
        (lambda b: b.update({"api_key": "secret"}), "sensitive"),
        (lambda b: b["provider_families"]["openai"].update({"token": "secret"}), "sensitive"),
        (lambda b: b["provider_families"].pop("cursor"), "provider family"),
        (lambda b: b.update({"observed_at": "2026-07-18T18:01:00Z", "expires_at": "2026-07-18T18:06:00Z"}), "future"),
        (lambda b: b.update({"observed_at": "2026-07-18T17:50:00Z", "expires_at": "2026-07-18T17:55:00Z"}), "expired"),
        (lambda b: b.update({"host_identity": _sha("other-host")}), "host identity"),
        (lambda b: b.update({"route_policy_sha256": _sha("other-policy")}), "route policy"),
        (lambda b: b.update({"config_fingerprint": _sha("other-config")}), "config fingerprint"),
        (lambda b: b.update({"version": 1}), "version"),
        (lambda b: b["provider_families"]["cursor"].update({"headroom_fraction": 0.8}), "schema"),
    ],
)
def test_attested_evidence_rejects_contract_and_gate_failures(tmp_path: Path, mutate, message: str):
    protocol = _protocol()
    bundle = _bundle(protocol)
    mutate(bundle)
    path = tmp_path / "attested-evidence.json"
    _write(path, bundle)
    with pytest.raises(benchmark.BenchmarkProtocolError, match=message):
        benchmark.load_attested_evidence(
            path, protocol, now=NOW,
            expected_host_identity=_sha("host"), expected_checkout_identity=_sha("checkout"),
            expected_config_fingerprint=_sha("credential-stripped-config"),
        )


def test_attested_evidence_rejects_missing_path_permissions_and_symlink(tmp_path: Path):
    protocol = _protocol()
    with pytest.raises(benchmark.BenchmarkProtocolError, match="unavailable"):
        benchmark.load_attested_evidence(None, protocol, now=NOW)
    path = tmp_path / "attested-evidence.json"
    _write(path, _bundle(protocol), mode=0o644)
    with pytest.raises(benchmark.BenchmarkProtocolError, match="0600"):
        benchmark.load_attested_evidence(path, protocol, now=NOW)
    os.chmod(path, 0o600)
    link = tmp_path / "linked-evidence.json"
    link.symlink_to(path)
    with pytest.raises(benchmark.BenchmarkProtocolError, match="symlink"):
        benchmark.load_attested_evidence(link, protocol, now=NOW)


def test_attested_pre_block_gate_reloads_and_rejects_evidence_that_expires_between_blocks(tmp_path: Path):
    protocol = _protocol()
    path = tmp_path / "attested-evidence.json"
    _write(path, _bundle(protocol))
    bindings = {
        "expected_host_identity": _sha("host"),
        "expected_checkout_identity": _sha("checkout"),
        "expected_config_fingerprint": _sha("credential-stripped-config"),
    }
    assert benchmark.attested_pre_block_gate(path, protocol, now=NOW, **bindings)["pre_block_gate"]["eligible"]
    with pytest.raises(benchmark.BenchmarkProtocolError, match="expired"):
        benchmark.attested_pre_block_gate(path, protocol, now=NOW + dt.timedelta(minutes=6), **bindings)


def test_executable_protocol_rejects_short_or_placeholder_commit_and_route_policy_hash():
    bad_commit = _protocol()
    bad_commit["tasks"][0]["base_commit"] = "abc1234"
    with pytest.raises(benchmark.BenchmarkProtocolError, match="base_commit"):
        benchmark.validate_executable_protocol(bad_commit)
    bad_hash = _protocol()
    bad_hash["tasks"][0]["route_policy_sha256"] = "a" * 64
    with pytest.raises(benchmark.BenchmarkProtocolError, match="route_policy_sha256"):
        benchmark.validate_executable_protocol(bad_hash)


def test_executable_protocol_requires_frozen_quota_independent_preflight_policy():
    missing = _protocol()
    missing.pop("provider_preflight_policy")
    with pytest.raises(benchmark.BenchmarkProtocolError, match="provider_preflight_policy"):
        benchmark.validate_executable_protocol(missing)
    legacy = _protocol()
    legacy["quota_rules"] = {}
    with pytest.raises(benchmark.BenchmarkProtocolError, match="quota_rules"):
        benchmark.validate_executable_protocol(legacy)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "different-mode"),
        ("quota_monitoring", True),
        ("inside_block_rate_limit", "preflight-blocker"),
        ("evidence_schema_version", 3),
        ("max_freshness_seconds", 3599),
        ("future_skew_seconds", 31.0),
    ],
)
def test_executable_protocol_rejects_preflight_policy_mutation(field: str, value: object):
    protocol = _protocol()
    protocol["provider_preflight_policy"][field] = value
    with pytest.raises(benchmark.BenchmarkProtocolError, match="provider_preflight_policy"):
        benchmark.validate_executable_protocol(protocol)


def test_live_runner_rechecks_each_block_and_keeps_prior_receipts_when_later_gate_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    protocol = _protocol()
    preregistration = {
        "frozen": True,
        "protocol": protocol,
        "protocol_sha256": benchmark.sha256_value(protocol),
    }
    launches: list[tuple[str, str]] = []
    calls = 0
    ready = {
        "eligible": True,
        "action": "launch-live-block",
        "blockers": [],
        "pre_block_gate": {"eligible": True, "action": "start-whole-block", "reasons": []},
        "config_fingerprint": _sha("credential-stripped-config"),
    }
    expired = {
        "eligible": False,
        "action": "block-live-before-first-cell",
        "blockers": [{"code": "whole-block-evidence-expired", "detail": "expired"}],
        "pre_block_gate": {"eligible": False, "action": "postpone-whole-block", "reasons": [{"reason": "expired"}]},
        "config_fingerprint": _sha("credential-stripped-config"),
    }

    def fake_preflight(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return ready if calls < 3 else expired

    def fake_contract(_protocol, _root, task_id, arm):
        return benchmark.LaunchContract(
            task_id=task_id, arm=arm, launcher_kind="fake", payload={},
            graph_sha256=_sha(f"graph:{task_id}"), manual_runbook_sha256=_sha(f"runbook:{task_id}"),
        )

    class Adapter:
        def launch_benchmark_arm(self, contract, *, cell_root, reviewer, block_id):
            launches.append((contract.task_id, contract.arm))
            return {}

    monkeypatch.setattr(benchmark, "verify_evaluator_root", lambda *_args: {})
    monkeypatch.setattr(benchmark, "live_launch_preflight", fake_preflight)
    monkeypatch.setattr(
        benchmark,
        "counterbalanced_order",
        lambda _protocol: [
            *({"task_id": "sep", "arm": arm} for arm in benchmark.ARMS),
            *({"task_id": "neg", "arm": arm} for arm in benchmark.ARMS),
        ],
    )
    monkeypatch.setattr(benchmark, "build_launch_contract", fake_contract)
    monkeypatch.setattr(benchmark, "_public_task", lambda *_args: {"reviewer": {}})
    monkeypatch.setattr(benchmark, "_live_outcome", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(
        benchmark,
        "derive_trial_receipt",
        lambda contract, _events, **_kwargs: {"task_id": contract.task_id, "arm": contract.arm},
    )

    root = tmp_path / "live-output"
    with pytest.raises(benchmark.BenchmarkProtocolError, match="block-neg"):
        benchmark.run_live_experiment(preregistration, tmp_path / "evaluator", root, adapter=Adapter())
    assert launches == [("sep", "A"), ("sep", "B"), ("sep", "C")]
    assert [row["task_id"] for row in json.loads((root / "raw-trials.partial.json").read_text())] == ["sep"] * 3
