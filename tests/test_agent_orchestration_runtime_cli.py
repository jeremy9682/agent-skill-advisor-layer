import json
from pathlib import Path
import subprocess
import sys

import pytest

import scripts.agent_orchestrate as orchestrate_cli
import scripts.agent_orchestration_benchmark as benchmark_cli
from scripts.agent_orchestrate import _compiled, canonical_ledger_slug, main
from scripts.orchestration.bridge import BridgeError, NativeAgentRunBridge
from scripts.orchestration.journal import EventJournal, read_cancel_file, write_replaceable_manifest
from scripts.orchestration.plan import PlanValidationError, validate_plan
from scripts.orchestration.runtime import BenchmarkLiveRuntimeAdapter, AgentLedgerCLI, InMemoryLedger, OrchestrationRuntime, RuntimeErrorSafe, benchmark_live_adapter
from scripts.orchestration.scheduler import Scheduler


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "prompt.txt").write_text("safe prompt\n", encoding="utf-8")
    _git(repo, "add", "prompt.txt")
    _git(repo, "commit", "-qm", "base")
    return repo


def _plan(repo: Path) -> dict:
    return validate_plan({"version": 1, "run_id": "runtime-test", "repo_root": str(repo), "tasks": [{"id": "probe", "task_shape": "mechanical", "input_ref": "prompt.txt"}]})


def _writer_plan(repo: Path, *, acceptance: list[list[str]] | None = None) -> dict:
    base = _git(repo, "rev-parse", "HEAD")
    return validate_plan(
        {
            "version": 1,
            "run_id": "writer-failure-evidence",
            "repo_root": str(repo),
            "base_sha": base,
            "ledger_slug": "runtime-test",
            "tasks": [
                {
                    "id": "writer",
                    "task_shape": "mechanical",
                    "input_ref": "prompt.txt",
                    "workspace": {"kind": "isolated-writer", "own": ["output.txt"]},
                    "acceptance": acceptance or [],
                }
            ],
        }
    )


def _prepare_writer(runtime: OrchestrationRuntime, plan: dict, worktrees: Path) -> dict:
    task = plan["tasks"][0]
    runtime.prepare_resource(
        task,
        ownership={
            "created_by_run_id": plan["run_id"],
            "fencing_token": "fence-test",
            "path": str(worktrees / plan["run_id"] / "writer"),
            "branch": f"agent-run/{plan['run_id']}/writer",
            "base_sha": plan["base_sha"],
            "ledger_slug": plan["ledger_slug"],
            "generation": 1,
        },
    )
    return task


def test_runtime_resolves_only_repo_input_and_defaults_live_closed(tmp_path: Path):
    repo = _repo(tmp_path)
    runtime = OrchestrationRuntime(_plan(repo), artifact_root=tmp_path / "artifacts", worktree_root=tmp_path / "worktrees", ledger=InMemoryLedger())
    assert runtime._prompt(_plan(repo)["tasks"][0]) == "safe prompt\n"
    poisoned = dict(_plan(repo)["tasks"][0])
    poisoned["input_ref"] = "../outside.txt"
    with pytest.raises(RuntimeErrorSafe):
        runtime._prompt(poisoned)
    result = runtime.run_task(_plan(repo)["tasks"][0], run_id="runtime-test", attempt_id="attempt-1", generation=1)
    assert result["status"] == "failed-unsafe"
    assert result["failure_class"] == "live-provider-disabled"


def test_mechanical_projection_disables_managed_skill_bodies(tmp_path: Path):
    repo = _repo(tmp_path)
    plan = _plan(repo)
    runtime = OrchestrationRuntime(
        plan,
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
        ledger=InMemoryLedger(),
    )
    task = plan["tasks"][0]
    projected = runtime._project_task(
        task,
        cwd=repo,
        checkpoint="evt-mechanical",
        attempt_id="attempt-mechanical",
    )
    assert projected["disable_managed_skills"] is True

    bridge = NativeAgentRunBridge(artifact_root=tmp_path / "bridge-artifacts")
    command = bridge.build_command(projected, projected["prompt"])
    assert "--no-skills" in command


def test_runtime_prepares_private_read_only_review_bundle_from_persisted_results(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "review.txt").write_text("review the candidate\n", encoding="utf-8")
    plan = validate_plan(
        {
            "version": 1,
            "run_id": "review-seam",
            "repo_root": str(repo),
            "ledger_slug": "demo",
            "tasks": [
                {"id": "producer", "task_shape": "ordinary_bug_fix", "input_ref": "prompt.txt"},
                {
                    "id": "review",
                    "task_shape": "claude_final_review",
                    "input_ref": "review.txt",
                    "depends_on": ["producer"],
                    "reviewer_for": ["producer"],
                },
            ],
        }
    )
    answer = tmp_path / "answer.txt"
    answer.write_text("candidate answer\n", encoding="utf-8")
    answer.chmod(0o600)
    runtime = OrchestrationRuntime(
        plan,
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
        ledger=InMemoryLedger(),
    )
    integration = runtime.finalize_run(
        plan, {}, run_id="review-seam", generation=1, fencing_token="fence-1"
    )
    state = {
        "integration": integration,
        "tasks": {
            "producer": {
                "status": "succeeded",
                "result": {
                    "provider_run_id": "provider-run",
                    "provider": "codex",
                    "model_observed": "gpt-5.6-terra",
                    "model_family": "openai",
                    "session_id": "session-1",
                    "session_status": "attributed-stream-json",
                    "artifact_path": str(answer),
                    "artifact_sha256": __import__("hashlib").sha256(answer.read_bytes()).hexdigest(),
                },
            }
        },
    }
    review_task = next(task for task in plan["tasks"] if task["id"] == "review")
    prepared = runtime.prepare_review(
        review_task,
        state,
        run_id="review-seam",
        attempt_id="attempt-review",
        generation=1,
        fencing_token="fence-1",
    )
    bundle = Path(prepared["review_bundle_path"])
    assert bundle.stat().st_mode & 0o777 == 0o600
    body = json.loads(bundle.read_text())
    assert body["producers"][0]["run_id"] == "provider-run"
    assert body["producers"][0]["mode"] == "read-only"
    assert "provider" not in review_task and "session_id" not in review_task
    projected = runtime._project_task(
        review_task, cwd=repo, checkpoint="evt-review", attempt_id="attempt-review"
    )
    assert projected["disable_managed_skills"] is True
    assert projected["producer_review_bundle_sha256"] == prepared["review_bundle_sha256"]
    assert "Review the frozen candidate only" in projected["prompt"]
    assert projected["reviewer_for"] == ["producer"]
    assert "final non-empty line MUST be exactly AGENT_RUN_REVIEW_VERDICT: PASS or" in projected["prompt"]

    answer.write_text("drifted\n", encoding="utf-8")
    with pytest.raises(RuntimeErrorSafe, match="hash drift"):
        runtime.prepare_review(
            review_task,
            state,
            run_id="review-seam",
            attempt_id="attempt-review-2",
            generation=1,
            fencing_token="fence-1",
        )


def test_runtime_prepares_bounded_private_dependency_bundle_and_reverifies_dispatch(tmp_path: Path):
    repo = _repo(tmp_path)
    plan = validate_plan(
        {
            "version": 1,
            "run_id": "dependency-seam",
            "repo_root": str(repo),
            "tasks": [
                {
                    "id": "producer",
                    "task_shape": "mechanical",
                    "input_ref": "prompt.txt",
                    "result_contract": "analysis-v1",
                },
                {
                    "id": "consumer",
                    "task_shape": "mechanical",
                    "input_ref": "prompt.txt",
                    "depends_on": ["producer"],
                },
            ],
        }
    )
    artifact = tmp_path / "producer-answer.txt"
    artifact.write_text("safe result\n", encoding="utf-8")
    artifact.chmod(0o600)
    digest = __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
    semantic = tmp_path / "analysis-result.json"
    semantic.write_text(
        '{"decisions":[],"findings":[],"open_questions":[],"summary":"safe",'
        '"verification":[],"version":1}\n',
        encoding="utf-8",
    )
    semantic.chmod(0o600)
    semantic_digest = __import__("hashlib").sha256(semantic.read_bytes()).hexdigest()
    runtime = OrchestrationRuntime(
        plan,
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
        ledger=InMemoryLedger(),
    )
    consumer = next(task for task in plan["tasks"] if task["id"] == "consumer")
    prepared = runtime.prepare_dependencies(
        consumer,
        {
            "tasks": {
                "producer": {
                    "status": "succeeded",
                    "current_attempt_id": "producer-attempt",
                    "result": {
                        "status": "succeeded",
                        "provider_run_id": "provider-run",
                        "provider": "cursor",
                        "model_observed": "composer-2.5-fast",
                        "model_family": "cursor",
                        "session_id": "session-one",
                        "session_status": "attributed-stream-json",
                        "artifact_path": str(artifact),
                        "artifact_sha256": digest,
                        "analysis_result_path": str(semantic),
                        "analysis_result_sha256": semantic_digest,
                        "prompt": "must never propagate",
                    },
                }
            }
        },
        run_id="dependency-seam",
        attempt_id="consumer-attempt",
        generation=1,
        fencing_token="fence-one",
    )
    bundle = Path(prepared["dependency_bundle_path"])
    assert bundle.stat().st_mode & 0o777 == 0o600
    body = json.loads(bundle.read_text(encoding="utf-8"))
    assert body["dependencies"][0]["provider_run_id"] == "provider-run"
    assert body["dependencies"][0]["analysis_result_sha256"] == semantic_digest
    assert "prompt" not in bundle.read_text(encoding="utf-8")
    projected = runtime._project_task(
        consumer,
        cwd=repo,
        checkpoint="evt-dependency",
        attempt_id="consumer-attempt",
    )
    assert projected["dependency_bundle_sha256"] == prepared["dependency_bundle_sha256"]
    assert "Governed dependency input" in projected["prompt"]
    bundle.chmod(0o644)
    with pytest.raises(RuntimeErrorSafe, match="mode"):
        runtime._project_task(
            consumer,
            cwd=repo,
            checkpoint="evt-dependency",
            attempt_id="consumer-attempt",
        )


def test_benchmark_live_adapter_attests_checkout_but_blocks_missing_provider_evidence(tmp_path: Path):
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir(mode=0o700)
    adapter = benchmark_live_adapter(checkout_root=Path(__file__).resolve().parents[1])
    protocol = {
        "required_provider_families": ["openai", "cursor", "anthropic"]
    }
    observed = adapter.inspect_benchmark_live(protocol, evaluator_root=evaluator)
    assert all(observed["capabilities"].values())
    assert observed["orchestrator_entrypoint"] == str(
        Path(__file__).resolve().parents[1] / "scripts" / "agent_orchestrate.py"
    )
    assert {
        row["evidence_status"] for row in observed["preflight_evidence"].values()
    } == {"unknown-blocked"}
    with pytest.raises(RuntimeErrorSafe, match="compiler-produced lifecycle plan"):
        adapter.launch_benchmark_arm(
            object(), cell_root=tmp_path / "cell", reviewer={}, block_id="block"
        )


def test_benchmark_cli_preflight_is_launch_free_and_redacts_observed_detail(
    tmp_path: Path, monkeypatch, capsys
):
    """The preflight command must not create an output root or launch a cell."""

    prereg = tmp_path / "frozen.json"
    evaluator = tmp_path / "evaluator"
    evidence = tmp_path / "attested-evidence.json"
    output = tmp_path / "must-not-exist"
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        benchmark_cli,
        "load_preregistration",
        lambda _path: {"protocol": {"required_provider_families": ["cursor"]}},
    )
    monkeypatch.setattr(benchmark_cli, "verify_evaluator_root", lambda *_args: {"tasks": {}})
    monkeypatch.setattr(
        benchmark_cli,
        "_live_adapter",
        lambda *, evidence_path=None: seen.setdefault("evidence_path", evidence_path) or object(),
    )
    monkeypatch.setattr(
        benchmark_cli,
        "live_launch_preflight",
        lambda *_args, **_kwargs: {
            "eligible": False,
            "action": "block-live-before-first-cell",
            "blockers": [{"code": "whole-block-provider-evidence-missing", "detail": "token=must-not-leak"}],
            "pre_block_gate": {
                "eligible": False,
                "action": "postpone-whole-block",
                "reasons": [{"provider_family": "cursor", "reason": "provider-evidence-missing"}],
            },
            "config_fingerprint": "a" * 64,
            "raw_provider_payload": "token=must-not-leak",
        },
    )
    from scripts.orchestration import benchmark as benchmark_module

    monkeypatch.setattr(
        benchmark_module,
        "run_live_experiment",
        lambda *_args, **_kwargs: pytest.fail("preflight must never launch a live experiment"),
    )

    assert benchmark_cli.main([
        "preflight", "--prereg", str(prereg), "--evaluator-root", str(evaluator),
        "--preflight-evidence", str(evidence),
    ]) == 3
    assert seen["evidence_path"] == evidence
    assert not output.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["live"] is True
    rendered = json.dumps(payload)
    assert "token=must-not-leak" not in rendered
    assert "raw_provider_payload" not in payload


def test_benchmark_cli_live_run_passes_attested_evidence_to_runtime_adapter(
    tmp_path: Path, monkeypatch, capsys
):
    prereg, evaluator, evidence, output = (tmp_path / name for name in ("frozen.json", "evaluator", "evidence.json", "output"))
    seen: dict[str, object] = {}
    envelope = {"protocol": {"required_provider_families": ["cursor"]}, "frozen": True}
    monkeypatch.setattr(benchmark_cli, "load_preregistration", lambda _path: envelope)
    monkeypatch.setattr(benchmark_cli, "verify_evaluator_root", lambda *_args: {"tasks": {}})
    monkeypatch.setattr(
        benchmark_cli, "_live_adapter", lambda *, evidence_path=None: seen.setdefault("evidence_path", evidence_path) or object()
    )
    monkeypatch.setattr(
        benchmark_cli, "live_launch_preflight", lambda *_args, **_kwargs: {"eligible": True, "config_fingerprint": "a" * 64}
    )
    from scripts.orchestration import benchmark as benchmark_module

    monkeypatch.setattr(
        benchmark_module, "run_live_experiment", lambda *_args, **_kwargs: {"cell_count": 9}
    )

    assert benchmark_cli.main([
        "run", "--live", "--prereg", str(prereg), "--evaluator-root", str(evaluator),
        "--output-root", str(output), "--preflight-evidence", str(evidence),
    ]) == 0
    assert seen["evidence_path"] == evidence
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_cli_validate_start_status_and_collect_are_offline(tmp_path: Path, capsys):
    repo = _repo(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "cli-test",
                "repo_root": str(repo),
                "tasks": [
                    {
                        "id": "probe",
                        "task_shape": "mechanical",
                        "input_ref": "prompt.txt",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runs"
    assert main(["validate", str(plan_path)]) == 0
    assert main(["--runtime-root", str(runtime_root), "start", str(plan_path)]) == 0
    assert main(["--runtime-root", str(runtime_root), "status", "cli-test"]) == 0
    assert main(["--runtime-root", str(runtime_root), "collect", "cli-test"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[1]["status"] == "completed"
    assert (runtime_root / "cli-test" / "events.jsonl").stat().st_mode & 0o777 == 0o600


def test_live_runtime_writer_isolated_then_controller_joins(tmp_path: Path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    plan = validate_plan({
        "version": 1, "run_id": "writer-e2e", "repo_root": str(repo), "base_sha": base, "ledger_slug": "runtime-test",
        "integrated_acceptance": [["git", "status", "--porcelain"]],
        "tasks": [{"id": "writer", "task_shape": "mechanical", "input_ref": "prompt.txt", "workspace": {"kind": "isolated-writer", "own": ["output.txt"]}}],
    })

    class WritingBridge:
        def run_task(self, task, **_kwargs):
            Path(task["cwd"]).joinpath("output.txt").write_text("written\n", encoding="utf-8")
            return {"status": "succeeded", "failure_class": "none", "provider_run_id": "fake", "session_id": "fake-session", "artifact_path": "fake", "artifact_sha256": "0" * 64}

    worktrees = repo.parent / ".agent-run-worktrees"
    runtime = OrchestrationRuntime(plan, artifact_root=tmp_path / "artifacts", worktree_root=worktrees, bridge=WritingBridge(), ledger=InMemoryLedger(), live=True)
    journal = EventJournal(tmp_path / "events.jsonl", "writer-e2e")
    state = Scheduler(plan, runtime, journal, tmp_path / "controller.lock").run()
    assert state["status"] == "completed"
    integration = json.loads((tmp_path / "artifacts" / "writer-e2e" / "integration.json").read_text())
    assert integration["applied_task_ids"] == ["writer"]
    assert integration["acceptance_argv"] == [["git", "status", "--porcelain"]]
    assert integration["producer_acceptance_argv"] == {"writer": []}
    assert _git(Path(integration["integration_path"]), "show", "HEAD:output.txt") == "written"


def test_recoverable_writer_acceptance_failure_preserves_provider_evidence(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    plan = _writer_plan(repo, acceptance=[["false"]])
    wrapper = tmp_path / "provider.py"
    receipt = {
        "run_id": "provider-acceptance", "provider": "cursor",
        "seat": "claude-landing", "exit_code": 0, "failure_class": "none",
        "duration_ms": 12, "session_id": "session-acceptance",
        "session_status": "attributed-stream-json", "model": "composer-2.5-fast",
        "model_family": "cursor",
    }
    wrapper.write_text(
        "#!" + sys.executable + "\n"
        "from pathlib import Path\nimport json\n"
        "Path('output.txt').write_text('provider edit\\n', encoding='utf-8')\n"
        f"print(json.dumps({{'agent_run': {receipt!r}}}))\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    artifacts, worktrees = tmp_path / "artifacts", tmp_path / "worktrees"
    ledger = InMemoryLedger()
    runtime = OrchestrationRuntime(
        plan,
        artifact_root=artifacts,
        worktree_root=worktrees,
        bridge=NativeAgentRunBridge(artifact_root=artifacts, binary=str(wrapper)),
        ledger=ledger,
        live=True,
    )
    task = _prepare_writer(runtime, plan, worktrees)
    launched = runtime.launch_task(
        task, run_id=plan["run_id"], attempt_id="attempt", generation=1
    )
    result = runtime.collect_task(launched)

    assert result["status"] == "failed"
    assert result["failure_class"] == "acceptance-failed"
    assert result["detail"] == "AcceptanceFailure"
    assert result["provider_run_id"] == "provider-acceptance"
    assert result["provider"] == "cursor"
    assert result["model_observed"] == "composer-2.5-fast"
    assert result["model_family"] == "cursor"
    assert result["session_id"] == "session-acceptance"
    assert result["provider_duration_ms"] == 12
    assert Path(result["artifact_path"]).is_file()
    assert result["process_cleanup"]["residual"] is False
    assert ledger._lookup(launched.checkpoint_event)["state"] == "closed"
    state = {"tasks": {"writer": {"result": result}}}
    assert (
        BenchmarkLiveRuntimeAdapter._benchmark_failure_class(state)
        == "orchestration-infrastructure-failure"
    )
    assert (
        BenchmarkLiveRuntimeAdapter._benchmark_failure_class(
            state, trusted_acceptance_failure=True
        )
        == "task-quality-failure"
    )


def test_legacy_writer_scope_violation_stays_unsafe_but_preserves_evidence(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    plan = _writer_plan(repo)

    class ScopeViolatingBridge:
        def run_task(self, task, **_kwargs):
            Path(task["cwd"]).joinpath("outside.txt").write_text("nope\n")
            return {
                "status": "succeeded", "failure_class": "none",
                "provider_run_id": "provider-scope", "provider": "cursor",
                "model_observed": "composer-2.5-fast", "model_family": "cursor",
                "session_id": "session-scope", "session_status": "attributed-stream-json",
                "provider_duration_ms": 5, "artifact_path": "/private/artifact",
                "artifact_sha256": "a" * 64,
                "process_cleanup": {"residual": False},
            }

    worktrees = tmp_path / "worktrees"
    runtime = OrchestrationRuntime(
        plan, artifact_root=tmp_path / "artifacts", worktree_root=worktrees,
        bridge=ScopeViolatingBridge(), ledger=InMemoryLedger(), live=True,
    )
    task = _prepare_writer(runtime, plan, worktrees)
    result = runtime.run_task(
        task, run_id=plan["run_id"], attempt_id="attempt", generation=1
    )
    assert result["status"] == "failed-unsafe"
    assert result["failure_class"] == "runtime-safety-error"
    assert result["detail"] == "ScopeViolation"
    assert result["provider_run_id"] == "provider-scope"
    assert result["session_id"] == "session-scope"
    assert result["process_cleanup"] == {"residual": False}


def test_bridge_exception_before_result_remains_fail_closed(tmp_path: Path):
    repo = _repo(tmp_path)
    plan = _writer_plan(repo)

    class BrokenBridge:
        def run_task(self, _task, **_kwargs):
            raise BridgeError("receipt unavailable")

    worktrees = tmp_path / "worktrees"
    runtime = OrchestrationRuntime(
        plan, artifact_root=tmp_path / "artifacts", worktree_root=worktrees,
        bridge=BrokenBridge(), ledger=InMemoryLedger(), live=True,
    )
    task = _prepare_writer(runtime, plan, worktrees)
    result = runtime.run_task(
        task, run_id=plan["run_id"], attempt_id="attempt", generation=1
    )
    assert result == {
        "status": "failed-unsafe",
        "failure_class": "runtime-safety-error",
        "detail": "BridgeError",
    }


def test_writer_retry_stops_before_new_session_when_prior_attempt_left_dirty_worktree(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    plan = validate_plan(
        {
            "version": 1,
            "run_id": "writer-dirty-retry",
            "repo_root": str(repo),
            "base_sha": base,
            "ledger_slug": "runtime-test",
            "tasks": [
                {
                    "id": "writer",
                    "task_shape": "mechanical",
                    "input_ref": "prompt.txt",
                    "workspace": {
                        "kind": "isolated-writer",
                        "own": ["output.txt"],
                    },
                    "retry": {
                        "max_attempts": 2,
                        "retry_on": ["provider-transient"],
                    },
                }
            ],
        }
    )

    class DirtyFailingBridge:
        def __init__(self):
            self.calls = 0

        def run_task(self, task, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                Path(task["cwd"]).joinpath("output.txt").write_text(
                    "residual from first session\n", encoding="utf-8"
                )
                return {
                    "status": "failed",
                    "failure_class": "provider-transient",
                    "provider_run_id": "provider-first",
                    "session_id": "session-first",
                }
            return {
                "status": "succeeded",
                "failure_class": "none",
                "provider_run_id": "provider-second",
                "session_id": "session-second",
            }

    bridge = DirtyFailingBridge()
    worktrees = repo.parent / ".agent-run-worktrees"
    runtime = OrchestrationRuntime(
        plan,
        artifact_root=tmp_path / "artifacts",
        worktree_root=worktrees,
        bridge=bridge,
        ledger=InMemoryLedger(),
        live=True,
    )
    journal = EventJournal(tmp_path / "events-dirty-retry.jsonl", plan["run_id"])

    state = Scheduler(
        plan, runtime, journal, tmp_path / "controller-dirty-retry.lock"
    ).run()

    assert state["status"] == "failed-unsafe"
    assert state["tasks"]["writer"]["failure_class"] == "writer-retry-dirty-worktree"
    assert bridge.calls == 1
    assert not any(
        event["event_type"] == "dispatch_claimed"
        and event["attempt_id"] == state["tasks"]["writer"]["current_attempt_id"]
        for event in journal.read()
    )


def test_single_stage_runtime_reviewer_uses_full_projection_and_verdict_gate(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    (repo / "review.txt").write_text("review the producer\n", encoding="utf-8")
    _git(repo, "add", "review.txt")
    _git(repo, "commit", "-qm", "review input")
    plan = validate_plan(
        {
            "version": 1,
            "run_id": "legacy-review",
            "repo_root": str(repo),
            "ledger_slug": "runtime-test",
            "tasks": [
                {
                    "id": "producer",
                    "task_shape": "mechanical",
                    "input_ref": "prompt.txt",
                },
                {
                    "id": "review",
                    "task_shape": "claude_final_review",
                    "input_ref": "review.txt",
                    "depends_on": ["producer"],
                    "reviewer_for": ["producer"],
                },
            ],
        }
    )

    class LegacyBridge:
        def __init__(self):
            self.tasks = []

        def run_task(self, task, **_kwargs):
            projected = dict(task)
            self.tasks.append(projected)
            artifact = tmp_path / f"{task['id']}-answer.txt"
            artifact.write_text(
                "producer answer\n" if task["id"] == "producer" else "review findings\n",
                encoding="utf-8",
            )
            artifact.chmod(0o600)
            return {
                "status": "succeeded",
                "failure_class": "none",
                "provider_run_id": f"provider-{task['id']}",
                "provider": "cursor" if task["id"] == "producer" else "claude",
                "model_observed": (
                    "composer-2.5-fast" if task["id"] == "producer" else "claude-fable-5"
                ),
                "model_family": "cursor" if task["id"] == "producer" else "anthropic",
                "session_id": f"session-{task['id']}",
                "session_status": "attributed-stream-json",
                "artifact_path": str(artifact),
                "artifact_sha256": __import__("hashlib").sha256(
                    artifact.read_bytes()
                ).hexdigest(),
            }

    bridge = LegacyBridge()
    runtime = OrchestrationRuntime(
        plan,
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
        bridge=bridge,
        ledger=InMemoryLedger(),
        live=True,
    )
    state = Scheduler(
        plan,
        runtime,
        EventJournal(tmp_path / "legacy-review.jsonl", plan["run_id"]),
        tmp_path / "controller-legacy-review.lock",
    ).run()

    review = next(task for task in bridge.tasks if task["id"] == "review")
    assert review["reviewer_for"] == ["producer"]
    assert review["disable_managed_skills"] is True
    assert Path(review["producer_review_bundle"]).stat().st_mode & 0o777 == 0o600
    assert "AGENT_RUN_REVIEW_VERDICT" in review["prompt"]
    assert state["status"] == "failed"
    assert state["tasks"]["review"]["failure_class"] == "review-verdict-missing"


def test_writer_candidate_and_integration_resume_then_safe_terminal_cleanup(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "review.txt").write_text("review\n", encoding="utf-8")
    _git(repo, "add", "review.txt")
    _git(repo, "commit", "-qm", "review input")
    base = _git(repo, "rev-parse", "HEAD")
    plan = validate_plan(
        {
            "version": 1,
            "run_id": "resume-cleanup",
            "repo_root": str(repo),
            "base_sha": base,
            "ledger_slug": "runtime-test",
            "tasks": [
                {
                    "id": "writer",
                    "task_shape": "mechanical",
                    "input_ref": "prompt.txt",
                    "workspace": {
                        "kind": "isolated-writer",
                        "own": ["output.txt"],
                    },
                },
                {
                    "id": "review",
                    "task_shape": "claude_final_review",
                    "input_ref": "review.txt",
                    "depends_on": ["writer"],
                    "reviewer_for": ["writer"],
                },
            ],
        }
    )

    producer_artifact = tmp_path / "producer-artifact.txt"
    producer_artifact.write_text("attributed result\n", encoding="utf-8")
    producer_artifact.chmod(0o600)
    producer_digest = __import__("hashlib").sha256(
        producer_artifact.read_bytes()
    ).hexdigest()

    class WritingBridge:
        def run_task(self, task, **_kwargs):
            Path(task["cwd"]).joinpath("output.txt").write_text(
                "written\n", encoding="utf-8"
            )
            return {
                "status": "succeeded",
                "failure_class": "none",
                "provider_run_id": "provider-writer",
                "provider": "cursor",
                "model_observed": "composer-2.5-fast",
                "model_family": "cursor",
                "session_id": "session-writer",
                "session_status": "attributed-stream-json",
                "artifact_path": str(producer_artifact),
                "artifact_sha256": producer_digest,
            }

    artifacts = tmp_path / "artifacts"
    worktrees = repo.parent / ".agent-run-worktrees"
    first = OrchestrationRuntime(
        plan,
        artifact_root=artifacts,
        worktree_root=worktrees,
        bridge=WritingBridge(),
        ledger=InMemoryLedger(),
        live=True,
    )
    writer = next(task for task in plan["tasks"] if task["id"] == "writer")
    path = worktrees / "resume-cleanup" / "writer"
    first.prepare_resource(
        writer,
        ownership={
            "created_by_run_id": "resume-cleanup",
            "fencing_token": "fence-old",
            "path": str(path),
            "branch": "agent-run/resume-cleanup/writer",
            "base_sha": base,
            "ledger_slug": "runtime-test",
            "generation": 1,
        },
    )
    result = first.run_task(
        writer,
        run_id="resume-cleanup",
        attempt_id="writer-attempt",
        generation=1,
    )
    assert result["status"] == "succeeded"
    assert path.exists()

    resumed = OrchestrationRuntime(
        plan,
        artifact_root=artifacts,
        worktree_root=worktrees,
        ledger=InMemoryLedger(),
        live=True,
    )
    integration = resumed.finalize_run(
        plan,
        {"tasks": {"writer": {"status": "succeeded", "result": result}}},
        run_id="resume-cleanup",
        generation=2,
        fencing_token="fence-new",
    )
    assert integration["status"] == "succeeded"
    integration_path = Path(integration["integration_path"])
    assert _git(integration_path, "show", "HEAD:output.txt") == "written"
    review = next(task for task in plan["tasks"] if task["id"] == "review")
    resumed_state = {
        "tasks": {
            "writer": {"status": "succeeded", "result": result},
        },
        "integration": integration,
    }
    review_context = resumed.prepare_review(
        review,
        resumed_state,
        run_id="resume-cleanup",
        attempt_id="review-attempt",
        generation=2,
        fencing_token="fence-new",
    )
    assert review_context["status"] == "succeeded"
    projected_review = resumed._project_task(
        review,
        cwd=integration_path,
        checkpoint="evt-review-resume",
        attempt_id="review-attempt",
    )
    assert projected_review["cwd"] == str(integration_path)
    assert "Review the frozen candidate only" in projected_review["prompt"]
    outcomes = resumed.terminal_cleanup(
        plan,
        {
            "tasks": {
                "writer": {"status": "succeeded", "result": result},
                "review": {"status": "succeeded", "result": {"status": "succeeded"}},
            },
            "integration": integration,
        },
        run_id="resume-cleanup",
        generation=2,
        fencing_token="fence-new",
    )
    assert outcomes["writer"]["worktree"] == {"status": "succeeded"}
    assert outcomes["integration"]["worktree"] == {"status": "succeeded"}
    assert not path.exists() and not integration_path.exists()
    assert _git(repo, "show-ref", "--verify", "refs/heads/agent-run/resume-cleanup/writer")
    assert _git(repo, "show-ref", "--verify", "refs/heads/agent-run/resume-cleanup/integration")


def test_writer_resume_preserves_and_fails_unsafe_on_git_drift(tmp_path: Path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    plan = validate_plan(
        {
            "version": 1,
            "run_id": "resume-drift",
            "repo_root": str(repo),
            "base_sha": base,
            "ledger_slug": "runtime-test",
            "tasks": [
                {
                    "id": "writer",
                    "task_shape": "mechanical",
                    "input_ref": "prompt.txt",
                    "workspace": {"kind": "isolated-writer", "own": ["output.txt"]},
                }
            ],
        }
    )

    class WritingBridge:
        def run_task(self, task, **_kwargs):
            Path(task["cwd"]).joinpath("output.txt").write_text("ok\n")
            return {"status": "succeeded"}

    artifacts = tmp_path / "artifacts"
    worktrees = repo.parent / ".agent-run-worktrees"
    runtime = OrchestrationRuntime(
        plan, artifact_root=artifacts, worktree_root=worktrees,
        bridge=WritingBridge(), ledger=InMemoryLedger(), live=True,
    )
    writer = plan["tasks"][0]
    path = worktrees / "resume-drift" / "writer"
    runtime.prepare_resource(
        writer,
        ownership={
            "created_by_run_id": "resume-drift", "fencing_token": "fence-old",
            "path": str(path), "branch": "agent-run/resume-drift/writer",
            "base_sha": base, "ledger_slug": "runtime-test", "generation": 1,
        },
    )
    result = runtime.run_task(
        writer, run_id="resume-drift", attempt_id="attempt", generation=1
    )
    assert result["status"] == "succeeded"
    (path / "output.txt").write_text("drifted\n", encoding="utf-8")
    resumed = OrchestrationRuntime(
        plan, artifact_root=artifacts, worktree_root=worktrees,
        ledger=InMemoryLedger(), live=True,
    )
    integration = resumed.finalize_run(
        plan, {"tasks": {"writer": {"status": "succeeded", "result": result}}},
        run_id="resume-drift", generation=2, fencing_token="fence-new",
    )
    assert integration == {
        "status": "failed-unsafe",
        "failure_class": "writer-candidate-resume-unreconciled",
    }
    assert path.exists()


def test_checkpoint_cli_accepts_only_an_attributed_event_id(tmp_path: Path):
    repo = _repo(tmp_path)
    seen = []
    class Completed:
        returncode = 0
        stdout = "evt-20260718T123456.123456Z-codex-landing\n"
        stderr = ""

    def runner(argv, **_kwargs):
        seen.append(argv)
        return Completed()
    ledger = AgentLedgerCLI(slug="runtime-test", intent_ref="docs/intents/x.md", repo_root=repo, runner=runner)
    event = ledger.open(task=_plan(repo)["tasks"][0], cwd=repo, run_id="run", attempt_id="attempt")
    assert event.startswith("evt-")
    ledger.claim(event)
    ledger.close(event, outcome="done")
    assert [call[1] for call in seen] == ["open", "claim", "close"]
    assert seen[0][seen[0].index("--from-seat") + 1] == "codex-orchestrator"
    assert seen[0][seen[0].index("--to-seat") + 1] == "claude-landing"
    assert seen[1][seen[1].index("--seat") + 1] == "claude-landing"
    assert seen[2][seen[2].index("--seat") + 1] == "claude-landing"


def test_checkpoint_seat_is_exact_compiled_binding_and_cannot_be_overridden(tmp_path: Path):
    repo = _repo(tmp_path)
    ordinary = validate_plan({"version": 1, "repo_root": str(repo), "tasks": [{"id": "bug", "task_shape": "ordinary_bug_fix", "input_ref": "prompt.txt"}]})["tasks"][0]
    assert ordinary["binding"]["seat"] == "codex-landing"
    mechanical = _plan(repo)["tasks"][0]
    assert mechanical["binding"]["seat"] == "claude-landing"

    with pytest.raises(PlanValidationError):
        validate_plan({"version": 1, "repo_root": str(repo), "tasks": [{"id": "probe", "task_shape": "mechanical", "input_ref": "prompt.txt", "metadata": {"seat": "codex-final-review"}}]})

    malicious = dict(mechanical)
    malicious["seat"] = "codex-final-review"
    ledger = AgentLedgerCLI(slug="runtime-test", intent_ref="docs/intents/x.md", repo_root=repo, runner=lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeErrorSafe, match="may not override"):
        ledger.open(task=malicious, cwd=repo, run_id="run", attempt_id="attempt")


def test_live_resume_without_durable_manifests_fails_unsafe_without_replay(tmp_path: Path):
    repo = _repo(tmp_path)
    runtime = OrchestrationRuntime(_plan(repo), artifact_root=tmp_path / "artifacts", worktree_root=tmp_path / "worktrees", ledger=InMemoryLedger(), live=True)
    task = _plan(repo)["tasks"][0]
    task_result = runtime.reconcile_task(task, run_id="runtime-test", attempt_id="attempt", generation=2, prior_state={"status": "running"})
    resource_result = runtime.reconcile_resource(task, ownership={})
    assert task_result == {"status": "failed-unsafe", "failure_class": "checkpoint-or-process-resume-unreconciled", "replayed": False}
    assert resource_result == {"status": "failed-unsafe", "failure_class": "resource-resume-manifest-unreconciled", "replayed": False}


def test_crash_resume_closes_same_checkpoint_from_private_attempt_manifest(tmp_path: Path):
    repo = _repo(tmp_path)
    plan = _plan(repo)
    task = plan["tasks"][0]
    wrapper = tmp_path / "agent-run-fixture.py"
    receipt = {
        "run_id": "provider-recovered", "provider": "cursor",
        "seat": "claude-landing", "exit_code": 0, "failure_class": "none",
        "duration_ms": 1, "session_id": "session-recovered",
        "session_status": "attributed-stream-json", "model": "composer-2.5-fast",
    }
    wrapper.write_text(
        f"#!{sys.executable}\nimport json\nprint(json.dumps({{'agent_run': {receipt!r}}}))\nprint('private')\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    artifacts = tmp_path / "artifacts"
    ledger = InMemoryLedger()
    first = OrchestrationRuntime(
        plan,
        artifact_root=artifacts,
        worktree_root=tmp_path / "worktrees",
        bridge=NativeAgentRunBridge(artifact_root=artifacts, binary=str(wrapper)),
        ledger=ledger,
        live=True,
    )
    launched = first.launch_task(
        task,
        run_id="runtime-test",
        attempt_id="attempt-stable",
        generation=1,
    )
    checkpoint = launched.checkpoint_event
    assert launched.bridge_launch.process.wait(timeout=2) == 0
    assert ledger._lookup(checkpoint)["state"] == "claimed"

    resumed = OrchestrationRuntime(
        plan,
        artifact_root=artifacts,
        worktree_root=tmp_path / "worktrees-resumed",
        bridge=NativeAgentRunBridge(artifact_root=artifacts, binary=str(wrapper)),
        ledger=ledger,
        live=True,
    )
    result = resumed.reconcile_task(
        task,
        run_id="runtime-test",
        attempt_id="attempt-stable",
        generation=2,
        prior_state={"status": "dispatch-intent"},
    )
    assert result["status"] == "succeeded"
    assert result["replayed"] is False
    assert ledger._lookup(checkpoint)["state"] == "closed"
    manifest = json.loads(launched.bridge_launch.manifest_path.read_text())
    assert manifest["checkpoint_event"] == checkpoint
    assert manifest["compiled_seat"] == task["binding"]["seat"]
    assert "prompt" not in manifest and "command" not in manifest


def test_crash_resume_checkpoint_seat_drift_fails_unsafe_and_does_not_close(tmp_path: Path):
    repo = _repo(tmp_path)
    plan = _plan(repo)
    task = plan["tasks"][0]
    wrapper = tmp_path / "agent-run-fixture.py"
    receipt = {
        "run_id": "provider-drift", "provider": "cursor",
        "seat": "claude-landing", "exit_code": 0, "failure_class": "none",
        "session_id": "session-drift", "session_status": "attributed-stream-json",
        "model": "composer-2.5-fast",
    }
    wrapper.write_text(
        f"#!{sys.executable}\nimport json\nprint(json.dumps({{'agent_run': {receipt!r}}}))\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    artifacts = tmp_path / "artifacts"
    ledger = InMemoryLedger()
    runtime = OrchestrationRuntime(
        plan, artifact_root=artifacts, worktree_root=tmp_path / "worktrees",
        bridge=NativeAgentRunBridge(artifact_root=artifacts, binary=str(wrapper)),
        ledger=ledger, live=True,
    )
    launched = runtime.launch_task(
        task, run_id="runtime-test", attempt_id="attempt-drift", generation=1
    )
    assert launched.bridge_launch.process.wait(timeout=2) == 0
    manifest = json.loads(launched.bridge_launch.manifest_path.read_text())
    manifest["compiled_seat"] = "codex-final-review"
    launched.bridge_launch.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = runtime.reconcile_task(
        task, run_id="runtime-test", attempt_id="attempt-drift", generation=2,
        prior_state={"status": "running"},
    )
    assert result["status"] == "failed-unsafe"
    assert result["failure_class"] == "checkpoint-or-process-resume-unreconciled"
    assert ledger._lookup(launched.checkpoint_event)["state"] == "claimed"


def test_cli_cancel_writes_fenced_request_not_terminal_event(tmp_path: Path, capsys):
    repo = _repo(tmp_path)
    plan = _plan(repo)
    runtime_root = tmp_path / "runs"
    directory = runtime_root / "runtime-test"
    directory.mkdir(parents=True)
    write_replaceable_manifest(directory / "plan.json", plan)
    journal = EventJournal(directory / "events.jsonl", "runtime-test")
    journal.append("controller_acquired", attempt_id="controller", generation=1, fencing_token="fence-current", payload={"action": "start"})
    assert main(["--runtime-root", str(runtime_root), "cancel", "runtime-test"]) == 0
    request = read_cancel_file(directory / "events.jsonl.cancel-request.json")
    assert request["run_id"] == "runtime-test"
    assert request["generation"] == 1
    assert request["fencing_token"] == "fence-current"
    assert (fold_status := json.loads(capsys.readouterr().out))
    assert fold_status["status"] == "cancel-requested"


def test_linked_worktree_uses_source_canonical_ledger_slug(tmp_path: Path):
    source_parent = tmp_path / "source-parent"
    source_parent.mkdir()
    source = _repo(source_parent)
    (source / ".agents").mkdir()
    (source / ".agents" / "ledger-slug").write_text(
        "canonical-project\n", encoding="utf-8"
    )
    _git(source, "add", ".agents/ledger-slug")
    _git(source, "commit", "-qm", "canonical ledger identity")
    linked = tmp_path / "misleading-linked-worktree"
    _git(source, "worktree", "add", "-q", "-b", "linked-test", str(linked), "HEAD")
    plan_path = tmp_path / "linked-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "version": 1,
                "repo_root": str(linked),
                "tasks": [
                    {
                        "id": "probe",
                        "task_shape": "mechanical",
                        "input_ref": "prompt.txt",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert canonical_ledger_slug(linked) == "canonical-project"
    assert _compiled(plan_path)["ledger_slug"] == "canonical-project"

    mismatched = json.loads(plan_path.read_text(encoding="utf-8"))
    mismatched["ledger_slug"] = "wrong-declared-slug"
    plan_path.write_text(json.dumps(mismatched), encoding="utf-8")
    with pytest.raises(ValueError, match="plan ledger_slug conflicts"):
        _compiled(plan_path)

    (linked / ".agents" / "ledger-slug").write_text(
        "wrong-worktree-slug\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="conflicts"):
        canonical_ledger_slug(linked)


def test_evaluator_root_is_explicit_private_and_stable_across_resume(
    tmp_path: Path, capsys
):
    repo = _repo(tmp_path)
    plan_path = tmp_path / "evaluator-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "evaluator-test",
                "repo_root": str(repo),
                "tasks": [
                    {
                        "id": "probe",
                        "task_shape": "mechanical",
                        "input_ref": "evaluator:hidden-case",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runs"
    assert main(
        [
            "--runtime-root",
            str(runtime_root),
            "start",
            str(plan_path),
        ]
    ) == 2
    assert not (runtime_root / "evaluator-test").exists()

    # Use a distinct accepted run so the two outcomes remain easy to inspect.
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    raw["run_id"] = "evaluator-approved"
    plan_path.write_text(json.dumps(raw), encoding="utf-8")
    evaluator = tmp_path / "private-evaluator"
    evaluator.mkdir(mode=0o700)
    (evaluator / "hidden-case.txt").write_text("private body\n", encoding="utf-8")
    assert main(
        [
            "--runtime-root",
            str(runtime_root),
            "start",
            str(plan_path),
            "--evaluator-root",
            str(evaluator),
        ]
    ) == 0
    manifest_path = runtime_root / "evaluator-approved" / "evaluator-root.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["path"] == str(evaluator.resolve())
    assert "private body" not in manifest_path.read_text(encoding="utf-8")
    assert manifest_path.stat().st_mode & 0o777 == 0o600

    assert main(
        [
            "--runtime-root",
            str(runtime_root),
            "resume",
            "evaluator-approved",
        ]
    ) == 2
    assert main(
        [
            "--runtime-root",
            str(runtime_root),
            "resume",
            "evaluator-approved",
            "--evaluator-root",
            str(evaluator),
        ]
    ) == 0
    capsys.readouterr()


def test_evaluator_root_rejects_non_private_mode(tmp_path: Path):
    root = tmp_path / "not-private"
    root.mkdir(mode=0o755)
    repo = _repo(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "version": 1,
                "repo_root": str(repo),
                "tasks": [
                    {
                        "id": "probe",
                        "task_shape": "mechanical",
                        "input_ref": "evaluator:case",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(
        [
            "--runtime-root",
            str(tmp_path / "runs"),
            "start",
            str(plan_path),
            "--evaluator-root",
            str(root),
        ]
    ) == 2


def test_start_bundle_publish_is_atomic_if_manifest_write_fails(
    tmp_path: Path, monkeypatch
):
    repo = _repo(tmp_path)
    evaluator = tmp_path / "private-evaluator"
    evaluator.mkdir(mode=0o700)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "atomic-start",
                "repo_root": str(repo),
                "tasks": [
                    {
                        "id": "probe",
                        "task_shape": "mechanical",
                        "input_ref": "evaluator:case",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original = orchestrate_cli.write_replaceable_manifest

    def fail_evaluator_manifest(path, value):
        if path.name == "evaluator-root.json":
            raise OSError("injected manifest failure")
        return original(path, value)

    monkeypatch.setattr(
        orchestrate_cli, "write_replaceable_manifest", fail_evaluator_manifest
    )
    runtime_root = tmp_path / "runs"
    assert main(
        [
            "--runtime-root",
            str(runtime_root),
            "start",
            str(plan_path),
            "--evaluator-root",
            str(evaluator),
        ]
    ) == 2
    assert not (runtime_root / "atomic-start").exists()
