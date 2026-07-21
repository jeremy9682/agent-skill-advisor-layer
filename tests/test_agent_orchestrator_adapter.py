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


def test_adapter_rejects_a_dirty_pin(monkeypatch, tmp_path):
    """Drift protection moved from the development checkout to the pin.

    The adapter used to reject a development checkout whose HEAD differed from
    the lock or whose tree was dirty. That guarded the wrong thing: the lock
    decides which revision is consumed, and where a developer is working is
    unrelated -- so the check only ever fired on ordinary days. The tree that
    must stay pristine is the materialised pin, because a write into it means
    what runs is no longer the reviewed commit it claims to be.
    """

    module = _module()
    pin = tmp_path / "pin"
    entrypoint = pin / "scripts" / "agent_orchestrate.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("pass\n", encoding="utf-8")
    lock = {
        "version": 1,
        "repository": "private",
        "commit": "a" * 40,
        "entrypoint": "scripts/agent_orchestrate.py",
    }
    monkeypatch.setattr(module, "_materialise_pin", lambda *_args: pin)

    for status in (" M scripts/agent_orchestrate.py", "?? untracked-provider-wrapper.py"):
        monkeypatch.setattr(module, "_git", lambda *_args, _s=status: _s)
        with pytest.raises(module.OrchestratorAdapterError, match="dirty"):
            module._verified_entrypoint(tmp_path, lock)

    monkeypatch.setattr(module, "_git", lambda *_args: "")
    assert module._verified_entrypoint(tmp_path, lock) == entrypoint.resolve()


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
