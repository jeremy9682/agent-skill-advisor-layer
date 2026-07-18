"""Deterministic, mechanical integration of controller-created candidates."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from .worktree import (
    AcceptanceResult,
    CandidateCommit,
    CommandRunner,
    ResourceOwnership,
    WorktreeError,
    WorktreeManager,
    run_acceptance_commands,
)


class JoinDispute(RuntimeError):
    """The candidate set requires human resolution and is preserved in place."""


@dataclass(frozen=True)
class IntegrationResult:
    base_sha: str
    integration_head: str
    integration_path: Path
    applied_task_ids: tuple[str, ...]
    changed_paths: tuple[str, ...]
    integrated_acceptance: tuple[AcceptanceResult, ...]


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ("git", "-C", str(cwd), *args),
        capture_output=True,
        check=False,
        env=None if env is None else {**os.environ, **env},
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise JoinDispute(f"Git integration step failed ({args[0]}): {detail}")
    return completed


def _stdout(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.decode("utf-8", errors="strict").strip()


def _validate_candidates(
    repo_root: Path,
    candidates: Sequence[CandidateCommit],
    base_sha: str,
) -> tuple[CandidateCommit, ...]:
    if not candidates:
        raise JoinDispute("join requires at least one candidate")
    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.task_id))
    if len({candidate.task_id for candidate in ordered}) != len(ordered):
        raise JoinDispute("candidate task IDs must be unique")

    owner_by_path: dict[str, str] = {}
    for candidate in ordered:
        if candidate.base_sha != base_sha or candidate.parent_sha != base_sha:
            raise JoinDispute(f"candidate {candidate.task_id} requires rebase")
        observed = _stdout(repo_root, "rev-parse", f"{candidate.commit_sha}^{{commit}}")
        if observed != candidate.commit_sha:
            raise JoinDispute(f"candidate {candidate.task_id} commit is missing")
        parent = _stdout(repo_root, "rev-parse", f"{candidate.commit_sha}^")
        if parent != base_sha:
            raise JoinDispute(f"candidate {candidate.task_id} ancestry drifted")
        paths = tuple(
            sorted(
                path
                for path in _stdout(
                    repo_root,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "--no-renames",
                    candidate.commit_sha,
                ).splitlines()
                if path
            )
        )
        if paths != candidate.changed_paths:
            raise JoinDispute(f"candidate {candidate.task_id} path evidence drifted")
        if candidate.shared_interface_hits:
            raise JoinDispute(
                f"candidate {candidate.task_id} changes a shared interface: "
                + ", ".join(candidate.shared_interface_hits)
            )
        for path in paths:
            previous = owner_by_path.setdefault(path, candidate.task_id)
            if previous != candidate.task_id:
                raise JoinDispute(
                    f"candidate path overlap: {path} ({previous}, {candidate.task_id})"
                )
    return ordered


def join_candidates(
    manager: WorktreeManager,
    integration: ResourceOwnership,
    *,
    current_run_id: str,
    current_fencing_token: str,
    base_sha: str,
    candidates: Sequence[CandidateCommit],
    integrated_acceptance: Sequence[Sequence[str]],
    runner: CommandRunner | Any | None = None,
    controller_name: str = "Agent Run Controller",
    controller_email: str = "agent-run-controller@localhost",
) -> IntegrationResult:
    """Apply candidates by task ID and run acceptance on the frozen integration HEAD.

    The integration worktree must already have been created through
    :class:`WorktreeManager` from a confirmed write-ahead resource intent.
    Conflicts and dirty state are deliberately left in place for inspection.
    """

    try:
        path = manager.validate_created(
            integration,
            current_run_id=current_run_id,
            current_fencing_token=current_fencing_token,
        )
    except WorktreeError as exc:
        raise JoinDispute(f"integration ownership is invalid: {exc}") from exc
    if integration.kind != "integration" or integration.base_sha != base_sha:
        raise JoinDispute("integration resource does not match the frozen base")
    if not path.is_dir():
        raise JoinDispute("integration worktree does not exist")
    if _stdout(path, "rev-parse", "HEAD") != base_sha:
        raise JoinDispute("integration HEAD is not the frozen base")
    if _stdout(path, "status", "--porcelain"):
        raise JoinDispute("integration worktree is dirty before join")

    ordered = _validate_candidates(manager.repo_root, candidates, base_sha)
    for candidate in ordered:
        try:
            observed_hash = manager.commit_diff_hash(
                candidate.commit_sha, base_sha, candidate.changed_paths
            )
        except WorktreeError as exc:
            raise JoinDispute(
                f"candidate {candidate.task_id} diff evidence cannot be verified: {exc}"
            ) from exc
        if observed_hash != candidate.diff_hash:
            raise JoinDispute(f"candidate {candidate.task_id} diff hash drifted")
    base_date = _stdout(manager.repo_root, "show", "-s", "--format=%cI", base_sha)
    env = {
        "GIT_COMMITTER_NAME": controller_name,
        "GIT_COMMITTER_EMAIL": controller_email,
        "GIT_COMMITTER_DATE": base_date,
    }
    applied: list[str] = []
    for candidate in ordered:
        completed = _git(
            path,
            "-c",
            "core.hooksPath=/dev/null",
            "cherry-pick",
            candidate.commit_sha,
            check=False,
            env=env,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise JoinDispute(
                f"candidate {candidate.task_id} did not apply cleanly: {detail}"
            )
        applied.append(candidate.task_id)
        for relative in candidate.changed_paths:
            candidate_blob = _git(
                manager.repo_root,
                "rev-parse",
                f"{candidate.commit_sha}:{relative}",
                check=False,
            )
            integrated_blob = _git(
                path,
                "rev-parse",
                f"HEAD:{relative}",
                check=False,
            )
            if candidate_blob.returncode != integrated_blob.returncode or (
                candidate_blob.returncode == 0
                and candidate_blob.stdout.strip() != integrated_blob.stdout.strip()
            ):
                raise JoinDispute(
                    f"integrated content differs for {candidate.task_id}:{relative}"
                )

    if _stdout(path, "status", "--porcelain"):
        raise JoinDispute("integration worktree is dirty after apply")
    head = _stdout(path, "rev-parse", "HEAD")
    ancestor = _git(path, "merge-base", "--is-ancestor", base_sha, head, check=False)
    if ancestor.returncode != 0:
        raise JoinDispute("frozen base is not an ancestor of integration HEAD")
    try:
        results = run_acceptance_commands(path, integrated_acceptance, runner=runner)
    except WorktreeError as exc:
        raise JoinDispute(f"integrated acceptance failed: {exc}") from exc
    if _stdout(path, "rev-parse", "HEAD") != head:
        raise JoinDispute("integrated acceptance changed HEAD")
    if _stdout(path, "status", "--porcelain"):
        raise JoinDispute("integrated acceptance left dirty state")
    changed_paths = tuple(
        sorted(
            path_name
            for path_name in _stdout(
                path,
                "diff",
                "--name-only",
                "--no-renames",
                base_sha,
                head,
                "--",
            ).splitlines()
            if path_name
        )
    )
    return IntegrationResult(
        base_sha=base_sha,
        integration_head=head,
        integration_path=path,
        applied_task_ids=tuple(applied),
        changed_paths=changed_paths,
        integrated_acceptance=results,
    )


__all__ = ["IntegrationResult", "JoinDispute", "join_candidates"]
