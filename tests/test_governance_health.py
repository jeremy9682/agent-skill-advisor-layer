from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "governance_health", ROOT / "scripts" / "governance_health.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_never_claims_network_model_or_quota_checks():
    module = _module()
    report = module._report(
        "inspect", [module._check("fixture", True, "ok")]
    )["governance_health"]
    assert report["status"] == "ready"
    assert report["network_calls"] == 0
    assert report["model_calls"] == 0
    assert report["quota_checks"] == 0


def test_probe_contract_is_json_and_no_write(monkeypatch, tmp_path):
    module = _module()
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path))
    responses = iter(
        [
            (0, {"hookSpecificOutput": {"additionalContext": "suggest-confirm"}}),
            (0, {}),
            (0, {}),
        ]
    )
    monkeypatch.setattr(module, "_router_probe", lambda _prompt: next(responses))
    report = module.probe()["governance_health"]
    assert report["status"] == "ready"
    assert report["checks"][-1]["name"] == "probe_zero_write"


def test_real_probe_is_parseable_and_write_disabled():
    module = _module()
    code, payload = module._router_probe(
        "[SYSTEM NOTIFICATION - NOT USER INPUT] run research"
    )
    assert code == 0
    assert json.dumps(payload, sort_keys=True) == "{}"
