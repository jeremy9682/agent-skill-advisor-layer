"""Fail-closed Git worktree ownership and writer-candidate controls.

The journal owns persistence of resource intents.  This module consumes a
write-ahead :class:`ResourceOwnership` value and refuses to create, reuse, or
remove anything that it cannot prove belongs to the current controller.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
from typing import Any, Callable, Iterable, Mapping, Sequence


class WorktreeError(RuntimeError):
    """A Git worktree operation failed without authorizing destructive repair."""


class UnsafeWorktreeError(WorktreeError):
    """Observed state does not match the current resource authority."""


class ScopeViolation(WorktreeError):
    """A writer changed a path outside its frozen scope."""


@dataclass(frozen=True)
class ResourceOwnership:
    """Write-ahead ownership contract supplied by the journal/controller."""

    created_by_run_id: str
    fencing_token: str
    repo_root: Path
    path: Path
    branch: str
    base_sha: str
    ledger_slug: str
    kind: str
    task_id: str | None = None
    state: str = "intent"

    def confirmed(self) -> "ResourceOwnership":
        return replace(self, state="created")


@dataclass(frozen=True)
class AcceptanceResult:
    command_index: int
    command_sha256: str
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True)
class WriterResult:
    task_id: str
    base_sha: str
    worktree_path: Path
    changed_paths: tuple[str, ...]
    diff_hash: str
    shared_interface_hits: tuple[str, ...]
    acceptance: tuple[AcceptanceResult, ...]


@dataclass(frozen=True)
class CandidateCommit:
    task_id: str
    commit_sha: str
    parent_sha: str
    base_sha: str
    worktree_path: Path
    changed_paths: tuple[str, ...]
    diff_hash: str
    shared_interface_hits: tuple[str, ...]
    acceptance: tuple[AcceptanceResult, ...]


@dataclass(frozen=True)
class ResumeObservation:
    path: Path
    branch: str
    base_sha: str
    head_sha: str
    ledger_slug: str
    diff_hash: str | None
    integration_head: str | None


@dataclass(frozen=True)
class CleanupResult:
    worktree: str
    branch: str
    worktree_removed: bool
    branch_removed: bool = False


DEFAULT_SHARED_INTERFACE_TAXONOMY: Mapping[str, tuple[str, ...]] = {
    "public_api": ("api/**", "**/api/**", "include/**", "**/public/**"),
    "schema": ("schemas/**", "**/schemas/**", "**/schema/**"),
    "migration": ("migrations/**", "**/migrations/**", "**/migration/**"),
    "configuration": (
        "config/**",
        "**/config/**",
        "*.toml",
        "*.yaml",
        "*.yml",
        ".github/**",
    ),
}

CommandRunner = Callable[[Sequence[str], Path], Any]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_relative(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScopeViolation("scope paths must be non-empty strings")
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ScopeViolation(f"unsafe scope path: {value!r}")
    return path.as_posix()


def _matches(path: str, declarations: Iterable[str]) -> bool:
    for raw in declarations:
        declaration = _normalize_relative(raw)
        if fnmatch.fnmatchcase(path, declaration):
            return True
        if not any(char in declaration for char in "*?[") and path.startswith(
            declaration.rstrip("/") + "/"
        ):
            return True
    return False


def classify_shared_interfaces(
    paths: Iterable[str],
    taxonomy: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Return taxonomy hits without granting authority to change them."""

    configured = taxonomy or DEFAULT_SHARED_INTERFACE_TAXONOMY
    hits: dict[str, list[str]] = {}
    for raw_path in paths:
        path = _normalize_relative(raw_path)
        for category, patterns in configured.items():
            if _matches(path, patterns):
                hits.setdefault(str(category), []).append(path)
    return {category: tuple(sorted(set(values))) for category, values in hits.items()}


def validate_changed_scope(
    changed_paths: Iterable[str],
    *,
    own: Sequence[str],
    do_not_touch: Sequence[str],
    shared_interface_paths: Sequence[str],
    taxonomy: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    """Validate changed paths and return declared shared-interface hits."""

    normalized = tuple(sorted({_normalize_relative(path) for path in changed_paths}))
    for path in normalized:
        if _matches(path, do_not_touch):
            raise ScopeViolation(f"changed do_not_touch path: {path}")
        if not (_matches(path, own) or _matches(path, shared_interface_paths)):
            raise ScopeViolation(f"changed path is outside declared scope: {path}")

    classified = classify_shared_interfaces(normalized, taxonomy)
    return tuple(sorted({path for values in classified.values() for path in values}))


def run_acceptance_commands(
    cwd: Path,
    commands: Sequence[Sequence[str]],
    *,
    runner: CommandRunner | Any | None = None,
) -> tuple[AcceptanceResult, ...]:
    """Run frozen argv commands without a shell and retain only hashed output."""

    results: list[AcceptanceResult] = []
    for index, raw_command in enumerate(commands):
        if isinstance(raw_command, (str, bytes)) or not raw_command:
            raise WorktreeError("acceptance commands must be non-empty argv sequences")
        command = tuple(str(part) for part in raw_command)
        if any(not part for part in command):
            raise WorktreeError("acceptance argv entries must be non-empty")

        if runner is None:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                check=False,
            )
        elif hasattr(runner, "run"):
            completed = runner.run(command, cwd=cwd)
        else:
            completed = runner(command, cwd)

        if isinstance(completed, int):
            returncode, stdout, stderr = completed, b"", b""
        elif isinstance(completed, Mapping):
            returncode = int(completed.get("returncode", completed.get("exit_code", 1)))
            stdout = completed.get("stdout", b"")
            stderr = completed.get("stderr", b"")
        else:
            returncode = int(completed.returncode)
            stdout = completed.stdout or b""
            stderr = completed.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")

        result = AcceptanceResult(
            command_index=index,
            command_sha256=_sha256(b"\0".join(part.encode("utf-8") for part in command)),
            exit_code=returncode,
            stdout_sha256=_sha256(stdout),
            stderr_sha256=_sha256(stderr),
        )
        results.append(result)
        if returncode != 0:
            raise WorktreeError(f"acceptance command {index} failed with exit {returncode}")
    return tuple(results)


class WorktreeManager:
    """Controller-only Git operations beneath one explicit generated root."""

    def __init__(self, repo_root: Path, allowed_root: Path):
        self.repo_root = repo_root.resolve()
        self.allowed_root = allowed_root.resolve()
        if not self.repo_root.is_dir():
            raise WorktreeError("repository root does not exist")
        if not self.allowed_root.is_dir() or self.allowed_root == Path(self.allowed_root.anchor):
            raise WorktreeError("allowed worktree root must be an existing narrow directory")
        observed = Path(self._git(self.repo_root, "rev-parse", "--show-toplevel").strip()).resolve()
        if observed != self.repo_root:
            raise WorktreeError("repo_root must be the Git top-level directory")

    def _git(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> str:
        command = ("git", "-C", str(cwd), *args)
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=None if env is None else {**os.environ, **env},
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise WorktreeError(f"Git operation failed ({args[0]}): {detail}")
        return completed.stdout.decode("utf-8", errors="strict")

    def _inside_allowed_root(self, path: Path) -> Path:
        if not path.is_absolute():
            raise UnsafeWorktreeError("managed worktree path must be absolute")
        resolved = path.resolve(strict=False)
        try:
            relative = resolved.relative_to(self.allowed_root)
        except ValueError as exc:
            raise UnsafeWorktreeError("managed path escapes the allowed root") from exc
        if not relative.parts:
            raise UnsafeWorktreeError("the allowed root itself is not a worktree target")
        return resolved

    def _validate_ownership(
        self,
        ownership: ResourceOwnership,
        *,
        current_run_id: str,
        current_fencing_token: str,
        required_state: str,
    ) -> Path:
        if ownership.created_by_run_id != current_run_id:
            raise UnsafeWorktreeError("resource belongs to another run")
        if ownership.fencing_token != current_fencing_token:
            raise UnsafeWorktreeError("resource fencing token is stale")
        if ownership.state != required_state:
            raise UnsafeWorktreeError(
                f"resource manifest must be in {required_state!r} state"
            )
        if ownership.kind not in {"writer", "integration"}:
            raise UnsafeWorktreeError("unknown managed resource kind")
        if ownership.kind == "writer" and not ownership.task_id:
            raise UnsafeWorktreeError("writer resource requires task_id")
        if not ownership.ledger_slug or any(
            char.isspace() for char in ownership.ledger_slug
        ):
            raise UnsafeWorktreeError("resource ownership requires a canonical ledger slug")
        if ownership.repo_root.resolve() != self.repo_root:
            raise UnsafeWorktreeError("resource repository does not match manager")
        path = self._inside_allowed_root(ownership.path)
        if ownership.path != path:
            raise UnsafeWorktreeError("resource path is not canonical")
        if not ownership.branch or ownership.branch.startswith("-"):
            raise UnsafeWorktreeError("unsafe branch name")
        self._git(self.repo_root, "check-ref-format", "--branch", ownership.branch)
        base = self._git(
            self.repo_root, "rev-parse", f"{ownership.base_sha}^{{commit}}"
        ).strip()
        if base != ownership.base_sha or len(base) != 40:
            raise UnsafeWorktreeError("base_sha is not the exact frozen commit")
        if required_state == "created":
            slug_path = path / ".agents" / "ledger-slug"
            slug = (
                slug_path.read_text(encoding="utf-8").strip()
                if slug_path.is_file()
                else ""
            )
            if slug != ownership.ledger_slug:
                raise UnsafeWorktreeError("ledger slug does not match resource ownership")
        return path

    def validate_created(
        self,
        ownership: ResourceOwnership,
        *,
        current_run_id: str,
        current_fencing_token: str,
    ) -> Path:
        """Expose the ownership seam to join without exposing Git mutation."""

        return self._validate_ownership(
            ownership,
            current_run_id=current_run_id,
            current_fencing_token=current_fencing_token,
            required_state="created",
        )

    def _worktree_records(self) -> list[dict[str, str]]:
        output = self._git(self.repo_root, "worktree", "list", "--porcelain")
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in output.splitlines():
            if not line:
                if current:
                    records.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            records.append(current)
        return records

    def _assert_no_collision(self, path: Path, branch: str) -> None:
        if path.exists() or path.is_symlink():
            raise UnsafeWorktreeError("worktree target already exists")
        branch_ref = f"refs/heads/{branch}"
        branch_exists = subprocess.run(
            ("git", "-C", str(self.repo_root), "show-ref", "--verify", "--quiet", branch_ref),
            check=False,
        ).returncode
        if branch_exists == 0:
            raise UnsafeWorktreeError("same-name branch already exists")
        if branch_exists not in {1}:
            raise WorktreeError("could not determine branch collision state")
        for record in self._worktree_records():
            record_path = Path(record.get("worktree", "")).resolve(strict=False)
            record_branch = record.get("branch", "").removeprefix("refs/heads/")
            if record_path == path or record_branch == branch:
                raise UnsafeWorktreeError("path or branch is already checked out")

    def create(
        self,
        ownership: ResourceOwnership,
        *,
        current_run_id: str,
        current_fencing_token: str,
        ledger_slug: str | None = None,
    ) -> ResourceOwnership:
        """Create from a journaled intent; never reuse or repair collisions."""

        path = self._validate_ownership(
            ownership,
            current_run_id=current_run_id,
            current_fencing_token=current_fencing_token,
            required_state="intent",
        )
        if ledger_slug is not None and ledger_slug != ownership.ledger_slug:
            raise UnsafeWorktreeError("ledger slug argument conflicts with ownership")
        if path.parent.resolve() != self.allowed_root and not path.parent.is_dir():
            raise UnsafeWorktreeError("nested worktree parent must already exist")
        self._assert_no_collision(path, ownership.branch)
        self._git(
            self.repo_root,
            "worktree",
            "add",
            "-b",
            ownership.branch,
            str(path),
            ownership.base_sha,
        )
        self._stamp_ledger_slug(path, ownership.ledger_slug)
        if self._git(path, "rev-parse", "HEAD").strip() != ownership.base_sha:
            raise UnsafeWorktreeError("new worktree did not start at frozen base")
        return ownership.confirmed()

    def _stamp_ledger_slug(self, worktree: Path, ledger_slug: str) -> None:
        relative = ".agents/ledger-slug"
        tracked = subprocess.run(
            ("git", "-C", str(worktree), "ls-files", "--error-unmatch", relative),
            capture_output=True,
            check=False,
        ).returncode == 0
        stamp = worktree / relative
        if tracked:
            if stamp.read_text(encoding="utf-8").strip() != ledger_slug:
                raise UnsafeWorktreeError("tracked ledger slug conflicts with canonical slug")
            return
        if stamp.exists() or stamp.is_symlink():
            raise UnsafeWorktreeError("untracked ledger slug already exists")
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(ledger_slug + "\n", encoding="utf-8")
        exclude_path = Path(self._git(worktree, "rev-parse", "--git-path", "info/exclude").strip())
        if not exclude_path.is_absolute():
            exclude_path = worktree / exclude_path
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exact_rule = "/.agents/ledger-slug"
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        lines = existing.splitlines()
        if exact_rule not in lines:
            with exclude_path.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(exact_rule + "\n")
        ignored = subprocess.run(
            ("git", "-C", str(worktree), "check-ignore", "--quiet", relative),
            check=False,
        ).returncode
        if ignored != 0:
            raise UnsafeWorktreeError("ledger slug exclusion is not effective")

    def changed_paths(self, worktree: Path, base_sha: str) -> tuple[str, ...]:
        tracked = self._git(
            worktree,
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            base_sha,
            "--",
        ).split("\0")
        untracked = self._git(
            worktree,
            "ls-files",
            "--others",
            "-z",
        ).split("\0")
        return tuple(
            sorted(
                {
                    _normalize_relative(path)
                    for path in (*tracked, *untracked)
                    if path and path != ".agents/ledger-slug"
                }
            )
        )

    def _assert_no_symlink_escape(self, worktree: Path, paths: Sequence[str]) -> None:
        root = worktree.resolve()
        for relative in paths:
            candidate = worktree / relative
            if not candidate.exists() and not candidate.is_symlink():
                continue
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ScopeViolation(f"changed path escapes through symlink: {relative}") from exc

    def diff_hash(self, worktree: Path, base_sha: str, paths: Sequence[str]) -> str:
        """Hash the resulting path states against a fixed base, including untracked files."""

        digest = hashlib.sha256()
        digest.update(b"agent-orchestration-tree-delta-v1\0")
        digest.update(base_sha.encode("ascii") + b"\0")
        for relative in sorted(paths):
            candidate = worktree / relative
            digest.update(relative.encode("utf-8") + b"\0")
            if not candidate.exists() and not candidate.is_symlink():
                digest.update(b"deleted\0")
                continue
            path_stat = candidate.lstat()
            if candidate.is_symlink():
                digest.update(b"120000\0")
                digest.update(b"symlink\0" + os.readlink(candidate).encode("utf-8"))
            elif candidate.is_file():
                mode = b"100755" if path_stat.st_mode & stat.S_IXUSR else b"100644"
                digest.update(mode + b"\0")
                digest.update(b"file\0" + candidate.read_bytes())
            else:
                raise ScopeViolation(f"changed path is not a file: {relative}")
            digest.update(b"\0")
        return digest.hexdigest()

    def commit_diff_hash(
        self,
        commit_sha: str,
        base_sha: str,
        paths: Sequence[str],
    ) -> str:
        """Recompute a candidate delta hash from immutable Git objects."""

        digest = hashlib.sha256()
        digest.update(b"agent-orchestration-tree-delta-v1\0")
        digest.update(base_sha.encode("ascii") + b"\0")
        for relative in sorted(paths):
            relative = _normalize_relative(relative)
            digest.update(relative.encode("utf-8") + b"\0")
            tree = subprocess.run(
                (
                    "git",
                    "-C",
                    str(self.repo_root),
                    "ls-tree",
                    "-z",
                    commit_sha,
                    "--",
                    relative,
                ),
                capture_output=True,
                check=False,
            )
            if tree.returncode != 0:
                raise UnsafeWorktreeError("cannot inspect candidate Git tree")
            if not tree.stdout:
                digest.update(b"deleted\0")
                continue
            header, _, observed_path = tree.stdout.rstrip(b"\0").partition(b"\t")
            mode, object_type, _object_id = header.split(b" ", 2)
            if observed_path.decode("utf-8") != relative or object_type != b"blob":
                raise UnsafeWorktreeError("candidate path is not one expected Git blob")
            content = subprocess.run(
                (
                    "git",
                    "-C",
                    str(self.repo_root),
                    "show",
                    f"{commit_sha}:{relative}",
                ),
                capture_output=True,
                check=False,
            )
            if content.returncode != 0:
                raise UnsafeWorktreeError("cannot read candidate Git blob")
            digest.update(mode + b"\0")
            if mode == b"120000":
                digest.update(b"symlink\0" + content.stdout)
            else:
                digest.update(b"file\0" + content.stdout)
            digest.update(b"\0")
        return digest.hexdigest()

    def inspect_writer(
        self,
        ownership: ResourceOwnership,
        *,
        current_run_id: str,
        current_fencing_token: str,
        own: Sequence[str],
        do_not_touch: Sequence[str],
        shared_interface_paths: Sequence[str],
        acceptance_commands: Sequence[Sequence[str]] = (),
        taxonomy: Mapping[str, Sequence[str]] | None = None,
        runner: CommandRunner | Any | None = None,
    ) -> WriterResult:
        path = self._validate_ownership(
            ownership,
            current_run_id=current_run_id,
            current_fencing_token=current_fencing_token,
            required_state="created",
        )
        if ownership.kind != "writer" or ownership.task_id is None:
            raise UnsafeWorktreeError("writer inspection requires writer ownership")
        head = self._git(path, "rev-parse", "HEAD").strip()
        if head != ownership.base_sha:
            raise UnsafeWorktreeError("writer created a commit or changed HEAD")
        paths = self.changed_paths(path, ownership.base_sha)
        if not paths:
            raise ScopeViolation("writer produced no changed paths")
        self._assert_no_symlink_escape(path, paths)
        shared_hits = validate_changed_scope(
            paths,
            own=own,
            do_not_touch=do_not_touch,
            shared_interface_paths=shared_interface_paths,
            taxonomy=taxonomy,
        )
        diff_hash = self.diff_hash(path, ownership.base_sha, paths)
        acceptance = run_acceptance_commands(
            path, acceptance_commands, runner=runner
        )
        if self._git(path, "rev-parse", "HEAD").strip() != ownership.base_sha:
            raise UnsafeWorktreeError("acceptance command changed writer HEAD")
        if self.diff_hash(path, ownership.base_sha, self.changed_paths(path, ownership.base_sha)) != diff_hash:
            raise UnsafeWorktreeError("acceptance command changed the validated diff")
        return WriterResult(
            task_id=ownership.task_id,
            base_sha=ownership.base_sha,
            worktree_path=path,
            changed_paths=paths,
            diff_hash=diff_hash,
            shared_interface_hits=shared_hits,
            acceptance=acceptance,
        )

    def commit_candidate(
        self,
        ownership: ResourceOwnership,
        result: WriterResult,
        *,
        current_run_id: str,
        current_fencing_token: str,
        plan_id: str,
        controller_name: str = "Agent Run Controller",
        controller_email: str = "agent-run-controller@localhost",
    ) -> CandidateCommit:
        path = self._validate_ownership(
            ownership,
            current_run_id=current_run_id,
            current_fencing_token=current_fencing_token,
            required_state="created",
        )
        if ownership.task_id != result.task_id or result.worktree_path != path:
            raise UnsafeWorktreeError("writer result does not match resource ownership")
        if self._git(path, "rev-parse", "HEAD").strip() != ownership.base_sha:
            raise UnsafeWorktreeError("writer HEAD changed before controller commit")
        paths = self.changed_paths(path, ownership.base_sha)
        if tuple(paths) != result.changed_paths:
            raise UnsafeWorktreeError("writer paths changed after validation")
        if self.diff_hash(path, ownership.base_sha, paths) != result.diff_hash:
            raise UnsafeWorktreeError("writer diff changed after validation")

        base_date = self._git(
            self.repo_root, "show", "-s", "--format=%cI", ownership.base_sha
        ).strip()
        env = {
            "GIT_AUTHOR_NAME": controller_name,
            "GIT_AUTHOR_EMAIL": controller_email,
            "GIT_COMMITTER_NAME": controller_name,
            "GIT_COMMITTER_EMAIL": controller_email,
            "GIT_AUTHOR_DATE": base_date,
            "GIT_COMMITTER_DATE": base_date,
        }
        self._git(path, "add", "--all", "--", *paths)
        self._git(
            path,
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-gpg-sign",
            "-m",
            f"agent-orchestrator: {plan_id}/{result.task_id}",
            env=env,
        )
        head = self._git(path, "rev-parse", "HEAD").strip()
        parent = self._git(path, "rev-parse", f"{head}^").strip()
        count = int(self._git(path, "rev-list", "--count", f"{ownership.base_sha}..{head}").strip())
        if parent != ownership.base_sha or count != 1:
            raise UnsafeWorktreeError("candidate is not exactly one child of frozen base")
        if self._git(path, "status", "--porcelain").strip():
            raise UnsafeWorktreeError("candidate worktree is dirty after controller commit")
        committed_paths = tuple(
            sorted(
                path
                for path in self._git(
                    path,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "--no-renames",
                    head,
                ).splitlines()
                if path
            )
        )
        if committed_paths != result.changed_paths:
            raise UnsafeWorktreeError("candidate commit paths differ from validated diff")
        if self.commit_diff_hash(head, ownership.base_sha, committed_paths) != result.diff_hash:
            raise UnsafeWorktreeError("candidate commit content differs from validated diff")
        return CandidateCommit(
            task_id=result.task_id,
            commit_sha=head,
            parent_sha=parent,
            base_sha=ownership.base_sha,
            worktree_path=path,
            changed_paths=committed_paths,
            diff_hash=result.diff_hash,
            shared_interface_hits=result.shared_interface_hits,
            acceptance=result.acceptance,
        )

    def reconcile(
        self,
        ownership: ResourceOwnership,
        *,
        current_run_id: str,
        current_fencing_token: str,
        expected_head: str,
        expected_ledger_slug: str,
        expected_diff_hash: str | None = None,
        expected_integration_head: str | None = None,
    ) -> ResumeObservation:
        path = self._validate_ownership(
            ownership,
            current_run_id=current_run_id,
            current_fencing_token=current_fencing_token,
            required_state="created",
        )
        matching = [
            record
            for record in self._worktree_records()
            if Path(record.get("worktree", "")).resolve(strict=False) == path
        ]
        if len(matching) != 1:
            raise UnsafeWorktreeError("worktree list does not contain one matching resource")
        record = matching[0]
        branch = record.get("branch", "").removeprefix("refs/heads/")
        head = self._git(path, "rev-parse", "HEAD").strip()
        slug_path = path / ".agents" / "ledger-slug"
        slug = slug_path.read_text(encoding="utf-8").strip() if slug_path.is_file() else ""
        if branch != ownership.branch or head != expected_head or slug != expected_ledger_slug:
            raise UnsafeWorktreeError("worktree identity drifted since manifest confirmation")
        paths = self.changed_paths(path, ownership.base_sha)
        observed_diff = self.diff_hash(path, ownership.base_sha, paths) if paths else None
        if expected_diff_hash != observed_diff:
            raise UnsafeWorktreeError("worktree diff hash drifted")
        if ownership.kind == "integration" and expected_integration_head != head:
            raise UnsafeWorktreeError("integration HEAD drifted")
        return ResumeObservation(
            path=path,
            branch=branch,
            base_sha=ownership.base_sha,
            head_sha=head,
            ledger_slug=slug,
            diff_hash=observed_diff,
            integration_head=head if ownership.kind == "integration" else None,
        )

    def cleanup(
        self,
        ownership: ResourceOwnership,
        *,
        current_run_id: str,
        current_fencing_token: str,
    ) -> CleanupResult:
        """Remove only a clean, currently-owned worktree; never delete its branch."""

        path = self._validate_ownership(
            ownership,
            current_run_id=current_run_id,
            current_fencing_token=current_fencing_token,
            required_state="created",
        )
        if not path.exists():
            raise UnsafeWorktreeError("manifest resource path is missing; preserve for dispute")
        matching = [
            record
            for record in self._worktree_records()
            if Path(record.get("worktree", "")).resolve(strict=False) == path
        ]
        if len(matching) != 1:
            raise UnsafeWorktreeError("resource is unknown to git worktree list")
        branch = matching[0].get("branch", "").removeprefix("refs/heads/")
        if branch != ownership.branch:
            raise UnsafeWorktreeError("worktree branch does not match ownership")
        if self._git(path, "status", "--porcelain").strip():
            raise UnsafeWorktreeError("dirty managed worktree is preserved for inspection")
        self._git(self.repo_root, "worktree", "remove", str(path))
        return CleanupResult(
            worktree=str(path),
            branch=ownership.branch,
            worktree_removed=True,
            branch_removed=False,
        )
