"""Every model a task shape routes to must be accepted by provider preflight."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_every_routed_claude_model_is_in_provider_options():
    policy = _load("routing-policy.yaml")
    providers = _load("agent-providers.yaml")
    claude = next(
        p for p in (providers.get("providers") or {}).values()
        if isinstance(p, dict) and p.get("family") == "anthropic"
    ) if isinstance(providers.get("providers"), dict) else None
    assert claude is not None, "anthropic provider not found in agent-providers.yaml"
    options = set(claude["model_options"])
    missing = []
    for shape, spec in (policy.get("task_shapes") or {}).items():
        model = ((spec or {}).get("execution_model") or {}).get("claude")
        if model and model not in options:
            missing.append((shape, model))
    assert missing == [], f"routed models rejected by provider preflight: {missing}"
