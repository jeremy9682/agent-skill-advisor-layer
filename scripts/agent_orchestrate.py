#!/usr/bin/env python3
"""Pinned thin launcher for the private Agent Run orchestrator package."""

from __future__ import annotations

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
LEGACY_COMPONENT_OVERRIDES = {
    "AGENT_RUN_ROUTING_CANON",
    "AGENT_RUN_PROVIDER_MANIFEST",
    "AGENT_RUN_PROVIDER_WRAPPER",
}


class OrchestratorAdapterError(RuntimeError):
    """The pinned private runtime is absent or has drifted."""


def _load_lock(path: Path = LOCK) -> dict[str, Any]:
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


def _verified_entrypoint(checkout: Path, lock: Mapping[str, Any]) -> Path:
    head = _git(checkout, "rev-parse", "HEAD")
    if head != lock["commit"]:
        raise OrchestratorAdapterError("private orchestrator commit differs from lock")
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise OrchestratorAdapterError("private orchestrator worktree is dirty")
    entrypoint = (checkout / str(lock["entrypoint"])).resolve()
    try:
        entrypoint.relative_to(checkout)
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


def main() -> int:
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
