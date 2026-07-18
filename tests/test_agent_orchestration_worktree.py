from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess

import pytest

from scripts.orchestration.join import JoinDispute, join_candidates
from scripts.orchestration.scheduler import _fencing_token
from scripts.orchestration.worktree import (
    ResourceOwnership,
    ScopeViolation,
    UnsafeWorktreeError,
    WorktreeManager,
    validate_changed_scope,
)


RUN_ID = "run-test"
FENCE = "fence-test-generation-7"
SLUG = "fixture-repo"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ("git", "-C", str(cwd), *args),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    generated = tmp_path / "generated"
    repo.mkdir()
    generated.mkdir()
    git(repo, "init")
    git(repo, "checkout", "-b", "main")
    git(repo, "config", "user.name", "Fixture User")
    git(repo, "config", "user.email", "fixture@example.test")
    (repo / "app").mkdir()
    (repo / "app" / "a.txt").write_text("a0\n", encoding="utf-8")
    (repo / "app" / "b.txt").write_text("b0\n", encoding="utf-8")
    (repo / "schemas").mkdir()
    (repo / "schemas" / "model.yaml").write_text("version: 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".env\n*.secret\n", encoding="utf-8")
    (repo / ".codex").mkdir()
    (repo / ".codex" / "project.txt").write_text("tracked\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    # These are deliberately outside the tracked base and must not propagate.
    (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    host_config = tmp_path / "host" / ".codex"
    host_config.mkdir(parents=True)
    (host_config / "config.toml").write_text("secret = true\n", encoding="utf-8")
    return repo, generated, base


def ownership(
    repo: Path,
    generated: Path,
    base: str,
    task_id: str,
    *,
    kind: str = "writer",
) -> ResourceOwnership:
    return ResourceOwnership(
        created_by_run_id=RUN_ID,
        fencing_token=FENCE,
        repo_root=repo.resolve(),
        path=(generated / task_id).resolve(),
        branch=f"agent/{task_id}",
        base_sha=base,
        ledger_slug=SLUG,
        kind=kind,
        task_id=task_id if kind == "writer" else None,
    )


def test_validate_changed_scope_returns_declared_taxonomy_hits_only():
    assert validate_changed_scope(
        ["schemas/model.yaml"],
        own=["schemas/**"],
        do_not_touch=[],
        shared_interface_paths=[],
    ) == ("schemas/model.yaml",)
    with pytest.raises(ScopeViolation, match="outside declared scope"):
        validate_changed_scope(
            ["schemas/model.yaml"],
            own=["app/**"],
            do_not_touch=[],
            shared_interface_paths=[],
        )


def create_writer(
    manager: WorktreeManager,
    resource: ResourceOwnership,
) -> ResourceOwnership:
    return manager.create(
        resource,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        ledger_slug=SLUG,
    )


def candidate_for(
    manager: WorktreeManager,
    resource: ResourceOwnership,
    relative: str,
    content: str,
):
    (resource.path / relative).write_text(content, encoding="utf-8")
    result = manager.inspect_writer(
        resource,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        own=[relative],
        do_not_touch=[],
        shared_interface_paths=[],
        acceptance_commands=[["git", "diff", "--check"]],
    )
    return manager.commit_candidate(
        resource,
        result,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        plan_id="plan-fixture",
    )


def test_create_uses_frozen_tracked_base_and_exact_ledger_stamp(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    main_head = git(repo, "rev-parse", "HEAD")
    main_status = git(repo, "status", "--porcelain")
    resource = create_writer(manager, ownership(repo, generated, base, "task-a"))

    assert resource.state == "created"
    assert git(resource.path, "rev-parse", "HEAD") == base
    assert (resource.path / ".codex" / "project.txt").read_text() == "tracked\n"
    assert not (resource.path / ".env").exists()
    assert (resource.path / ".agents" / "ledger-slug").read_text() == SLUG + "\n"
    assert git(resource.path, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "HEAD") == main_head
    assert git(repo, "status", "--porcelain") == main_status


@pytest.mark.parametrize("collision", ["path", "branch"])
def test_create_stops_closed_on_path_or_branch_collision(repository, collision):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    resource = ownership(repo, generated, base, "task-a")
    if collision == "path":
        resource.path.mkdir()
    else:
        git(repo, "branch", resource.branch, base)

    with pytest.raises(UnsafeWorktreeError):
        create_writer(manager, resource)

    assert not (resource.path / ".agents" / "ledger-slug").exists()
    assert (resource.path.exists() if collision == "path" else not resource.path.exists())


def test_create_rejects_stale_fence_foreign_run_and_path_escape(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    resource = ownership(repo, generated, base, "task-a")
    with pytest.raises(UnsafeWorktreeError, match="another run"):
        manager.create(
            resource,
            current_run_id="foreign",
            current_fencing_token=FENCE,
            ledger_slug=SLUG,
        )
    with pytest.raises(UnsafeWorktreeError, match="stale"):
        manager.create(
            resource,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE + "-stale",
            ledger_slug=SLUG,
        )
    escaped = replace(resource, path=(generated.parent / "escape").resolve())
    with pytest.raises(UnsafeWorktreeError, match="escapes"):
        create_writer(manager, escaped)


def test_scheduler_string_fencing_token_is_the_worktree_authority_type(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    token = _fencing_token(RUN_ID, 3)
    assert isinstance(token, str) and token.startswith("fence-")
    resource = replace(
        ownership(repo, generated, base, "task-a"), fencing_token=token
    )

    created = manager.create(
        resource,
        current_run_id=RUN_ID,
        current_fencing_token=token,
        ledger_slug=SLUG,
    )
    assert created.fencing_token == token


def test_writer_diff_is_scoped_hashed_and_controller_committed_once(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    resource = create_writer(manager, ownership(repo, generated, base, "task-a"))
    (resource.path / "app" / "a.txt").write_text("a1\n", encoding="utf-8")
    (resource.path / "app" / "new.txt").write_text("new\n", encoding="utf-8")

    result = manager.inspect_writer(
        resource,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        own=["app/a.txt", "app/new.txt"],
        do_not_touch=["app/b.txt"],
        shared_interface_paths=[],
        acceptance_commands=[["git", "diff", "--check"]],
    )
    assert git(resource.path, "rev-parse", "HEAD") == base
    assert result.changed_paths == ("app/a.txt", "app/new.txt")
    assert len(result.diff_hash) == 64
    assert result.acceptance[0].exit_code == 0

    candidate = manager.commit_candidate(
        resource,
        result,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        plan_id="plan-fixture",
    )
    assert candidate.parent_sha == base
    assert git(resource.path, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert git(resource.path, "status", "--porcelain") == ""
    assert git(resource.path, "show", "-s", "--format=%an <%ae>") == (
        "Agent Run Controller <agent-run-controller@localhost>"
    )
    assert candidate.diff_hash == result.diff_hash


def test_scope_do_not_touch_and_symlink_escape_fail_closed(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)

    protected = create_writer(manager, ownership(repo, generated, base, "protected"))
    (protected.path / "app" / "b.txt").write_text("forbidden\n", encoding="utf-8")
    with pytest.raises(ScopeViolation, match="do_not_touch"):
        manager.inspect_writer(
            protected,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            own=["app/**"],
            do_not_touch=["app/b.txt"],
            shared_interface_paths=[],
        )

    escaped = create_writer(manager, ownership(repo, generated, base, "escaped"))
    os.symlink(repo / ".env", escaped.path / "app" / "leak")
    with pytest.raises(ScopeViolation, match="symlink"):
        manager.inspect_writer(
            escaped,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            own=["app/leak"],
            do_not_touch=[],
            shared_interface_paths=[],
        )

    ignored = create_writer(manager, ownership(repo, generated, base, "ignored"))
    (ignored.path / "payload.secret").write_text("hidden\n", encoding="utf-8")
    with pytest.raises(ScopeViolation, match="outside declared scope"):
        manager.inspect_writer(
            ignored,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            own=["app/**"],
            do_not_touch=[],
            shared_interface_paths=[],
        )


def test_agent_created_commit_is_a_dispute_and_is_preserved(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    resource = create_writer(manager, ownership(repo, generated, base, "task-a"))
    (resource.path / "app" / "a.txt").write_text("agent edit\n", encoding="utf-8")
    git(resource.path, "add", "app/a.txt")
    git(resource.path, "commit", "-m", "agent-owned")

    with pytest.raises(UnsafeWorktreeError, match="commit|HEAD"):
        manager.inspect_writer(
            resource,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            own=["app/a.txt"],
            do_not_touch=[],
            shared_interface_paths=[],
        )
    assert resource.path.exists()
    assert git(resource.path, "rev-parse", "HEAD") != base


def test_resume_reconciles_slug_head_diff_and_rejects_drift(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    resource = create_writer(manager, ownership(repo, generated, base, "task-a"))
    candidate = candidate_for(manager, resource, "app/a.txt", "a1\n")

    observation = manager.reconcile(
        resource,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        expected_head=candidate.commit_sha,
        expected_ledger_slug=SLUG,
        expected_diff_hash=candidate.diff_hash,
    )
    assert observation.head_sha == candidate.commit_sha
    assert observation.diff_hash == candidate.diff_hash

    (resource.path / "app" / "a.txt").write_text("drift\n", encoding="utf-8")
    with pytest.raises(UnsafeWorktreeError, match="diff hash"):
        manager.reconcile(
            resource,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            expected_head=candidate.commit_sha,
            expected_ledger_slug=SLUG,
            expected_diff_hash=candidate.diff_hash,
        )
    assert resource.path.exists()


def test_cleanup_requires_current_ownership_and_never_deletes_branch(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    resource = create_writer(manager, ownership(repo, generated, base, "task-a"))

    with pytest.raises(UnsafeWorktreeError, match="another run"):
        manager.cleanup(
            resource,
            current_run_id="foreign",
            current_fencing_token=FENCE,
        )
    (resource.path / "app" / "a.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(UnsafeWorktreeError, match="preserved"):
        manager.cleanup(
            resource,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
        )
    assert resource.path.exists()

    (resource.path / "app" / "a.txt").write_text("a0\n", encoding="utf-8")
    result = manager.cleanup(
        resource,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
    )
    assert result.worktree_removed is True
    assert result.branch_removed is False
    assert not resource.path.exists()
    assert git(repo, "show-ref", "--verify", f"refs/heads/{resource.branch}")


def test_join_applies_candidates_in_task_id_order_and_runs_integrated_acceptance(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    writer_b = create_writer(manager, ownership(repo, generated, base, "task-b"))
    writer_a = create_writer(manager, ownership(repo, generated, base, "task-a"))
    candidate_b = candidate_for(manager, writer_b, "app/b.txt", "B\n")
    candidate_a = candidate_for(manager, writer_a, "app/a.txt", "A\n")
    integration = create_writer(
        manager,
        ownership(repo, generated, base, "integration", kind="integration"),
    )
    main_head = git(repo, "rev-parse", "main")

    result = join_candidates(
        manager,
        integration,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        base_sha=base,
        candidates=[candidate_b, candidate_a],
        integrated_acceptance=[
            [
                "python3",
                "-c",
                "from pathlib import Path; "
                "assert Path('app/a.txt').read_text() == 'A\\n'; "
                "assert Path('app/b.txt').read_text() == 'B\\n'",
            ]
        ],
    )
    assert result.applied_task_ids == ("task-a", "task-b")
    assert result.changed_paths == ("app/a.txt", "app/b.txt")
    assert result.integrated_acceptance[0].exit_code == 0
    assert git(integration.path, "log", "--reverse", "--format=%s", f"{base}..HEAD").splitlines() == [
        "agent-orchestrator: plan-fixture/task-a",
        "agent-orchestrator: plan-fixture/task-b",
    ]
    assert git(repo, "rev-parse", "main") == main_head
    assert git(repo, "status", "--porcelain") == ""

    replay = create_writer(
        manager,
        ownership(repo, generated, base, "integration-replay", kind="integration"),
    )
    replay_result = join_candidates(
        manager,
        replay,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        base_sha=base,
        candidates=[candidate_a, candidate_b],
        integrated_acceptance=[],
    )
    assert replay_result.integration_head == result.integration_head


def test_join_rejects_overlap_rebase_dirty_and_shared_interface(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    first = create_writer(manager, ownership(repo, generated, base, "task-a"))
    second = create_writer(manager, ownership(repo, generated, base, "task-b"))
    candidate_a = candidate_for(manager, first, "app/a.txt", "first\n")
    candidate_b = candidate_for(manager, second, "app/a.txt", "second\n")
    integration = create_writer(
        manager,
        ownership(repo, generated, base, "integration", kind="integration"),
    )
    with pytest.raises(JoinDispute, match="overlap"):
        join_candidates(
            manager,
            integration,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            base_sha=base,
            candidates=[candidate_a, candidate_b],
            integrated_acceptance=[],
        )
    assert git(integration.path, "rev-parse", "HEAD") == base
    assert git(integration.path, "status", "--porcelain") == ""

    with pytest.raises(JoinDispute, match="rebase"):
        join_candidates(
            manager,
            integration,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            base_sha=base,
            candidates=[replace(candidate_a, parent_sha="0" * 40)],
            integrated_acceptance=[],
        )

    (integration.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(JoinDispute, match="dirty"):
        join_candidates(
            manager,
            integration,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            base_sha=base,
            candidates=[candidate_a],
            integrated_acceptance=[],
        )

    shared = create_writer(manager, ownership(repo, generated, base, "task-schema"))
    (shared.path / "schemas" / "model.yaml").write_text("version: 2\n", encoding="utf-8")
    inspected = manager.inspect_writer(
        shared,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        own=["schemas/model.yaml"],
        do_not_touch=[],
        shared_interface_paths=["schemas/model.yaml"],
    )
    shared_candidate = manager.commit_candidate(
        shared,
        inspected,
        current_run_id=RUN_ID,
        current_fencing_token=FENCE,
        plan_id="plan-fixture",
    )
    fresh_integration = create_writer(
        manager,
        ownership(repo, generated, base, "integration-2", kind="integration"),
    )
    with pytest.raises(JoinDispute, match="shared interface"):
        join_candidates(
            manager,
            fresh_integration,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            base_sha=base,
            candidates=[shared_candidate],
            integrated_acceptance=[],
        )


def test_integrated_acceptance_failure_is_not_mistaken_for_success(repository):
    repo, generated, base = repository
    manager = WorktreeManager(repo, generated)
    writer = create_writer(manager, ownership(repo, generated, base, "task-a"))
    candidate = candidate_for(manager, writer, "app/a.txt", "A\n")
    integration = create_writer(
        manager,
        ownership(repo, generated, base, "integration", kind="integration"),
    )

    with pytest.raises(JoinDispute, match="acceptance"):
        join_candidates(
            manager,
            integration,
            current_run_id=RUN_ID,
            current_fencing_token=FENCE,
            base_sha=base,
            candidates=[candidate],
            integrated_acceptance=[["python3", "-c", "raise SystemExit(9)"]],
        )
    assert git(repo, "rev-parse", "main") == base
    assert integration.path.exists()
