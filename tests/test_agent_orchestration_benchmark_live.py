from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

import pytest

from scripts.agent_orchestration_benchmark import main
from scripts.orchestration import benchmark
from scripts.orchestration.benchmark_lifecycle import LifecycleLaunch
from scripts.orchestration.runtime import BenchmarkLiveRuntimeAdapter, RuntimeErrorSafe


class _LiveAdapter:
    """No-provider lifecycle double; the harness still validates its receipts."""

    def __init__(self, *, bad: str | None = None):
        self.bad = bad

    def inspect_benchmark_live(self, protocol, *, evaluator_root):
        entrypoint, digest = benchmark._current_orchestrator_entrypoint()
        evidence = {
            family: {
                "auth_ok": True,
                "host_healthy": True,
                "provider_incident": False,
            }
            for family in protocol["required_provider_families"]
        }
        return {
            "capabilities": {name: True for name in benchmark._LIVE_REQUIRED_CAPABILITIES},
            "preflight_evidence": evidence,
            "config_fingerprint": "c" * 64,
            "orchestrator_entrypoint": str(entrypoint),
            "orchestrator_entrypoint_sha256": digest,
        }

    def launch_benchmark_arm(self, contract, *, cell_root, reviewer, block_id):
        artifact = cell_root / "candidate.txt"
        artifact.write_text(f"live receipt {contract.task_id}:{contract.arm}", encoding="utf-8")
        events = [
            {"event": "task_handoff", "at": 0},
            {"event": "graph_ready", "at": 1},
        ]
        if contract.arm == "B":
            events += [
                {"event": "coordination_started", "at": 2, "interval_id": "manual-fanout"},
                {"event": "coordination_completed", "at": 3, "interval_id": "manual-fanout"},
            ]
        events += [
            {"event": "producer_started", "at": 4},
            {"event": "candidate_created", "at": 10},
            {"event": "acceptance_started", "at": 11},
            {"event": "acceptance_completed", "at": 12, "accepted": True},
            {"event": "review_started", "at": 13},
            {"event": "review_completed", "at": 14},
            {"event": "trial_completed", "at": 15, "accepted": True, "failure_class": "none", "attributions": [
                {"run_id": f"producer-{contract.arm}", "model": "producer", "session_id": f"producer-session-{contract.arm}", "duration_seconds": 10},
                {"run_id": f"review-{contract.arm}", "model": reviewer["model"], "session_id": f"review-session-{contract.arm}", "duration_seconds": 1},
            ]},
        ]
        binding = dict(reviewer)
        outcome = {
            "launcher_kind": contract.launcher_kind,
            "graph_sha256": contract.graph_sha256,
            "manual_runbook_sha256": contract.manual_runbook_sha256,
            "block_id": block_id,
            "config_fingerprint": "c" * 64,
            "review_binding": binding,
            "events": events,
            "artifact_paths": [str(artifact)],
        }
        if self.bad == "review":
            outcome["review_binding"]["model"] = "different"
        if self.bad == "manual" and contract.arm == "B":
            outcome["launcher_kind"] = "automatic-orchestrator"
        if self.bad == "config" and contract.arm == "C":
            outcome["config_fingerprint"] = "d" * 64
        if self.bad == "cancel" and contract.arm == "C":
            outcome["events"][-1]["failure_class"] = "failed-unsafe"
            outcome["events"][-1]["accepted"] = False
        return outcome


def _private(task_id: str, *, scenario: str = "success") -> dict:
    return {
        "version": 1,
        "task_id": task_id,
        "intent": f"intent for {task_id}",
        "task_input": f"implement only {task_id}",
        "graph": {
            "nodes": [
                {
                    "id": "producer",
                    "task_shape": "ordinary_bug_fix",
                    "depends_on": [],
                    "input": f"implement only {task_id}",
                }
            ]
        },
        "manual_runbook": {
            "events": ["launch_ready_nodes", "collect_receipts", "join", "accept"]
        },
        "hidden_assertions": ["private evaluator assertion"],
        "fixture": {"scenario": scenario},
    }


def _public(private: dict, task_class: str) -> dict:
    return {
        "task_id": private["task_id"],
        "task_class": task_class,
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "intent_sha256": benchmark.sha256_value(private["intent"]),
        "prompt_sha256": benchmark.sha256_value(private["task_input"]),
        "route_policy_sha256": "0123456789abcdef" * 4,
        "manual_runbook_sha256": benchmark.sha256_value(private["manual_runbook"]),
        "graph_sha256": benchmark.sha256_value(private["graph"]),
        "acceptance_commands": ["python -m pytest -q"],
        "deadline_seconds": 600,
        "writer_limit": 1 if task_class == "negative_control" else 2,
        "producer_families": ["openai", "cursor"],
        "single_producer_task_shape": "ordinary_bug_fix",
        "reviewer": {
            "route": "claude_final_review",
            "model": "fable-fast",
            "effort": "high",
            "family": "anthropic",
            "independence": "cross_family",
            "prompt_sha256": "b" * 64,
            "timeout_seconds": 900,
        },
    }


def _fixture(
    tmp_path: Path,
    *,
    stage: str = "pilot",
    scenario: str = "success",
) -> tuple[dict, Path, Path]:
    if stage == "pilot":
        identities = [("sep-1", "separable"), ("neg-1", "negative_control"), ("read-1", "read_only")]
    else:
        identities = [(f"sep-{index}", "separable") for index in range(6)]
        identities += [(f"neg-{index}", "negative_control") for index in range(3)]
        identities += [(f"read-{index}", "read_only") for index in range(3)]
    reserve_identities = [
        ("reserve-sep", "separable"),
        ("reserve-neg", "negative_control"),
        ("reserve-read", "read_only"),
    ]
    root = tmp_path / "evaluator"
    tasks_dir = root / "tasks"
    tasks_dir.mkdir(parents=True)
    os.chmod(root, 0o700)
    private_rows: dict[str, dict] = {}
    public_rows = []
    reserve_rows = []
    entries = {}
    for task_id, task_class in [*identities, *reserve_identities]:
        private = _private(task_id, scenario=scenario if task_id in {item[0] for item in identities} else "success")
        private_rows[task_id] = private
        payload = (benchmark.canonical_json(private) + "\n").encode()
        path = tasks_dir / f"{task_id}.json"
        path.write_bytes(payload)
        os.chmod(path, 0o600)
        digest = hashlib.sha256(payload).hexdigest()
        public = _public(private, task_class)
        public["private_task_sha256"] = digest
        (public_rows if (task_id, task_class) in identities else reserve_rows).append(public)
        entries[task_id] = {"path": f"tasks/{task_id}.json", "sha256": digest}
    manifest_bytes = (benchmark.canonical_json({"version": 1, "tasks": entries}) + "\n").encode()
    manifest_path = root / "private-manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    os.chmod(manifest_path, 0o600)
    protocol = {
        "version": 1,
        "stage": stage,
        "order_seed": 418,
        "hidden_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
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
        "tasks": public_rows,
        "reserve_tasks": reserve_rows,
    }
    prereg_path = tmp_path / "prereg.json"
    envelope = benchmark.preregister(protocol, prereg_path)
    return envelope, root, prereg_path


def test_counterbalanced_order_and_launch_contract_hide_evaluator_authority(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path)
    order = benchmark.counterbalanced_order(envelope["protocol"])
    assert len(order) == 9
    assert [row["position"] for row in order] == [0, 1, 2] * 3
    assert {tuple(row["arm"] for row in order[index : index + 3]) for index in range(0, 9, 3)} == {
        ("A", "B", "C"),
        ("B", "C", "A"),
        ("C", "A", "B"),
    }
    b = benchmark.build_launch_contract(envelope["protocol"], evaluator, "sep-1", "B")
    c = benchmark.build_launch_contract(envelope["protocol"], evaluator, "sep-1", "C")
    assert b.graph_sha256 == c.graph_sha256
    serialized = json.dumps(b.payload)
    assert "reviewer" not in serialized
    assert "hidden_assertions" not in serialized
    assert "reserve" not in serialized


def test_private_evaluator_root_requires_exact_mode_and_hash(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path)
    assert benchmark.verify_evaluator_root(envelope["protocol"], evaluator)["tasks"]
    os.chmod(evaluator, 0o755)
    with pytest.raises(benchmark.BenchmarkProtocolError, match="0700"):
        benchmark.verify_evaluator_root(envelope["protocol"], evaluator)


def test_pre_block_missing_or_unhealthy_evidence_postpones_whole_block(tmp_path: Path):
    envelope, _, _ = _fixture(tmp_path)
    protocol = envelope["protocol"]
    assert benchmark.pre_block_gate(protocol, {})["action"] == "postpone-whole-block"
    evidence = {
        family: {
            "auth_ok": False,
            "host_healthy": True,
            "provider_incident": False,
        }
        for family in protocol["required_provider_families"]
    }
    result = benchmark.pre_block_gate(protocol, evidence)
    assert result["eligible"] is False
    assert {row["reason"] for row in result["reasons"]} == {"auth-failure"}


@pytest.mark.parametrize("missing", [True, False])
def test_pre_block_requires_explicit_no_incident(tmp_path: Path, missing: bool):
    envelope, _, _ = _fixture(tmp_path)
    protocol = envelope["protocol"]
    evidence = {
        family: {
            "auth_ok": True,
            "host_healthy": True,
            "provider_incident": False,
        }
        for family in protocol["required_provider_families"]
    }
    if missing:
        evidence["cursor"].pop("provider_incident")
    else:
        evidence["cursor"]["provider_incident"] = None
    result = benchmark.pre_block_gate(protocol, evidence)
    assert result["eligible"] is False
    assert {row["reason"] for row in result["reasons"]} == {"provider-incident"}


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("success", "pilot-ready-for-separate-confirmation-approval"),
        ("misleading-fast-wrong", "pilot-ready-for-separate-confirmation-approval"),
        ("config-drift", "pilot-invalid-repair-harness"),
        ("failed-unsafe", "pilot-failed-unsafe"),
    ],
)
def test_fake_pilot_end_to_end_preserves_outcome_truth(tmp_path: Path, scenario: str, expected: str):
    envelope, evaluator, _ = _fixture(tmp_path, scenario=scenario)
    report = benchmark.run_fake_experiment(envelope, evaluator, tmp_path / "output")
    assert report["dry_run"] is True and report["synthetic"] is True
    assert report["cell_count"] == 9
    assert report["evaluation"]["evaluation"]["decision"] == expected
    raw = json.loads((tmp_path / "output" / "raw-trials.json").read_text())
    if scenario == "misleading-fast-wrong":
        assert all(row["accepted"] is False for row in raw)
        assert all(row["failure_class"] == "task-quality-failure" for row in raw)
        assert max(row["time_to_accepted_seconds"] for row in raw) == 12


def test_inside_rate_limit_is_treatment_outcome_not_preblock_exclusion(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path, scenario="inside-rate-limit")
    report = benchmark.run_fake_experiment(envelope, evaluator, tmp_path / "output")
    assert report["pre_block_gate"]["eligible"] is True
    raw = json.loads((tmp_path / "output" / "raw-trials.json").read_text())
    assert sum(row["failure_class"] == "provider-environment-failure" for row in raw) == 6


def test_event_metrics_define_rework_first_pass_coordination_and_slow_review(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path)
    contract = benchmark.build_launch_contract(envelope["protocol"], evaluator, "sep-1", "B")
    artifact = tmp_path / "candidate.txt"
    artifact.write_text("candidate", encoding="utf-8")
    events = [
        {"event": "task_handoff", "at": 0},
        {"event": "coordination_started", "at": 1, "interval_id": "manual-launch"},
        {"event": "producer_started", "at": 2},
        {"event": "coordination_completed", "at": 4, "interval_id": "manual-launch"},
        {"event": "candidate_created", "at": 10},
        {"event": "acceptance_started", "at": 11},
        {"event": "acceptance_completed", "at": 12, "accepted": False},
        {"event": "rework_started", "at": 13},
        {"event": "rework_completed", "at": 18},
        {"event": "candidate_created", "at": 19},
        {"event": "acceptance_completed", "at": 20, "accepted": True},
        {"event": "review_started", "at": 21},
        {"event": "review_completed", "at": 27},
        {
            "event": "trial_completed",
            "at": 28,
            "accepted": True,
            "failure_class": "none",
            "attributions": [
                {"run_id": "run", "model": "model", "session_id": "session", "duration_seconds": 16}
            ],
        },
    ]
    receipt = benchmark.derive_trial_receipt(
        contract,
        events,
        block_id="block-sep-1",
        config_fingerprint_value="f" * 64,
        artifact_paths=[artifact],
    )
    assert receipt["accepted"] is True
    assert receipt["first_pass_accepted"] is False
    assert receipt["rework_rounds"] == 1
    assert receipt["coordination_seconds"] == 3
    assert receipt["review_slowness_warning"] is True
    assert receipt["review_warning_threshold_seconds"] == 5


def test_confirmation_fake_executes_36_cells_but_does_not_claim_live(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path, stage="confirmation")
    report = benchmark.run_fake_experiment(envelope, evaluator, tmp_path / "output")
    assert report["cell_count"] == 36
    assert report["synthetic"] is True
    assert all(row["dry_run"] for row in json.loads((tmp_path / "output" / "raw-trials.json").read_text()))


def test_confirmation_allows_one_whole_block_frozen_reserve_and_retains_original(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path, stage="confirmation")
    benchmark.run_fake_experiment(envelope, evaluator, tmp_path / "output")
    originals = json.loads((tmp_path / "output" / "raw-trials.json").read_text())
    for row in originals:
        if row["task_id"] == "sep-0":
            row["failure_class"] = "protocol-invalid"
    reserve_rows = []
    for arm in benchmark.ARMS:
        template = next(row for row in originals if row["task_id"] == "sep-1" and row["arm"] == arm)
        replacement = dict(template)
        replacement.update(
            {
                "task_id": "reserve-sep",
                "block_id": "block-reserve-sep",
                "replacement_of": "sep-0",
                "invalid_reason": "config-drift",
            }
        )
        reserve_rows.append(replacement)
    evaluated = benchmark.evaluate_with_replacements(envelope["protocol"], [*originals, *reserve_rows])
    assert evaluated["raw_original_trials"] == 36
    assert evaluated["raw_replacement_trials"] == 3
    assert evaluated["originals_retained"] is True
    assert evaluated["replacements"] == [
        {
            "original_task_id": "sep-0",
            "reserve_task_id": "reserve-sep",
            "invalid_reason": "config-drift",
        }
    ]


def test_live_cli_fails_closed_without_matching_checkout_adapter(tmp_path: Path, capsys):
    _, evaluator, prereg_path = _fixture(tmp_path)
    output = tmp_path / "must-not-exist"
    assert main([
        "run",
        "--prereg",
        str(prereg_path),
        "--evaluator-root",
        str(evaluator),
        "--output-root",
        str(output),
        "--live",
    ]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked" and result["live"] is True
    # The checkout-local adapter needs healthy provider evidence before launch;
    # no quota-derived blocker may be emitted.
    codes = {row["code"] for row in result["blockers"]}
    assert codes & {"whole-block-auth-failure", "whole-block-host-unhealthy"}
    assert not any("headroom" in code or "quota" in code for code in codes)
    assert not output.exists()


def test_live_runner_executes_frozen_pilot_through_adapter(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path)
    report = benchmark.run_live_experiment(
        envelope, evaluator, tmp_path / "live-output", adapter=_LiveAdapter()
    )
    assert report["live"] is True and report["synthetic"] is False
    assert report["cell_count"] == 9
    assert (tmp_path / "live-output" / "raw-trials.json").is_file()


@pytest.mark.parametrize("bad", ["review", "manual", "config"])
def test_live_runner_rejects_review_control_plane_and_config_drift(tmp_path: Path, bad: str):
    envelope, evaluator, _ = _fixture(tmp_path)
    with pytest.raises(benchmark.BenchmarkProtocolError):
        benchmark.run_live_experiment(
            envelope, evaluator, tmp_path / f"live-{bad}", adapter=_LiveAdapter(bad=bad)
        )
    assert (tmp_path / f"live-{bad}" / "raw-trials.partial.json").is_file()


def test_live_runner_records_failed_unsafe_without_replacement(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path)
    report = benchmark.run_live_experiment(
        envelope, evaluator, tmp_path / "unsafe", adapter=_LiveAdapter(bad="cancel")
    )
    rows = json.loads((tmp_path / "unsafe" / "raw-trials.json").read_text())
    assert any(row["failure_class"] == "failed-unsafe" for row in rows)
    assert report["evaluation"]["evaluation"]["decision"] == "pilot-failed-unsafe"


def test_live_preflight_missing_provider_evidence_fails_closed(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path)

    class MissingEvidence(_LiveAdapter):
        def inspect_benchmark_live(self, protocol, *, evaluator_root):
            observed = super().inspect_benchmark_live(protocol, evaluator_root=evaluator_root)
            observed["preflight_evidence"] = {}
            return observed

    preflight = benchmark.live_launch_preflight(
        envelope["protocol"], adapter=MissingEvidence(), evaluator_root=evaluator
    )
    assert preflight["eligible"] is False
    assert any("provider-evidence-missing" in row["code"] for row in preflight["blockers"])


def test_runtime_adapter_binds_verified_inspection_and_keeps_production_evidence_missing(tmp_path: Path, monkeypatch):
    envelope, evaluator, _ = _fixture(tmp_path)
    checkout = Path(__file__).parents[1]
    adapter = BenchmarkLiveRuntimeAdapter(checkout)
    observed = adapter.inspect_benchmark_live(envelope["protocol"], evaluator_root=evaluator)
    assert all(row["evidence_status"] == "unknown-blocked" for row in observed["preflight_evidence"].values())
    assert adapter._inspection is not None
    # No CLI switch can turn this production adapter's missing provider evidence into a
    # launchable observation; tests must opt in through its injected factory.
    assert benchmark.live_launch_preflight(envelope["protocol"], adapter=adapter, evaluator_root=evaluator)["eligible"] is False
    monkeypatch.setattr(adapter, "_fingerprint", lambda: "0" * 64)
    contract = benchmark.build_launch_contract(envelope["protocol"], evaluator, "sep-1", "A")
    cell = tmp_path / "cell"
    cell.mkdir(mode=0o700)
    with pytest.raises(RuntimeErrorSafe, match="config drift"):
        adapter.launch_benchmark_arm(contract, cell_root=cell, reviewer=benchmark._public_task(envelope["protocol"], "sep-1")["reviewer"], block_id="block-sep-1")


def test_runtime_adapter_uses_only_attested_evidence_path_for_launch_preflight(
    tmp_path: Path, monkeypatch
):
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir(mode=0o700)
    protocol = {
        "required_provider_families": ["openai", "cursor", "anthropic"],
        "provider_preflight_policy": {
            "mode": "auth-host-incident-v1",
            "quota_monitoring": False,
            "inside_block_rate_limit": "treatment-outcome",
            "evidence_schema_version": 2,
            "max_freshness_seconds": 3600,
            "future_skew_seconds": 30.0,
        },
    }
    evidence_path = tmp_path / "attested-provider-state.json"
    evidence_path.write_text('{"opaque":"not provider configuration"}\n', encoding="utf-8")
    os.chmod(evidence_path, 0o600)
    supplied = {
        family: {
            "auth_ok": True,
            "host_healthy": True,
            "provider_incident": False,
            "evidence_status": "attested",
            "credential": "must-not-be-returned",
        }
        for family in protocol["required_provider_families"]
    }
    calls: list[tuple[Path, dict]] = []

    def load(path, protocol, **_kwargs):
        calls.append((path, dict(protocol)))
        return {"preflight_evidence": supplied}

    monkeypatch.setattr(benchmark, "validate_executable_protocol", lambda value: dict(value))
    monkeypatch.setattr(benchmark, "verify_evaluator_root", lambda *_args: {"manifest_sha256": "m" * 64})
    monkeypatch.setattr(benchmark, "load_attested_evidence", load, raising=False)
    adapter = BenchmarkLiveRuntimeAdapter(Path(__file__).parents[1], evidence_path=evidence_path)
    observed = adapter.inspect_benchmark_live(protocol, evaluator_root=evaluator)
    assert calls and calls[0][0] == evidence_path
    assert observed["preflight_evidence"] == supplied
    assert benchmark.live_launch_preflight(
        protocol, adapter=adapter, evaluator_root=evaluator
    )["eligible"] is True


def test_runtime_adapter_does_not_resolve_attested_evidence_symlink_before_loader(
    tmp_path: Path, monkeypatch
):
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir(mode=0o700)
    target = tmp_path / "attested-provider-state.json"
    target.write_text("{}\n", encoding="utf-8")
    os.chmod(target, 0o600)
    link = tmp_path / "attested-provider-state-link.json"
    link.symlink_to(target)
    protocol = {"required_provider_families": ["openai"]}

    def reject_symlink(path, _protocol, **_kwargs):
        assert path == link and path.is_symlink()
        raise benchmark.BenchmarkProtocolError("attested evidence must not be a symlink")

    monkeypatch.setattr(benchmark, "validate_executable_protocol", lambda value: dict(value))
    monkeypatch.setattr(benchmark, "verify_evaluator_root", lambda *_args: {"manifest_sha256": "m" * 64})
    monkeypatch.setattr(benchmark, "load_attested_evidence", reject_symlink)
    adapter = BenchmarkLiveRuntimeAdapter(Path(__file__).parents[1], evidence_path=link)
    with pytest.raises(RuntimeErrorSafe, match="attested preflight evidence was rejected"):
        adapter.inspect_benchmark_live(protocol, evaluator_root=evaluator)


def test_runtime_adapter_attribution_uses_observed_provider_milliseconds():
    state = {
        "tasks": {
            "producer": {
                "result": {
                    "provider_run_id": "run-1",
                    "model_observed": "model-1",
                    "session_id": "session-1",
                    "provider_duration_ms": 2500,
                }
            }
        }
    }
    assert BenchmarkLiveRuntimeAdapter._attributions(state) == [
        {
            "run_id": "run-1",
            "model": "model-1",
            "session_id": "session-1",
            "duration_seconds": 2.5,
        }
    ]


class _ManualRuntimeProbe:
    two_phase_process = True
    owns_deadline = True

    def __init__(self, plan: dict, artifact_root: Path):
        self.plan = plan
        self.calls: list[tuple[str, float]] = []
        artifact = artifact_root / "observed.json"
        artifact.write_text("{}\n", encoding="utf-8")
        os.chmod(artifact, 0o600)

    def prepare_resource(self, _task, **_kwargs):
        return {"status": "created"}

    def prepare_dependencies(self, _task, _state, **_kwargs):
        return {"status": "not-applicable", "dependency_count": 0}

    def launch_task(self, task, *, run_id, **_kwargs):
        assert run_id == self.plan["run_id"]
        self.calls.append((f"launch:{task['id']}", time.time()))
        return task

    def collect_task(self, task):
        self.calls.append((f"collect:{task['id']}", time.time()))
        return {
            "status": "succeeded",
            "provider_run_id": f"run-{task['id']}",
            "model_observed": f"model-{task['id']}",
            "session_id": f"session-{task['id']}",
            "provider_duration_ms": 1000,
        }

    def finalize_run(self, _plan, _state, **_kwargs):
        self.calls.append(("finalize", time.time()))
        return {"status": "succeeded"}

    def prepare_review(self, _task, _state, **_kwargs):
        self.calls.append(("prepare-review", time.time()))
        return {"status": "succeeded"}

    def terminal_cleanup(self, *_args, **_kwargs):
        return {}


def _manual_launch_fixture(tmp_path: Path) -> LifecycleLaunch:
    evaluator = tmp_path / "evaluator-inputs"
    evaluator.mkdir(mode=0o700)
    plan = {
        "version": 1,
        "run_id": "pre-cell-run-id",
        "repo_root": str(tmp_path / "fixture-repo"),
        "base_sha": "a" * 40,
        "ledger_slug": "fixture-repo",
        "tasks": [
            {
                "id": "producer",
                "depends_on": [],
                "reviewer_for": [],
                "workspace": {"kind": "read-only"},
                "family": "openai",
            },
            {
                "id": "review",
                "depends_on": ["producer"],
                "reviewer_for": ["producer"],
                "workspace": {"kind": "read-only"},
                "family": "claude-family",
            },
        ],
        "budgets": {
            "total_concurrency": 2,
            "writer_concurrency": 1,
            "family_concurrency": {"openai": 1, "claude-family": 1},
        },
    }
    return LifecycleLaunch(
        plan=plan,
        evaluator_root=evaluator,
        input_manifest_sha256="d" * 64,
        manual_runbook={"ready_sets": [["producer"]]},
        graph_sha256="e" * 64,
        manual_runbook_sha256="f" * 64,
    )


def test_runtime_adapter_manual_arm_rebinds_one_run_and_preserves_real_event_order(
    tmp_path: Path, monkeypatch
):
    launch = _manual_launch_fixture(tmp_path)
    contract = benchmark.LaunchContract(
        task_id="fixture",
        arm="B",
        launcher_kind="manual-event-fanout",
        payload={},
        graph_sha256=launch.graph_sha256,
        manual_runbook_sha256=launch.manual_runbook_sha256,
    )
    probes: list[_ManualRuntimeProbe] = []

    def runtime_factory(plan, artifact_root, worktree_root, evaluator_root):
        assert worktree_root == Path(plan["repo_root"]).parent / ".agent-run-worktrees"
        assert evaluator_root == launch.evaluator_root
        probe = _ManualRuntimeProbe(dict(plan), artifact_root)
        probes.append(probe)
        return probe

    checkout = Path(__file__).parents[1]
    adapter = BenchmarkLiveRuntimeAdapter(checkout, runtime_factory=runtime_factory)
    protocol = {"fixture": True}
    adapter._inspection = {
        "protocol": protocol,
        "protocol_sha256": benchmark.sha256_value(protocol),
        "evaluator_root": tmp_path,
        "manifest_sha256": "b" * 64,
        "config_fingerprint": adapter._config_fingerprint,
    }
    monkeypatch.setattr(
        benchmark,
        "compile_governed_lifecycle",
        lambda *_args, **_kwargs: launch,
    )
    cell = tmp_path / "cell"
    cell.mkdir(mode=0o700)
    outcome = adapter.launch_benchmark_arm(
        contract,
        cell_root=cell,
        reviewer={"route": "review"},
        block_id="block-fixture",
    )
    names = [event["event"] for event in outcome["events"]]
    assert names.index("producer_started") < names.index("candidate_created")
    assert names.index("candidate_created") < names.index("acceptance_started")
    assert names.index("acceptance_completed") < names.index("review_started")
    assert len([name for name in names if name == "review_started"]) == 1
    assert len(outcome["events"][-1]["attributions"]) == 2
    assert probes and probes[0].plan["run_id"].startswith("benchmark-cell-")


def test_live_runner_rejects_preregistration_protocol_drift(tmp_path: Path):
    envelope, evaluator, _ = _fixture(tmp_path)
    drifted = dict(envelope)
    drifted["protocol"] = dict(envelope["protocol"])
    drifted["protocol"]["order_seed"] = 999
    # The frozen envelope hash must guard the live lifecycle before an adapter
    # is asked to inspect providers or launch a cell.
    with pytest.raises(benchmark.BenchmarkProtocolError, match="hash mismatch"):
        benchmark.run_live_experiment(
            drifted, evaluator, tmp_path / "drift", adapter=_LiveAdapter()
        )
