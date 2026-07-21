#!/usr/bin/env python3
"""Pinned thin launcher for the private Agent Run orchestrator package."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "orchestrator.lock.json"
CHECKOUT_ENV = "AGENT_RUN_ORCHESTRATOR_CHECKOUT"
# Locked commits are materialised here, one detached worktree per commit, so a
# launch never depends on where the development checkout happens to be parked.
PIN_CACHE = Path(
    os.environ.get("AGENT_RUN_ORCHESTRATOR_PIN_CACHE")
    or Path.home() / ".cache" / "agent-run" / "orchestrator-pins"
).expanduser()
LEGACY_COMPONENT_OVERRIDES = {
    "AGENT_RUN_ROUTING_CANON",
    "AGENT_RUN_PROVIDER_MANIFEST",
    "AGENT_RUN_PROVIDER_WRAPPER",
}


class OrchestratorAdapterError(RuntimeError):
    """The pinned private runtime is absent or has drifted."""


def _load_lock(path: Path | None = None) -> dict[str, Any]:
    # Resolved at call time, not bound at definition time: a module-level
    # default freezes the real repository path into the signature, so any caller
    # or test that redirects LOCK is silently ignored -- which is how a negative
    # promote test passed while reading the live lock.
    path = LOCK if path is None else path
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestratorAdapterError("orchestrator lock is unavailable") from exc
    if not isinstance(value, Mapping) or set(value) != {
        "version", "repository", "commit", "entrypoint"
    }:
        raise OrchestratorAdapterError("orchestrator lock has unexpected fields")
    if value.get("version") != 1 or not isinstance(value.get("commit"), str) or len(value["commit"]) != 40:
        raise OrchestratorAdapterError("orchestrator lock identity is invalid")
    return dict(value)


def _resolve_checkout() -> Path:
    explicit = os.environ.get(CHECKOUT_ENV)
    candidate = (
        Path(explicit).expanduser()
        if explicit
        else ROOT.parent / "agent-run-orchestrator"
    ).resolve()
    if not candidate.is_dir():
        raise OrchestratorAdapterError(
            f"private orchestrator checkout is unavailable; set {CHECKOUT_ENV}"
        )
    return candidate


def _git(checkout: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode:
        raise OrchestratorAdapterError("private orchestrator Git identity is unavailable")
    return completed.stdout.strip()


def _pin_root(commit: str) -> Path:
    return PIN_CACHE / commit


def _materialise_pin(checkout: Path, commit: str) -> Path:
    """Check the locked commit out into its own detached worktree.

    The adapter used to require that the *development* checkout's HEAD equal the
    lock and that its tree be clean. That coupled two unrelated things: which
    revision gets consumed (the lock's job) and where someone happens to be
    working (nobody's business). The orchestrator is actively developed, so its
    main is permanently ahead of the lock and the check refused to launch on
    ordinary days. Materialising the locked commit instead makes the guarantee
    stronger, not weaker: what runs is the locked tree itself, whatever the
    developer's checkout looks like.
    """

    pin = _pin_root(commit)
    if pin.is_dir():
        if _git(pin, "rev-parse", "HEAD") != commit:
            raise OrchestratorAdapterError(
                f"pinned worktree {pin} is not at {commit[:12]}; remove it and retry"
            )
        return pin
    if not _has_commit(checkout, commit):
        raise OrchestratorAdapterError(
            f"locked commit {commit[:12]} is not present in {checkout}; "
            f"run: git -C {checkout} fetch origin"
        )
    PIN_CACHE.mkdir(parents=True, exist_ok=True)
    _prune_pins(checkout, keep=commit)
    completed = subprocess.run(
        ["git", "-C", str(checkout), "worktree", "add", "--detach", str(pin), commit],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode or not pin.is_dir():
        # Another launcher may have won the race and created this very pin
        # between our existence check and our add. Losing that race is not a
        # failure, so re-verify before refusing: the alternative is one of two
        # concurrent live dispatches dying for no reason.
        if pin.is_dir() and _try_git(pin, "rev-parse", "HEAD") == commit:
            return pin
        raise OrchestratorAdapterError(
            f"cannot materialise pinned worktree at {pin}: "
            f"{completed.stderr.strip() or 'git worktree add failed'}"
        )
    return pin


def _try_git(checkout: Path, *args: str) -> str | None:
    """``_git`` that reports failure instead of raising, for race recovery."""

    try:
        return _git(checkout, *args)
    except OrchestratorAdapterError:
        return None


PIN_RETENTION = 3


def _prune_pins(checkout: Path, *, keep: str) -> None:
    """Retire old pins through Git, so the registry cannot grow without bound.

    Every promotion leaves its predecessor's worktree registered forever
    otherwise, and deleting the directory by hand leaves Git metadata that
    blocks the next add of the same commit. Best-effort: pruning must never be
    the reason a dispatch fails.
    """

    try:
        pins = sorted(
            (path for path in PIN_CACHE.iterdir() if path.is_dir() and path.name != keep),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in pins[PIN_RETENTION - 1:]:
        subprocess.run(
            ["git", "-C", str(checkout), "worktree", "remove", "--force", str(stale)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=60,
        )
    subprocess.run(
        ["git", "-C", str(checkout), "worktree", "prune"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=60,
    )


def _has_commit(checkout: Path, commit: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "-e", f"{commit}^{{commit}}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return completed.returncode == 0


def _verified_entrypoint(checkout: Path, lock: Mapping[str, Any]) -> Path:
    pin = _materialise_pin(checkout, str(lock["commit"]))
    if _git(pin, "status", "--porcelain", "--untracked-files=all"):
        # The pin is ours to keep pristine; anything written into it means the
        # tree no longer matches the reviewed commit it claims to be.
        raise OrchestratorAdapterError(
            f"pinned worktree {pin} is dirty; remove it and retry"
        )
    entrypoint = (pin / str(lock["entrypoint"])).resolve()
    try:
        entrypoint.relative_to(pin.resolve())
        info = entrypoint.lstat()
    except (ValueError, OSError) as exc:
        raise OrchestratorAdapterError("private orchestrator entrypoint is unavailable") from exc
    if entrypoint.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise OrchestratorAdapterError("private orchestrator entrypoint is unsafe")
    return entrypoint


def _delegated_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in LEGACY_COMPONENT_OVERRIDES:
        environment.pop(name, None)
    environment["AGENT_RUN_GOVERNANCE_ROOT"] = str(ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _canonical_remote_matches(checkout: Path, repository: str) -> bool:
    """Is this checkout actually the repository the lock names?"""

    url = _try_git(checkout, "remote", "get-url", "origin")
    if url is None:
        return False
    normalise = lambda value: value.rstrip("/").removesuffix(".git")  # noqa: E731
    return normalise(url) == normalise(repository)


def _promote(argv: list[str]) -> int:
    """Move the lock to a reviewed commit, recording why it was allowed.

    Promotion stays a deliberate act. Landing on the orchestrator's main is not
    the same as having been reviewed for consumption -- a green pipeline is a
    gate, not an exemption -- so the lock must never simply follow origin/main.
    What was missing was not automation but a ritual: before this, promotion
    meant hand-editing JSON, which is how the lock silently fell three commits
    behind and refused to launch.

    The write is transactional. The launcher reads the lock file, not the commit,
    so a half-done promotion is immediately live: it would consume a revision
    that no one can see in Git history. Any failure therefore restores the file
    exactly as it was.
    """

    parser = argparse.ArgumentParser(
        prog="agent-orchestrate promote",
        description="Point the orchestrator lock at a reviewed commit.",
    )
    parser.add_argument("commit", help="full or abbreviated commit sha to promote")
    parser.add_argument(
        "--evidence",
        required=True,
        help="why this commit may be consumed: tests run and review verdict",
    )
    args = parser.parse_args(argv)

    evidence = " ".join(args.evidence.split())
    if len(evidence) < 20:
        print(
            "agent-orchestrate promote: --evidence must state what was run and "
            "what it returned (tests, review verdict), not a placeholder",
            file=sys.stderr,
        )
        return 2

    try:
        lock = _load_lock()
        checkout = _resolve_checkout()
    except OrchestratorAdapterError as exc:
        print(f"agent-orchestrate promote: {exc}", file=sys.stderr)
        return 2

    # A lock that is already modified or staged means someone is mid-edit; a
    # promotion on top of that would commit their work under our message.
    status = _try_git(ROOT, "status", "--porcelain", "--", str(LOCK))
    if status is None:
        print("agent-orchestrate promote: governance root is not a Git checkout", file=sys.stderr)
        return 2
    if status.strip():
        print(
            f"agent-orchestrate promote: {LOCK.name} already has uncommitted changes; "
            "commit or restore it first",
            file=sys.stderr,
        )
        return 2

    if not _canonical_remote_matches(checkout, str(lock["repository"])):
        # Promoting from a fork would write a fork-only commit into a lock that
        # still claims the canonical repository: unobtainable, unauditable.
        print(
            f"agent-orchestrate promote: {checkout} origin is not {lock['repository']}; "
            "point AGENT_RUN_ORCHESTRATOR_CHECKOUT at the canonical repository",
            file=sys.stderr,
        )
        return 2

    resolved = _try_git(checkout, "rev-parse", f"{args.commit}^{{commit}}")
    if resolved is None:
        print(
            f"agent-orchestrate promote: {args.commit} is not a commit in {checkout}",
            file=sys.stderr,
        )
        return 2
    if resolved == lock["commit"]:
        print(f"agent-orchestrate promote: lock already at {resolved[:12]}")
        return 0

    subprocess.run(
        ["git", "-C", str(checkout), "fetch", "--quiet", "origin", "main"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=120,
    )
    reachable = subprocess.run(
        ["git", "-C", str(checkout), "merge-base", "--is-ancestor", resolved, "origin/main"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=30,
    )
    if reachable.returncode:
        print(
            f"agent-orchestrate promote: {resolved[:12]} is not on origin/main in "
            f"{checkout}; push it first",
            file=sys.stderr,
        )
        return 2

    candidate = dict(lock)
    candidate["commit"] = resolved
    try:
        # Prove the candidate is launchable before adopting it, rather than
        # discovering at the next dispatch that its entrypoint moved.
        _verified_entrypoint(checkout, candidate)
    except OrchestratorAdapterError as exc:
        print(f"agent-orchestrate promote: candidate is not launchable: {exc}", file=sys.stderr)
        return 2

    previous_text = LOCK.read_text(encoding="utf-8")
    previous = str(lock["commit"])
    LOCK.write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
    message = (
        f"chore: promote orchestrator lock to {resolved[:7]}\n\n"
        f"Previous: {previous}\n"
        f"Evidence: {evidence}\n"
    )
    # Commit this path only. A bare `git commit` would sweep whatever else the
    # index happens to hold into a promotion commit.
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "commit", "-m", message, "--only", "--", str(LOCK)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=60,
    )
    if completed.returncode:
        LOCK.write_text(previous_text, encoding="utf-8")
        print(
            "agent-orchestrate promote: commit failed, lock restored: "
            f"{completed.stderr.strip() or completed.stdout.strip()}",
            file=sys.stderr,
        )
        return 2

    print(f"agent-orchestrate promote: {previous[:12]} -> {resolved[:12]}")
    print(
        "next: push this governance commit, and make sure the checkout the "
        "launcher runs from is on it -- an unsynced governance root keeps "
        "executing the old lock."
    )
    return 0


def main() -> int:
    if sys.argv[1:2] == ["promote"]:
        return _promote(sys.argv[2:])
    try:
        lock = _load_lock()
        checkout = _resolve_checkout()
        entrypoint = _verified_entrypoint(checkout, lock)
    except OrchestratorAdapterError as exc:
        print(f"agent-orchestrate adapter: {exc}", file=sys.stderr)
        return 2
    os.execve(
        sys.executable,
        [sys.executable, str(entrypoint), *sys.argv[1:]],
        _delegated_environment(),
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
