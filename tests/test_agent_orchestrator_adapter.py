from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "agent_orchestrator_adapter", ROOT / "scripts" / "agent_orchestrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lock_is_strict_and_points_to_private_runtime():
    module = _module()
    lock = module._load_lock()
    assert lock["repository"].endswith("/agent-run-orchestrator")
    assert len(lock["commit"]) == 40
    assert lock["entrypoint"] == "scripts/agent_orchestrate.py"


def test_adapter_rejects_commit_or_dirty_drift(monkeypatch, tmp_path):
    module = _module()
    entrypoint = tmp_path / "scripts" / "agent_orchestrate.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text("pass\n", encoding="utf-8")
    lock = {
        "version": 1,
        "repository": "private",
        "commit": "a" * 40,
        "entrypoint": "scripts/agent_orchestrate.py",
    }
    monkeypatch.setattr(module, "_git", lambda *_args: "b" * 40)
    with pytest.raises(module.OrchestratorAdapterError, match="differs"):
        module._verified_entrypoint(tmp_path, lock)

    responses = iter(["a" * 40, " M scripts/agent_orchestrate.py"])
    monkeypatch.setattr(module, "_git", lambda *_args: next(responses))
    with pytest.raises(module.OrchestratorAdapterError, match="dirty"):
        module._verified_entrypoint(tmp_path, lock)

    responses = iter(["a" * 40, "?? untracked-provider-wrapper.py"])
    monkeypatch.setattr(module, "_git", lambda *_args: next(responses))
    with pytest.raises(module.OrchestratorAdapterError, match="dirty"):
        module._verified_entrypoint(tmp_path, lock)


def test_delegated_environment_forces_one_governance_root(monkeypatch):
    module = _module()
    for name in module.LEGACY_COMPONENT_OVERRIDES:
        monkeypatch.setenv(name, "/tmp/attacker")

    environment = module._delegated_environment()

    assert environment["AGENT_RUN_GOVERNANCE_ROOT"] == str(module.ROOT)
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not (module.LEGACY_COMPONENT_OVERRIDES & set(environment))


def test_lock_rejects_authority_fields(tmp_path):
    module = _module()
    path = tmp_path / "lock.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "repository": "private",
                "commit": "a" * 40,
                "entrypoint": "scripts/agent_orchestrate.py",
                "model": "must-not-be-here",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.OrchestratorAdapterError, match="unexpected"):
        module._load_lock(path)
