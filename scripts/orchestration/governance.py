"""Resolve the external governance control plane without copying its canon."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
from types import ModuleType
from typing import Any


ENV_GOVERNANCE_ROOT = "AGENT_RUN_GOVERNANCE_ROOT"
ENV_ROUTING_CANON = "AGENT_RUN_ROUTING_CANON"
ENV_PROVIDER_MANIFEST = "AGENT_RUN_PROVIDER_MANIFEST"
ENV_PROVIDER_WRAPPER = "AGENT_RUN_PROVIDER_WRAPPER"
POINTER_FILE = Path.home() / ".config" / "agent-run" / "governance-root"


class GovernanceAdapterError(RuntimeError):
    """The external governance seam is absent, ambiguous, or unsafe."""


def _regular_file(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise GovernanceAdapterError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise GovernanceAdapterError(f"{label} must be a non-symlink regular file")
    return path.resolve()


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    explicit = os.environ.get(ENV_GOVERNANCE_ROOT)
    if explicit:
        roots.append(Path(explicit).expanduser())
    roots.append(Path(__file__).resolve().parents[2])
    roots.append(Path(__file__).resolve().parents[3] / "agent-skill-advisor-layer")
    if POINTER_FILE.exists():
        info = POINTER_FILE.lstat()
        if POINTER_FILE.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise GovernanceAdapterError("governance pointer must be a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise GovernanceAdapterError("governance pointer must have mode 0600")
        roots.append(Path(POINTER_FILE.read_text(encoding="utf-8").strip()).expanduser())
    return roots


def governance_root() -> Path:
    seen: set[Path] = set()
    required = (
        "routing-policy.yaml",
        "agent-providers.yaml",
        "scripts/routing_runtime.py",
        "scripts/agent_provider_run.py",
    )
    for candidate in _candidate_roots():
        if not str(candidate):
            continue
        root = candidate.resolve()
        if root in seen:
            continue
        seen.add(root)
        if all((root / relative).is_file() for relative in required):
            for relative in required:
                _regular_file(root / relative, relative)
            return root
    raise GovernanceAdapterError(
        f"governance checkout not found; set {ENV_GOVERNANCE_ROOT}"
    )


def routing_canon_path() -> Path:
    raw = os.environ.get(ENV_ROUTING_CANON)
    return _regular_file(
        Path(raw).expanduser() if raw else governance_root() / "routing-policy.yaml",
        "routing canon",
    )


def provider_manifest_path() -> Path:
    raw = os.environ.get(ENV_PROVIDER_MANIFEST)
    return _regular_file(
        Path(raw).expanduser() if raw else governance_root() / "agent-providers.yaml",
        "provider manifest",
    )


def provider_wrapper_path() -> Path:
    raw = os.environ.get(ENV_PROVIDER_WRAPPER)
    return _regular_file(
        Path(raw).expanduser()
        if raw
        else governance_root() / "scripts" / "agent_provider_run.py",
        "governed provider wrapper",
    )


_ROUTING_MODULE: ModuleType | None = None


def routing_module() -> ModuleType:
    global _ROUTING_MODULE
    if _ROUTING_MODULE is not None:
        return _ROUTING_MODULE
    path = _regular_file(
        governance_root() / "scripts" / "routing_runtime.py",
        "routing runtime",
    )
    spec = importlib.util.spec_from_file_location("agent_run_governance_runtime", path)
    if spec is None or spec.loader is None:
        raise GovernanceAdapterError("routing runtime import spec is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _ROUTING_MODULE = module
    return module


def load_routing_canon(path: Path | None = None) -> dict[str, Any]:
    return routing_module().load_routing_canon(path or routing_canon_path())


def resolve_binding(*args: Any, **kwargs: Any) -> Any:
    return routing_module().resolve_binding(*args, **kwargs)


def resolve_model_family(*args: Any, **kwargs: Any) -> Any:
    return routing_module().resolve_model_family(*args, **kwargs)


__all__ = [
    "GovernanceAdapterError",
    "governance_root",
    "load_routing_canon",
    "provider_manifest_path",
    "provider_wrapper_path",
    "resolve_binding",
    "resolve_model_family",
    "routing_canon_path",
]
