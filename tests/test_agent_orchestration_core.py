from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time

import pytest
import yaml

from scripts.orchestration.journal import (
    ControllerLease,
    EventJournal,
    JournalError,
    fold_events,
    read_cancel_file,
    reconcile_identity,
    request_cancel_file,
)
from scripts.orchestration.plan import PlanValidationError, load_plan, validate_plan
from scripts.orchestration.scheduler import FakeAdapter, FakeClock, Scheduler


def raw_plan(tmp_path: Path, tasks: list[dict] | None = None, **extra) -> dict:
    value = {
        "version": 1,
        "run_id": "test-run",
        "repo_root": str(tmp_path),
        "base_sha": "a" * 40,
        "ledger_slug": "test-ledger",
        "tasks": tasks or [{"id": "one", "task_shape": "ordinary_bug_fix"}],
    }
    value.update(extra)
    return value


def compiled(tmp_path: Path, tasks: list[dict] | None = None, **extra) -> dict:
    return validate_plan(raw_plan(tmp_path, tasks, **extra))


def event_journal(tmp_path: Path, run_id: str = "test-run") -> EventJournal:
    return EventJournal(tmp_path / f"{run_id}.jsonl", run_id)


def append_controller(
    journal: EventJournal, generation: int = 1, token: str = "fence-1"
):
    return journal.append(
        "controller_acquired",
        attempt_id="run-attempt",
        generation=generation,
        fencing_token=token,
        payload={"action": "start"},
        event_id=f"controller-{generation}",
        timestamp=f"2026-01-01T00:00:0{generation}.000Z",
    )


def test_plan_compiles_governed_route_and_permission_projection(tmp_path):
    plan = compiled(
        tmp_path,
        [
            {"id": "inspect", "task_shape": "ordinary_bug_fix"},
            {
                "id": "write",
                "task_shape": "standard_feature",
                "depends_on": ["inspect"],
                "workspace": {
                    "kind": "isolated-writer",
                    "own": ["scripts/orchestration"],
                    "shared_interface_paths": ["scripts/orchestration/__init__.py"],
                },
                "acceptance": [["python3", "-m", "pytest", "-q"]],
            },
        ],
    )
    assert plan["topological_order"] == ["inspect", "write"]
    write = plan["tasks"][1]
    assert write["binding"]["provider"] == "claude"
    assert write["permission_projection"] == {
        "execution_mode": "execute",
        "permission_profile": "workspace-write",
    }
    assert "model" not in raw_plan(tmp_path)["tasks"][0]


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_load_plan_supports_versioned_json_and_yaml(tmp_path, suffix):
    path = tmp_path / f"plan{suffix}"
    value = raw_plan(tmp_path)
    path.write_text(json.dumps(value) if suffix == ".json" else yaml.safe_dump(value))
    assert load_plan(path)["version"] == 1


@pytest.mark.parametrize(
    "mutator,match",
    [
        (lambda p: p.update(version=2), "version"),
        (lambda p: p["tasks"].append(dict(p["tasks"][0])), "duplicate task"),
        (
            lambda p: p["tasks"][0].update(depends_on=["missing"]),
            "missing dependencies",
        ),
        (lambda p: p["tasks"][0].update(task_shape="invented"), "unknown or invalid"),
        (
            lambda p: p["tasks"][0].update(provider="codex"),
            "override governed authority",
        ),
        (
            lambda p: p["tasks"][0].update(permission_profile="yolo"),
            "override governed authority",
        ),
        (lambda p: p.update(budgets={"total_concurrency": 4}), "hard maximum"),
        (lambda p: p.update(budgets={"writer_concurrency": 3}), "hard maximum"),
        (lambda p: p["tasks"][0].update(deadline_seconds=301), "exceeds governed"),
    ],
)
def test_plan_rejects_malformed_and_authority_adding_inputs(tmp_path, mutator, match):
    value = raw_plan(tmp_path)
    mutator(value)
    with pytest.raises(PlanValidationError, match=match):
        validate_plan(value)


def test_plan_rejects_cycles(tmp_path):
    value = raw_plan(
        tmp_path,
        [
            {"id": "a", "task_shape": "ordinary_bug_fix", "depends_on": ["b"]},
            {"id": "b", "task_shape": "ordinary_bug_fix", "depends_on": ["a"]},
        ],
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(value)


@pytest.mark.parametrize("path", ["/tmp/escape", "../escape", ".git/config", "a\\b"])
def test_plan_rejects_unsafe_writer_paths(tmp_path, path):
    value = raw_plan(
        tmp_path,
        [
            {
                "id": "writer",
                "task_shape": "standard_feature",
                "workspace": {"kind": "isolated-writer", "own": [path]},
            }
        ],
    )
    with pytest.raises(PlanValidationError, match="path|Git metadata"):
        validate_plan(value)


def test_plan_rejects_writer_overlap_and_undeclared_shared_interface(tmp_path):
    overlap = raw_plan(
        tmp_path,
        [
            {
                "id": "a",
                "task_shape": "standard_feature",
                "workspace": {"kind": "isolated-writer", "own": ["scripts"]},
            },
            {
                "id": "b",
                "task_shape": "standard_feature",
                "workspace": {"kind": "isolated-writer", "own": ["scripts/x.py"]},
            },
        ],
    )
    with pytest.raises(PlanValidationError, match="overlaps"):
        validate_plan(overlap)
    shared = raw_plan(
        tmp_path,
        [
            {
                "id": "a",
                "task_shape": "standard_feature",
                "workspace": {
                    "kind": "isolated-writer",
                    "own": ["src"],
                    "shared_interface_paths": ["schemas/api.md"],
                },
            }
        ],
    )
    with pytest.raises(PlanValidationError, match="outside workspace.own"):
        validate_plan(shared)


def test_plan_rejects_reviewer_reuse_and_unsafe_argv(tmp_path):
    reviewers = raw_plan(
        tmp_path,
        [
            {"id": "producer", "task_shape": "ordinary_bug_fix"},
            {
                "id": "r1",
                "task_shape": "final_review",
                "depends_on": ["producer"],
                "reviewer_for": ["producer"],
            },
            {
                "id": "r2",
                "task_shape": "codex_final_review",
                "depends_on": ["producer"],
                "reviewer_for": ["producer"],
            },
        ],
    )
    with pytest.raises(PlanValidationError, match="reuses reviewers"):
        validate_plan(reviewers)
    unsafe = raw_plan(tmp_path)
    unsafe["tasks"][0]["acceptance"] = [["claude", "--dangerously-skip-permissions"]]
    with pytest.raises(PlanValidationError, match="forbidden"):
        validate_plan(unsafe)


@pytest.mark.parametrize(
    "command",
    [
        ["zsh", "-c", "echo unsafe"],
        ["python", "-c", "print('unsafe')"],
        ["python3", "-c", "print('unsafe')"],
        ["env", "bash", "-c", "echo unsafe"],
        ["timeout", "5", "python3", "-c", "print('unsafe')"],
    ],
)
def test_plan_rejects_command_interpreter_argv_forms(tmp_path, command):
    value = raw_plan(tmp_path)
    value["tasks"][0]["workspace"] = {
        "kind": "isolated-writer",
        "own": ["output.txt"],
    }
    value["tasks"][0]["acceptance"] = [command]
    with pytest.raises(PlanValidationError, match="forbidden"):
        validate_plan(value)


def test_plan_keeps_native_argv_acceptance(tmp_path):
    value = raw_plan(tmp_path)
    value["tasks"][0]["workspace"] = {
        "kind": "isolated-writer",
        "own": ["output.txt"],
    }
    command = ["python3", "-m", "pytest", "-q"]
    value["tasks"][0]["acceptance"] = [command]
    assert validate_plan(value)["tasks"][0]["acceptance"] == [command]


def test_read_only_task_rejects_inert_acceptance(tmp_path):
    value = raw_plan(tmp_path)
    value["tasks"][0]["acceptance"] = [["git", "status", "--porcelain"]]
    with pytest.raises(PlanValidationError, match="read-only.*acceptance"):
        validate_plan(value)


def test_no_writer_plan_rejects_inert_integrated_acceptance(tmp_path):
    value = raw_plan(tmp_path, integrated_acceptance=[["git", "status", "--porcelain"]])
    with pytest.raises(PlanValidationError, match="integrated_acceptance.*writer"):
        validate_plan(value)


def test_cross_family_review_uses_exact_model_family_not_provider_broker(tmp_path):
    same_family = raw_plan(
        tmp_path,
        [
            {"id": "producer", "task_shape": "mechanical_grok"},
            {
                "id": "review",
                "task_shape": "final_review",
                "depends_on": ["producer"],
                "reviewer_for": ["producer"],
            },
        ],
    )
    with pytest.raises(PlanValidationError, match="share model family 'xai'"):
        validate_plan(same_family)

    cross_family = raw_plan(
        tmp_path,
        [
            {"id": "producer", "task_shape": "mechanical_grok"},
            {
                "id": "review",
                "task_shape": "claude_final_review",
                "depends_on": ["producer"],
                "reviewer_for": ["producer"],
            },
        ],
    )
    plan = validate_plan(cross_family)
    by_id = {task["id"]: task for task in plan["tasks"]}
    assert by_id["producer"]["model_family"] == "xai"
    assert by_id["review"]["model_family"] == "anthropic"
    assert by_id["review"]["reviewer_independence_projection"] == {
        "kind": "cross-family",
        "reviewer_task_id": "review",
        "producer_task_ids": ["producer"],
        "require_distinct_attempt_id": True,
        "require_fresh_session": True,
    }


def test_claude_review_accepts_openai_producer(tmp_path):
    plan = compiled(
        tmp_path,
        [
            {"id": "producer", "task_shape": "ordinary_bug_fix"},
            {
                "id": "review",
                "task_shape": "claude_final_review",
                "depends_on": ["producer"],
                "reviewer_for": ["producer"],
            },
        ],
    )
    assert [task["model_family"] for task in plan["tasks"]] == [
        "openai",
        "anthropic",
    ]


def test_independent_supplement_is_route_eligible_and_fresh_session_projected(
    tmp_path,
):
    allowed = compiled(
        tmp_path,
        [
            {"id": "producer", "task_shape": "ordinary_bug_fix"},
            {
                "id": "supplement",
                "task_shape": "secondary_final_review",
                "depends_on": ["producer"],
                "reviewer_for": ["producer"],
            },
        ],
    )
    reviewer = allowed["tasks"][1]
    assert reviewer["model_family"] == "openai"
    assert reviewer["reviewer_independence_projection"] == {
        "kind": "independent-supplement",
        "reviewer_task_id": "supplement",
        "producer_task_ids": ["producer"],
        "require_distinct_attempt_id": True,
        "require_fresh_session": True,
    }

    ineligible = raw_plan(
        tmp_path,
        [
            {"id": "producer", "task_shape": "mechanical"},
            {
                "id": "supplement",
                "task_shape": "secondary_final_review",
                "depends_on": ["producer"],
                "reviewer_for": ["producer"],
            },
        ],
    )
    with pytest.raises(PlanValidationError, match="not eligible for producer route"):
        validate_plan(ineligible)

    same_session_override = raw_plan(tmp_path)
    same_session_override["tasks"][0]["session_id"] = "reuse-producer"
    with pytest.raises(PlanValidationError, match="override governed authority"):
        validate_plan(same_session_override)


def test_plan_config_fingerprint_is_digest_only(tmp_path):
    valid = raw_plan(
        tmp_path,
        config_fingerprint={"digest": "a" * 64, "provider_category": "official"},
    )
    assert validate_plan(valid)["config_fingerprint"]["digest"] == "a" * 64
    valid["config_fingerprint"]["base_url"] = "https://secret.invalid"
    with pytest.raises(PlanValidationError, match="fingerprint"):
        validate_plan(valid)


def test_plan_input_ref_and_metadata_cannot_hide_prompt_or_authority(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "task.md").write_text("fixture")
    value = raw_plan(tmp_path)
    value["tasks"][0]["input_ref"] = "inputs/task.md"
    value["tasks"][0]["metadata"] = {"label": "fixture-one"}
    assert validate_plan(value)["tasks"][0]["input_ref"] == "inputs/task.md"
    value["tasks"][0]["input_ref"] = "inline prompt body"
    with pytest.raises(PlanValidationError, match="existing repository file"):
        validate_plan(value)
    value = raw_plan(tmp_path, metadata={"provider": "codex"})
    with pytest.raises(PlanValidationError, match="forbidden metadata"):
        validate_plan(value)


def test_plan_accepts_only_versioned_analysis_result_contract(tmp_path):
    value = raw_plan(tmp_path)
    value["tasks"][0]["result_contract"] = "analysis-v1"
    assert validate_plan(value)["tasks"][0]["result_contract"] == "analysis-v1"
    value["tasks"][0]["result_contract"] = "free-form"
    with pytest.raises(PlanValidationError, match="result_contract"):
        validate_plan(value)
    value = raw_plan(tmp_path)
    value["tasks"][0]["metadata"] = {"prompt": "do hidden work"}
    with pytest.raises(PlanValidationError, match="forbidden metadata"):
        validate_plan(value)


def test_journal_is_mode_0600_and_rejects_stale_fencing(tmp_path):
    journal = event_journal(tmp_path)
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    append_controller(journal)
    journal.append(
        "run_started",
        attempt_id="run-attempt",
        generation=1,
        fencing_token="fence-1",
        payload={},
    )
    with pytest.raises(JournalError, match="stale"):
        journal.append(
            "run_failed",
            attempt_id="run-attempt",
            generation=1,
            fencing_token="wrong",
            payload={},
        )
    with pytest.raises(JournalError, match="non-monotonic"):
        append_controller(journal, generation=3, token="fence-3")


def test_journal_deduplicates_stable_ids_and_rejects_collision(tmp_path):
    journal = event_journal(tmp_path)
    append_controller(journal)
    event = journal.append(
        "run_started",
        attempt_id="run-attempt",
        generation=1,
        fencing_token="fence-1",
        payload={},
        event_id="stable",
        timestamp="2026-01-01T00:00:02.000Z",
    )
    journal.append_event(event)
    assert len(journal.read()) == 2
    collision = dict(event, payload={"different": True})
    with pytest.raises(JournalError, match="collision"):
        journal.append_event(collision)


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "secret"},
        {"nested": {"account_id": "x"}},
        {"note": "Bearer abcdefghijklmnop"},
        {"api_key": "x"},
        {"password": "x"},
        {"secret": "x"},
    ],
)
def test_journal_rejects_sensitive_fields(payload, tmp_path):
    journal = event_journal(tmp_path)
    append_controller(journal)
    with pytest.raises(JournalError, match="sensitive|secret-like"):
        journal.append(
            "run_started",
            attempt_id="run-attempt",
            generation=1,
            fencing_token="fence-1",
            payload=payload,
        )


def test_journal_sensitive_key_gate_does_not_use_substring_matching(tmp_path):
    journal = event_journal(tmp_path)
    append_controller(journal)
    event = journal.append(
        "run_started",
        attempt_id="run-attempt",
        generation=1,
        fencing_token="fence-1",
        payload={"secretary": "approved", "password_hint": "set-by-user"},
    )
    assert event["payload"]["secretary"] == "approved"


def test_event_fold_is_deterministic_and_preserves_orphan_resource_intent(tmp_path):
    journal = event_journal(tmp_path)
    append_controller(journal)
    event = journal.append(
        "resource_intent",
        task_id="writer",
        attempt_id="attempt-1",
        generation=1,
        fencing_token="fence-1",
        payload={
            "resource_id": "resource-1",
            "created_by_run_id": "test-run",
            "repo_root": str(tmp_path),
        },
    )
    first = fold_events(journal.read())
    second = fold_events([*journal.read(), event])
    assert first == second
    assert first["resources"]["resource-1"]["status"] == "intent"


def test_atomic_manifest_requires_current_fencing(tmp_path):
    journal = event_journal(tmp_path)
    append_controller(journal)
    target = tmp_path / "manifest.json"
    journal.write_manifest(
        target, {"complete": True}, generation=1, fencing_token="fence-1"
    )
    assert json.loads(target.read_text()) == {"complete": True}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(JournalError, match="stale"):
        journal.write_manifest(
            target, {"complete": False}, generation=1, fencing_token="wrong"
        )
    assert json.loads(target.read_text()) == {"complete": True}


def test_controller_lease_is_cross_process(tmp_path):
    path = tmp_path / "lease.lock"
    with ControllerLease(path, "test-run"):
        script = (
            "from pathlib import Path; "
            "from scripts.orchestration.journal import ControllerLease,LeaseContended; "
            f"p=Path({str(path)!r}); "
            "\ntry:\n ControllerLease(p,'test-run').acquire(); print('bad')\n"
            "except LeaseContended:\n print('contended')"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=True,
        )
        assert completed.stdout.strip() == "contended"


def test_scheduler_runs_ready_dag_and_never_replays_completed_nodes(tmp_path):
    plan = compiled(
        tmp_path,
        [
            {"id": "a", "task_shape": "mechanical"},
            {"id": "b", "task_shape": "mechanical", "depends_on": ["a"]},
        ],
    )
    adapter = FakeAdapter()
    journal = event_journal(tmp_path)
    scheduler = Scheduler(plan, adapter, journal, tmp_path / "lease", clock=FakeClock())
    assert scheduler.run()["status"] == "completed"
    assert [call["task_id"] for call in adapter.calls] == ["a", "b"]
    Scheduler(plan, adapter, journal, tmp_path / "lease", clock=FakeClock()).run(
        resume=True
    )
    assert [call["task_id"] for call in adapter.calls] == ["a", "b"]


def test_scheduler_enforces_total_family_and_writer_limits(tmp_path):
    barrier = threading.Barrier(2)

    def slow(**_kwargs):
        try:
            barrier.wait(timeout=0.2)
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.02)
        return {"status": "succeeded"}

    total_plan = compiled(
        tmp_path,
        [
            {"id": "cursor", "task_shape": "mechanical"},
            {"id": "codex", "task_shape": "ordinary_bug_fix"},
            {"id": "claude", "task_shape": "standard_feature"},
        ],
        budgets={"total_concurrency": 2},
    )
    adapter = FakeAdapter({key: [slow] for key in ("cursor", "codex", "claude")})
    Scheduler(
        total_plan, adapter, event_journal(tmp_path), tmp_path / "lease-total"
    ).run()
    assert adapter.max_active == 2

    family_plan = compiled(
        tmp_path,
        [
            {"id": "a", "task_shape": "mechanical"},
            {"id": "b", "task_shape": "mechanical"},
        ],
        budgets={"total_concurrency": 2, "family_concurrency": {"cursor": 1}},
    )
    family_adapter = FakeAdapter({"a": [slow], "b": [slow]})
    family_plan["run_id"] = "family"
    Scheduler(
        family_plan,
        family_adapter,
        event_journal(tmp_path, "family"),
        tmp_path / "lease-family",
    ).run()
    assert family_adapter.max_family_active["cursor"] == 1

    writer_plan = validate_plan(
        raw_plan(
            tmp_path,
            [
                {
                    "id": "a",
                    "task_shape": "mechanical",
                    "workspace": {"kind": "isolated-writer", "own": ["a"]},
                },
                {
                    "id": "b",
                    "task_shape": "mechanical",
                    "workspace": {"kind": "isolated-writer", "own": ["b"]},
                },
            ],
            budgets={"total_concurrency": 2, "writer_concurrency": 1},
        )
    )
    writer_adapter = FakeAdapter({"a": [slow], "b": [slow]})
    writer_journal = event_journal(tmp_path, "writers")
    writer_plan["run_id"] = "writers"
    Scheduler(
        writer_plan, writer_adapter, writer_journal, tmp_path / "lease-writers"
    ).run()
    assert writer_adapter.max_active == 1


def test_plan_family_limits_default_to_canon_and_can_only_tighten(tmp_path):
    default_plan = compiled(
        tmp_path,
        [
            {"id": "a", "task_shape": "mechanical"},
            {"id": "b", "task_shape": "mechanical_grok"},
        ],
    )
    assert default_plan["budgets"]["family_concurrency"]["cursor"] == 2
    assert {task["family_limit"] for task in default_plan["tasks"]} == {2}

    tightened = compiled(
        tmp_path,
        [{"id": "a", "task_shape": "mechanical"}],
        budgets={"family_concurrency": {"cursor": 1}},
    )
    assert tightened["budgets"]["family_concurrency"]["cursor"] == 1
    assert tightened["tasks"][0]["family_limit"] == 1

    with pytest.raises(PlanValidationError, match="exceeds canon limit 2"):
        compiled(
            tmp_path,
            [{"id": "a", "task_shape": "mechanical"}],
            budgets={"family_concurrency": {"cursor": 3}},
        )


def test_plan_serial_family_is_hard_one_and_unknown_families_fail(tmp_path):
    serial = compiled(
        tmp_path,
        [{"id": "a", "task_shape": "ordinary_bug_fix"}],
    )
    assert serial["budgets"]["family_concurrency"]["codex-family"] == 1
    assert serial["tasks"][0]["family_limit"] == 1

    with pytest.raises(PlanValidationError, match="exceeds canon limit 1"):
        compiled(
            tmp_path,
            [{"id": "a", "task_shape": "ordinary_bug_fix"}],
            budgets={"family_concurrency": {"codex-family": 2}},
        )
    with pytest.raises(PlanValidationError, match="unknown canon family"):
        compiled(
            tmp_path,
            [{"id": "a", "task_shape": "mechanical"}],
            budgets={"family_concurrency": {"invented-family": 1}},
        )


def test_scheduler_retries_only_declared_classes_and_propagates_failure(tmp_path):
    plan = compiled(
        tmp_path,
        [
            {
                "id": "retry",
                "task_shape": "ordinary_bug_fix",
                "retry": {"max_attempts": 2, "retry_on": ["adapter-transient"]},
            },
            {"id": "child", "task_shape": "mechanical", "depends_on": ["retry"]},
        ],
    )
    adapter = FakeAdapter(
        {
            "retry": [
                {"status": "failed", "failure_class": "adapter-transient"},
                {"status": "succeeded"},
            ]
        }
    )
    state = Scheduler(plan, adapter, event_journal(tmp_path), tmp_path / "lease").run()
    assert state["status"] == "completed"
    assert len([call for call in adapter.calls if call["task_id"] == "retry"]) == 2

    fail_plan = compiled(
        tmp_path,
        [
            {"id": "fail", "task_shape": "ordinary_bug_fix"},
            {"id": "blocked", "task_shape": "mechanical", "depends_on": ["fail"]},
        ],
    )
    fail_plan["run_id"] = "failure"
    fail_adapter = FakeAdapter(
        {"fail": [{"status": "failed", "failure_class": "task-quality-failure"}]}
    )
    state = Scheduler(
        fail_plan,
        fail_adapter,
        event_journal(tmp_path, "failure"),
        tmp_path / "lease-failure",
    ).run()
    assert state["status"] == "failed"
    assert state["tasks"]["blocked"]["status"] == "blocked"
    assert [call["task_id"] for call in fail_adapter.calls] == ["fail"]


@pytest.mark.parametrize(
    "failure_class", [
        "provider-rate-limit",
        "provider-transient",
        "provider-preflight-transient",
    ]
)
def test_scheduler_provider_retry_has_bounded_deterministic_backoff(
    tmp_path, failure_class
):
    class RecordingClock(FakeClock):
        def __init__(self):
            super().__init__()
            self.sleeps = []

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            super().sleep(seconds)

    plan = compiled(
        tmp_path,
        [
            {
                "id": "retry",
                "task_shape": "ordinary_bug_fix",
                "retry": {
                    "max_attempts": 3,
                    "retry_on": [failure_class],
                },
            }
        ],
    )
    adapter = FakeAdapter(
        {
            "retry": [
                {"status": "failed", "failure_class": failure_class},
                {"status": "failed", "failure_class": failure_class},
                {"status": "succeeded"},
            ]
        }
    )
    clock = RecordingClock()
    journal = event_journal(tmp_path)

    state = Scheduler(
        plan, adapter, journal, tmp_path / "lease-backoff", clock=clock
    ).run()

    assert state["status"] == "completed"
    assert clock.sleeps == [1.0, 2.0]
    retries = [
        event["payload"]
        for event in journal.read()
        if event["event_type"] == "task_retry_scheduled"
    ]
    assert [row["retry_after_seconds"] for row in retries] == [1.0, 2.0]
    assert all(set(row) <= {
        "failure_class", "next_ordinal", "retry_after_seconds", "retry_not_before"
    } for row in retries)


def test_scheduler_graceful_cancel_stops_admission_and_drains_live_task(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def blocking(**_kwargs):
        started.set()
        assert release.wait(timeout=2)
        return {"status": "succeeded"}

    plan = compiled(
        tmp_path,
        [
            {"id": "live", "task_shape": "mechanical", "deadline_seconds": 30},
            {"id": "pending", "task_shape": "mechanical", "depends_on": ["live"]},
        ],
        budgets={"total_concurrency": 1},
    )
    scheduler = Scheduler(
        plan,
        FakeAdapter({"live": [blocking]}),
        event_journal(tmp_path),
        tmp_path / "lease",
    )
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("state", scheduler.run())
    )
    thread.start()
    assert started.wait(timeout=1)
    cancel_state = scheduler.request_cancel()
    assert cancel_state["status"] == "canceling"
    assert cancel_state["eta_seconds"] <= 30
    release.set()
    thread.join(timeout=2)
    assert outcome["state"]["status"] == "canceled"
    assert outcome["state"]["tasks"]["live"]["status"] == "succeeded"
    assert outcome["state"]["tasks"]["pending"]["status"] == "canceled"


def test_cross_process_cancel_request_is_atomic_and_drained_by_controller(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def blocking(**_kwargs):
        started.set()
        assert release.wait(timeout=2)
        return {"status": "succeeded"}

    plan = compiled(
        tmp_path,
        [
            {"id": "live", "task_shape": "mechanical"},
            {"id": "pending", "task_shape": "mechanical", "depends_on": ["live"]},
        ],
        budgets={"total_concurrency": 1},
    )
    journal = event_journal(tmp_path)
    cancel_path = tmp_path / "external-cancel.json"
    scheduler = Scheduler(
        plan,
        FakeAdapter({"live": [blocking]}),
        journal,
        tmp_path / "lease-external",
        cancel_request_path=cancel_path,
    )
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("state", scheduler.run())
    )
    thread.start()
    assert started.wait(timeout=1)
    generation, token = journal.current_controller()
    request = request_cancel_file(
        cancel_path,
        run_id="test-run",
        generation=generation,
        fencing_token=token,
    )
    assert stat.S_IMODE(cancel_path.stat().st_mode) == 0o600
    assert read_cancel_file(cancel_path) == request
    deadline = time.time() + 1
    while not scheduler.status()["cancel_requested"] and time.time() < deadline:
        time.sleep(0.01)
    assert scheduler.status()["cancel_requested"] is True
    release.set()
    thread.join(timeout=2)
    assert outcome["state"]["status"] == "canceled"
    assert outcome["state"]["tasks"]["pending"]["status"] == "canceled"


def test_stale_cancel_request_does_not_cancel_resumed_generation(tmp_path):
    plan = compiled(tmp_path)
    journal = event_journal(tmp_path)
    append_controller(journal)
    journal.append(
        "run_started",
        attempt_id="run-attempt",
        generation=1,
        fencing_token="fence-1",
        payload={},
    )
    cancel_path = tmp_path / "stale-cancel.json"
    request_cancel_file(
        cancel_path,
        run_id="test-run",
        generation=1,
        fencing_token="fence-1",
    )
    state = Scheduler(
        plan,
        FakeAdapter(),
        journal,
        tmp_path / "lease-resume",
        cancel_request_path=cancel_path,
    ).run(resume=True)
    assert state["generation"] == 2
    assert state["status"] == "completed"
    assert state["cancel_requested"] is False


def test_resume_reconciles_claimed_attempt_without_replay(tmp_path):
    plan = compiled(tmp_path)
    journal = event_journal(tmp_path)
    append_controller(journal)
    journal.append(
        "run_started",
        attempt_id="run-attempt",
        generation=1,
        fencing_token="fence-1",
        payload={},
    )
    journal.append(
        "dispatch_intent",
        task_id="one",
        attempt_id="attempt-stable",
        generation=1,
        fencing_token="fence-1",
        payload={"deadline_at": "2026-01-01T00:05:00.000Z"},
    )
    journal.append(
        "dispatch_claimed",
        task_id="one",
        attempt_id="attempt-stable",
        generation=1,
        fencing_token="fence-1",
        payload={"wrapper_pid": 123, "wrapper_start_fingerprint": "start"},
    )
    adapter = FakeAdapter(
        reconcile={"one": {"status": "succeeded", "receipt_sha256": "a" * 64}}
    )
    state = Scheduler(plan, adapter, journal, tmp_path / "lease").run(resume=True)
    assert state["status"] == "completed"
    assert adapter.calls == []
    assert state["tasks"]["one"]["attempts"] == ["attempt-stable"]


def test_resume_reconciles_post_launch_dispatch_intent_without_replay(tmp_path):
    plan = compiled(tmp_path)
    journal = event_journal(tmp_path)
    append_controller(journal)
    journal.append(
        "run_started",
        attempt_id="run-attempt",
        generation=1,
        fencing_token="fence-1",
        payload={},
    )
    journal.append(
        "dispatch_intent",
        task_id="one",
        attempt_id="attempt-stable",
        generation=1,
        fencing_token="fence-1",
        payload={"deadline_at": "2026-01-01T00:05:00.000Z"},
    )
    adapter = FakeAdapter(
        reconcile={"one": {"status": "succeeded", "receipt_sha256": "b" * 64}}
    )
    state = Scheduler(plan, adapter, journal, tmp_path / "lease").run(resume=True)
    assert state["status"] == "completed"
    assert adapter.calls == []
    assert state["tasks"]["one"]["status"] == "succeeded"


def test_two_phase_adapter_journals_pid_before_collect_and_owns_deadline(tmp_path):
    launched = threading.Event()
    release = threading.Event()

    class Handle:
        def journal_evidence(self):
            return {
                "wrapper_pid": 4242,
                "wrapper_start_fingerprint": "start-fingerprint",
                "process_group_id": 4242,
                "deadline_at": "2099-01-01T00:00:00.000Z",
                "launch_manifest_path": str(tmp_path / "attempt-manifest.json"),
            }

    class TwoPhase:
        two_phase_process = True
        owns_deadline = True

        def launch_task(self, _task, **_kwargs):
            launched.set()
            return Handle()

        def collect_task(self, _handle):
            assert release.wait(timeout=2)
            return {"status": "succeeded", "failure_class": "none"}

    clock = FakeClock()
    journal = event_journal(tmp_path)
    scheduler = Scheduler(
        compiled(tmp_path), TwoPhase(), journal, tmp_path / "lease", clock=clock
    )
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("state", scheduler.run())
    )
    thread.start()
    assert launched.wait(timeout=1)
    deadline = time.time() + 1
    claimed = None
    while time.time() < deadline:
        claimed = next(
            (
                event
                for event in journal.read()
                if event["event_type"] == "dispatch_claimed"
            ),
            None,
        )
        if claimed:
            break
        time.sleep(0.01)
    assert claimed is not None
    assert claimed["payload"]["wrapper_pid"] == 4242
    assert claimed["payload"]["wrapper_start_fingerprint"] == "start-fingerprint"
    clock.advance(1_000)
    time.sleep(0.05)
    assert thread.is_alive(), (
        "scheduler must not discard a deadline-owning adapter future"
    )
    release.set()
    thread.join(timeout=2)
    assert outcome["state"]["status"] == "completed"


def test_resume_dispatch_intent_without_attempt_manifest_fails_closed(tmp_path):
    plan = compiled(
        tmp_path,
        [
            {
                "id": "writer",
                "task_shape": "standard_feature",
                "workspace": {"kind": "isolated-writer", "own": ["src"]},
            }
        ],
    )
    journal = event_journal(tmp_path)
    first = FakeAdapter(
        {"writer": [{"status": "failed", "failure_class": "task-quality-failure"}]}
    )
    assert (
        Scheduler(plan, first, journal, tmp_path / "lease-first").run()["status"]
        == "failed"
    )
    # Simulate recovery from the safe pre-claim point by removing only terminal
    # task/run lines in this fixture; the append-only production journal never does this.
    retained = [
        event
        for event in journal.read()
        if event["event_type"]
        not in {"dispatch_claimed", "task_failed", "integration_failed", "run_failed"}
    ]
    recovered = EventJournal(tmp_path / "recovered.jsonl", "test-run")
    for event in retained:
        recovered.append_event(event)
    second = FakeAdapter()
    state = Scheduler(plan, second, recovered, tmp_path / "lease-recovered").run(
        resume=True
    )
    assert state["status"] == "failed-unsafe"
    assert len(second.resources) == 0
    assert second.calls == []
    assert state["tasks"]["writer"]["failure_class"] == "unreconciled-live-wrapper"


def test_writer_resource_intent_precedes_confirmation_and_dispatch(tmp_path):
    plan = compiled(
        tmp_path,
        [
            {
                "id": "writer",
                "task_shape": "standard_feature",
                "workspace": {"kind": "isolated-writer", "own": ["src"]},
            }
        ],
    )
    journal = event_journal(tmp_path)
    state = Scheduler(plan, FakeAdapter(), journal, tmp_path / "lease").run()
    kinds = [event["event_type"] for event in journal.read()]
    assert (
        kinds.index("resource_intent")
        < kinds.index("resource_created")
        < kinds.index("dispatch_claimed")
    )
    assert state["resources"]["test-run:writer:worktree"]["status"] == "created"
    resource = state["resources"]["test-run:writer:worktree"]
    assert resource["base_sha"] == "a" * 40
    assert resource["ledger_slug"] == "test-ledger"
    assert resource["fencing_token"].startswith("fence-")


def test_adapter_contract_or_sensitive_payload_fails_unsafe(tmp_path):
    plan = compiled(tmp_path)
    state = Scheduler(
        plan,
        FakeAdapter({"one": [{"status": "succeeded", "prompt": "must-not-journal"}]}),
        event_journal(tmp_path),
        tmp_path / "lease-sensitive",
    ).run()
    assert state["status"] == "failed-unsafe"
    assert state["tasks"]["one"]["failure_class"] == "adapter-sensitive-payload"


def test_finalize_run_precedes_completion_and_failure_blocks_completion(tmp_path):
    plan = compiled(tmp_path)
    success_journal = event_journal(tmp_path)
    success = FakeAdapter(
        finalize={"status": "succeeded", "integration_head": "a" * 40}
    )
    state = Scheduler(plan, success, success_journal, tmp_path / "lease-success").run()
    kinds = [event["event_type"] for event in success_journal.read()]
    assert kinds.index("integration_succeeded") < kinds.index("run_completed")
    assert state["integration"]["integration_head"] == "a" * 40

    failed_plan = dict(plan, run_id="finalize-failed")
    failed = FakeAdapter(
        finalize={"status": "failed", "failure_class": "acceptance-failed"}
    )
    failed_journal = event_journal(tmp_path, "finalize-failed")
    state = Scheduler(
        failed_plan, failed, failed_journal, tmp_path / "lease-failed"
    ).run()
    assert state["status"] == "failed"
    assert "run_completed" not in [
        event["event_type"] for event in failed_journal.read()
    ]


def test_governed_review_runs_only_after_frozen_integration(tmp_path):
    plan = compiled(
        tmp_path,
        [
            {"id": "producer", "task_shape": "ordinary_bug_fix"},
            {
                "id": "review",
                "task_shape": "claude_final_review",
                "depends_on": ["producer"],
                "reviewer_for": ["producer"],
            },
        ],
    )

    class ReviewAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.order = []

        def run_task(self, task, **kwargs):
            self.order.append(task["id"])
            return super().run_task(task, **kwargs)

        def finalize_run(self, plan, state, **kwargs):
            self.order.append("integration")
            assert state["tasks"]["producer"]["status"] == "succeeded"
            return {"status": "succeeded", "integration_head": "frozen"}

        def prepare_review(self, task, state, **kwargs):
            self.order.append("review-context")
            assert state["integration"]["integration_head"] == "frozen"
            return {
                "status": "succeeded",
                "review_bundle_path": "/private/review.json",
                "review_bundle_sha256": "a" * 64,
                "producer_count": 1,
                "candidate_kind": "read-only-artifact-set",
                "integration_head": "frozen",
            }

    adapter = ReviewAdapter()
    state = Scheduler(
        plan,
        adapter,
        event_journal(tmp_path),
        tmp_path / "review.lock",
    ).run()
    assert state["status"] == "completed"
    assert adapter.order == ["producer", "integration", "review-context", "review"]
    events = event_journal(tmp_path).read()
    kinds = [row["event_type"] for row in events]
    assert kinds.index("integration_succeeded") < kinds.index("review_context_prepared")
    assert kinds.index("review_context_prepared") < kinds.index("dispatch_claimed", kinds.index("review_context_prepared"))

def test_cleanup_outcomes_are_independent(tmp_path):
    plan = compiled(tmp_path)
    journal = event_journal(tmp_path)
    scheduler = Scheduler(plan, FakeAdapter(), journal, tmp_path / "lease")
    scheduler.lease.acquire()
    try:
        scheduler.generation = journal.next_generation()
        scheduler.fencing_token = "fence-manual"
        scheduler._emit("controller_acquired", payload={"action": "test"})
        scheduler.record_terminal_cleanup(
            "one",
            "attempt-one",
            process={"status": "succeeded"},
            worktree={"status": "failed"},
            branch={"status": "preserved"},
        )
    finally:
        scheduler.lease.release()
    cleanup = fold_events(journal.read())["cleanup"]["one"]
    assert cleanup == {
        "process": {"status": "succeeded"},
        "worktree": {"status": "failed"},
        "branch": {"status": "preserved"},
    }


def test_cleanup_record_does_not_create_a_phantom_task(tmp_path):
    journal = event_journal(tmp_path)
    scheduler = Scheduler(
        compiled(tmp_path), FakeAdapter(), journal, tmp_path / "cleanup-lease"
    )
    scheduler.lease.acquire()
    try:
        scheduler.generation = journal.next_generation()
        scheduler.fencing_token = "fence-cleanup"
        scheduler._emit("controller_acquired", payload={"action": "test"})
        scheduler.record_terminal_cleanup(
            "integration",
            "run-attempt",
            process={"status": "not-applicable"},
            worktree={"status": "succeeded"},
            branch={"status": "preserved"},
        )
    finally:
        scheduler.lease.release()

    state = fold_events(journal.read())
    assert state["cleanup"]["integration"]["worktree"] == {"status": "succeeded"}
    assert "integration" not in state["tasks"]


def test_identity_reconciliation_fails_closed_on_missing_or_drift():
    fields = {
        "run_id": "r",
        "task_id": "t",
        "attempt_id": "a",
        "generation": 1,
        "session_id": "s",
        "wrapper_pid": 1,
        "wrapper_start_fingerprint": "p",
        "worktree_path": "/tmp/w",
        "branch": "b",
        "base_sha": "a" * 40,
    }
    reconcile_identity(fields, dict(fields))
    with pytest.raises(JournalError, match="drift"):
        reconcile_identity(fields, {**fields, "session_id": "other"})
    partial = dict(fields)
    partial.pop("branch")
    with pytest.raises(JournalError, match="incomplete"):
        reconcile_identity(fields, partial)
