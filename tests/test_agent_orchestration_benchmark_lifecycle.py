from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import time
import hashlib

import pytest

from scripts.orchestration.benchmark import LaunchContract, sha256_value
from scripts.orchestration.benchmark_lifecycle import (
    BenchmarkLifecycleError,
    compile_lifecycle_launch,
    run_manual_ready_sets,
)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    return repo, subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def _private(repo: Path) -> tuple[dict, dict, dict]:
    runbook = {"ready_sets": [["writer-a", "writer-b"]]}
    review_body = "Read the integrated result only and report findings."
    reviewer = {"route": "final_review", "model": "grok-4.5", "effort": "high", "family": "xai", "independence": "cross_family", "prompt_sha256": sha256_value(review_body), "timeout_seconds": 900}
    def node(ident: str, own: str, shape: str = "ordinary_bug_fix") -> dict:
        return {
            "id": ident,
            "task_shape": shape,
            "depends_on": [],
            "workspace": {"kind": "isolated-writer", "own": [own], "do_not_touch": ["README.md"]},
            "prompt_body": f"Change only {own}.",
            "acceptance_argv": [["git", "status", "--porcelain"]],
        }
    writer_a = node("writer-a", "a.txt")
    writer_b = node("writer-b", "b.txt", "standard_feature")
    graph = {"nodes": [
        {"id": writer_a["id"], "task_shape": writer_a["task_shape"], "depends_on": [], "prompt_sha256": sha256_value(writer_a["prompt_body"]), "acceptance_sha256": sha256_value(writer_a["acceptance_argv"])},
        {"id": writer_b["id"], "task_shape": writer_b["task_shape"], "depends_on": [], "prompt_sha256": sha256_value(writer_b["prompt_body"]), "acceptance_sha256": sha256_value(writer_b["acceptance_argv"])},
    ]}
    private = {
        "graph": graph,
        "manual_runbook": runbook,
        "lifecycle": {
            "fixture_repo_root": str(repo),
            "single_producer": node("single", "single.txt"),
            "nodes": [writer_a, writer_b],
            "review": {"id": "review", "prompt_body": review_body},
            "integrated_acceptance": [["git", "status", "--porcelain"]],
        },
    }
    return private, reviewer, graph


def _contract(arm: str, base: str, private: dict, graph: dict) -> LaunchContract:
    return LaunchContract(
        task_id="fixture",
        arm=arm,
        launcher_kind={"A": "single-native-producer", "B": "manual-event-fanout", "C": "automatic-orchestrator"}[arm],
        payload={"base_commit": base, "task_shape": "ordinary_bug_fix", "prompt_sha256": sha256_value(private["lifecycle"]["single_producer"]["prompt_body"]), "writer_limit": 2, "deadline_seconds": 300, "acceptance_commands": ["git status --porcelain"], "route_policy_sha256": hashlib.sha256((Path(__file__).parents[1] / "routing-policy.yaml").read_bytes()).hexdigest()},
        graph_sha256=sha256_value(graph),
        manual_runbook_sha256=sha256_value(private["manual_runbook"]),
    )


def test_compiler_materializes_private_prompts_and_preserves_arm_structure(tmp_path: Path):
    repo, base = _repo(tmp_path)
    private, reviewer, graph = _private(repo)
    launches = {
        arm: compile_lifecycle_launch(_contract(arm, base, private, graph), private, reviewer=reviewer, cell_root=tmp_path / arm)
        for arm in ("A", "B", "C")
    }
    assert [task["id"] for task in launches["A"].plan["tasks"]] == ["single", "review"]
    assert {task["id"] for task in launches["B"].plan["tasks"]} == {"writer-a", "writer-b", "review"}
    assert launches["B"].manual_runbook is not None
    assert launches["C"].manual_runbook is None
    for launch in launches.values():
        assert os.stat(launch.evaluator_root).st_mode & 0o777 == 0o700
        for task in launch.plan["tasks"]:
            path = launch.evaluator_root / f"{task['id']}.txt"
            assert os.stat(path).st_mode & 0o777 == 0o600
            assert task["input_ref"] == f"evaluator:{task['id']}"


@pytest.mark.parametrize("mutator, match", [
    (lambda private: private["lifecycle"].update({"model": "forbidden"}), "forbidden authority"),
    (lambda private: private["lifecycle"]["nodes"][0].pop("workspace"), "workspace"),
    (lambda private: private["lifecycle"]["review"].update({"prompt_body": "drift"}), "review prompt hash drift"),
])
def test_compiler_fails_closed_for_authority_scope_and_review_drift(tmp_path: Path, mutator, match: str):
    repo, base = _repo(tmp_path)
    private, reviewer, graph = _private(repo)
    mutator(private)
    with pytest.raises(BenchmarkLifecycleError, match=match):
        compile_lifecycle_launch(_contract("B", base, private, graph), private, reviewer=reviewer, cell_root=tmp_path / "cell")


def test_compiler_requires_exact_public_reviewer_binding_and_clean_top_level(tmp_path: Path):
    repo, base = _repo(tmp_path)
    private, reviewer, graph = _private(repo)
    reviewer["model"] = "different"
    with pytest.raises(BenchmarkLifecycleError, match="compiled reviewer binding"):
        compile_lifecycle_launch(_contract("B", base, private, graph), private, reviewer=reviewer, cell_root=tmp_path / "bad-review")
    private, reviewer, graph = _private(repo)
    (repo / "dirty.txt").write_text("not clean\n", encoding="utf-8")
    with pytest.raises(BenchmarkLifecycleError, match="clean worktree"):
        compile_lifecycle_launch(_contract("B", base, private, graph), private, reviewer=reviewer, cell_root=tmp_path / "dirty")


def test_compiler_rejects_lifecycle_graph_projection_drift(tmp_path: Path):
    repo, base = _repo(tmp_path)
    private, reviewer, graph = _private(repo)
    private["lifecycle"]["nodes"][0]["task_shape"] = "standard_feature"
    with pytest.raises(BenchmarkLifecycleError, match="graph projection drift"):
        compile_lifecycle_launch(_contract("B", base, private, graph), private, reviewer=reviewer, cell_root=tmp_path / "cell")


def test_compiler_binds_per_writer_acceptance_in_frozen_graph(tmp_path: Path):
    repo, base = _repo(tmp_path)
    private, reviewer, graph = _private(repo)
    writer_acceptance = [["git", "diff", "--check"]]
    private["lifecycle"]["nodes"][0]["acceptance_argv"] = writer_acceptance
    graph["nodes"][0]["acceptance_sha256"] = sha256_value(writer_acceptance)

    launch = compile_lifecycle_launch(
        _contract("B", base, private, graph),
        private,
        reviewer=reviewer,
        cell_root=tmp_path / "bounded",
    )
    writer = next(task for task in launch.plan["tasks"] if task["id"] == "writer-a")
    assert writer["acceptance"] == writer_acceptance
    assert launch.plan["integrated_acceptance"] == [["git", "status", "--porcelain"]]

    private["lifecycle"]["nodes"][0]["acceptance_argv"] = [["git", "diff", "--stat"]]
    with pytest.raises(BenchmarkLifecycleError, match="graph projection drift"):
        compile_lifecycle_launch(
            _contract("B", base, private, graph),
            private,
            reviewer=reviewer,
            cell_root=tmp_path / "drift",
        )


class _ManualLifecycle:
    def __init__(self, *, fail: str | None = None, failure_class: str = "failed-unsafe"):
        self.calls: list[str] = []
        self.fail = fail
        self.failure_class = failure_class
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def prepare_resource(self, task, **_kwargs):
        self.calls.append(f"resource:{task['id']}")
        return {"status": "created"}

    def launch_task(self, task, **_kwargs):
        self.calls.append(f"launch:{task['id']}")
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        return task

    def collect_task(self, launched):
        time.sleep(0.03)
        self.calls.append(f"collect:{launched['id']}")
        with self.lock:
            self.active -= 1
        if self.fail == launched["id"]:
            return {
                "status": "failed" if self.failure_class == "acceptance-failed" else "failed-unsafe",
                "failure_class": self.failure_class,
            }
        return {"status": "succeeded"}

    def finalize_run(self, _plan, _state, **_kwargs):
        self.calls.append("integration")
        return {"status": "succeeded"}

    def prepare_review(self, _task, _state, **_kwargs):
        self.calls.append("prepare-review")
        return {"status": "succeeded"}


def test_b_manual_ready_set_never_uses_scheduler_and_preserves_partial_failure(tmp_path: Path):
    repo, base = _repo(tmp_path)
    private, reviewer, graph = _private(repo)
    launch = compile_lifecycle_launch(_contract("B", base, private, graph), private, reviewer=reviewer, cell_root=tmp_path / "cell")
    lifecycle = _ManualLifecycle()
    events: list[dict] = []
    outcome = run_manual_ready_sets(launch, lifecycle, event_sink=events.append)
    assert outcome["status"] == "succeeded"
    assert "resource:writer-a" in lifecycle.calls and "resource:writer-b" in lifecycle.calls
    assert lifecycle.calls.index("integration") < lifecycle.calls.index("prepare-review") < lifecycle.calls.index("launch:review")
    assert lifecycle.max_active == 2
    names = [event["event"] for event in events]
    assert names[0] == "coordination_started"
    assert names.count("producer_started") == 2
    assert names.index("coordination_completed") < names.index("candidate_created")
    assert names[-2:] == ["review_started", "review_completed"]
    interval_events = [
        event
        for event in events
        if event["event"]
        in {
            "coordination_started",
            "coordination_completed",
            "review_started",
            "review_completed",
        }
    ]
    assert all(
        event["interval_id"].startswith("interval-")
        for event in interval_events
    )
    failed = _ManualLifecycle(fail="writer-b")
    partial_events: list[dict] = []
    partial = run_manual_ready_sets(launch, failed, event_sink=partial_events.append)
    assert partial["status"] == "partial-failure"
    assert "integration" not in failed.calls and "prepare-review" not in failed.calls
    assert "acceptance_completed" not in [event["event"] for event in partial_events]

    acceptance_failed = _ManualLifecycle(
        fail="writer-b", failure_class="acceptance-failed"
    )
    acceptance_events: list[dict] = []
    accepted_partial = run_manual_ready_sets(
        launch, acceptance_failed, event_sink=acceptance_events.append
    )
    assert accepted_partial["status"] == "partial-failure"
    assert [
        event for event in acceptance_events if event["event"] == "acceptance_completed"
    ] == [{
        "event": "acceptance_completed",
        "at": pytest.approx(acceptance_events[-2]["at"]),
        "task_id": "writer-b",
        "accepted": False,
        "failure_class": "acceptance-failed",
    }]
