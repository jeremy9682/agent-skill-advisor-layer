from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "routing_runtime", ROOT / "scripts" / "routing_runtime.py"
)
routing = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(routing)


def test_runtime_routes_have_one_canon_and_preserve_existing_binding():
    manifest = yaml.safe_load((ROOT / "agent-providers.yaml").read_text())
    assert "routes" not in manifest

    canon = routing.load_routing_canon(ROOT / manifest["routing_canon"])
    assert canon["runtime_routes"]["ordinary_bug_fix"] == {
        "policy_ref": "task_shapes.ordinary_bug_fix",
        "policy_family": "codex",
        "provider": "codex",
        "seat": "codex-landing",
    }
    binding = routing.resolve_binding(canon, "ordinary_bug_fix")

    assert binding == {
        "provider": "codex",
        "model": "gpt-5.6-terra",
        "effort": "medium",
        "seat": "codex-landing",
        "route_policy": "enabled",
        "review_independence": "not-applicable",
        "governance_effort": "medium",
    }


def test_cursor_catalog_and_model_family_are_discovered_not_hard_coded():
    manifest = yaml.safe_load((ROOT / "agent-providers.yaml").read_text())
    assert "cursor" in manifest["providers"]
    assert "cursor-auto" not in manifest["providers"]
    assert manifest["provider_aliases"]["cursor-auto"] == "cursor"

    output = """Available models

auto - Auto (current, default)
cursor-grok-4.5-high - Cursor Grok 4.5
composer-2.5 - Composer 2.5

Tip: use --model <id>
"""
    assert routing.parse_cursor_model_catalog(output) == [
        {"id": "auto", "label": "Auto (current, default)"},
        {"id": "cursor-grok-4.5-high", "label": "Cursor Grok 4.5"},
        {"id": "composer-2.5", "label": "Composer 2.5"},
    ]

    provider = manifest["providers"]["cursor"]
    assert routing.resolve_model_family(provider, "auto") == "undisclosed"
    assert routing.resolve_model_family(provider, "composer-2.5") == "cursor"
    assert routing.resolve_model_family(provider, "cursor-grok-4.5-high") == "xai"
    assert routing.resolve_model_family(provider, "cursor-claude-5-high") == "anthropic"
    assert routing.resolve_model_family(provider, "cursor-gpt-5.6-sol") == "openai"
    assert (
        routing.resolve_model_family(provider, "future-opaque-model") == "undisclosed"
    )


def test_independent_supplement_preserves_canon_eligible_producer_routes():
    canon = routing.load_routing_canon(ROOT / "routing-policy.yaml")
    binding = routing.resolve_binding(canon, "secondary_final_review")
    assert binding["review_independence"] == "independent-supplement"
    assert binding["eligible_producer_routes"] == ["ordinary_bug_fix"]


def test_instruction_bom_is_stable_private_and_changes_with_instruction_bytes(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    instruction = workspace / "AGENTS.md"
    instruction.write_text("private instruction alpha\n")
    canon = workspace / "routing-policy.yaml"
    canon.write_text("version: 1\nruntime_routes: {}\n")
    intent = workspace / "intent.md"
    intent.write_text("goal: test\n")
    provider = {
        "family": "cursor",
        "session": {"adapter": "cursor"},
        "commands": {"read-only": ["{binary}", "--model", "{model}"]},
        "model_family_rules": [{"glob": "cursor-grok-*", "family": "xai"}],
        "require_named_model_health": True,
        "requires_workspace_trust": True,
        "mcp_capabilities": {
            "status": "enumerated",
            "names": ["safe-capability", "https://secret.example/token=do-not-store"],
        },
    }
    kwargs = {
        "cwd": workspace,
        "provider_id": "cursor",
        "provider": provider,
        "provider_version": "2026.07.09-test",
        "canon_path": canon,
        "route_name": "explicit-provider",
        "binding": {
            "model": "composer-2.5",
            "effort": "provider-default",
            "seat": "codex-landing",
            "risk_triggers": [],
        },
        "prompt_sha256": "a" * 64,
        "skill_evidence": {"selected": [{"name": "tdd", "digest": "b" * 64}]},
        "intent_ref": "intent.md",
        "mode": "read-only",
    }
    first = routing.build_instruction_bom(**kwargs)
    second = routing.build_instruction_bom(**kwargs)
    assert first == second
    assert len(first["digest"]) == 64
    raw = yaml.safe_dump(first)
    assert "private instruction alpha" not in raw
    assert str(tmp_path) not in raw
    assert "safe-capability" not in raw
    assert "secret.example" not in raw
    assert first["mcp"]["capability_count"] == 2
    assert set(first["mcp"]) == {"status", "capability_count", "digest"}
    assert first["intent"]["sha256"] == routing.sha256_bytes(intent.read_bytes())
    assert first["provider_builtin_prompt"]["version"] == "opaque:2026.07.09-test"
    assert first["execution"]["mode"] == "read-only"

    instruction.write_text("private instruction beta\n")
    changed = routing.build_instruction_bom(**kwargs)
    assert changed["digest"] != first["digest"]

    mutated_provider = copy.deepcopy(provider)
    mutated_provider["model_family_rules"][0]["family"] = "cursor"
    policy_changed = routing.build_instruction_bom(
        **dict(kwargs, provider=mutated_provider)
    )
    assert policy_changed["provider_adapter_sha256"] != first["provider_adapter_sha256"]
    assert policy_changed["digest"] != first["digest"]

    execute_mode = routing.build_instruction_bom(**dict(kwargs, mode="execute"))
    assert execute_mode["execution"]["mode"] == "execute"
    assert execute_mode["digest"] != first["digest"]
