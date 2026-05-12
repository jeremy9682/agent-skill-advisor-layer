from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    path = ROOT / "scripts" / "skill_audit.py"
    spec = importlib.util.spec_from_file_location("skill_audit", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_skill_advisor_frontmatter_routes_as_router():
    audit = load_audit_module()
    skill_md = ROOT / "skills" / "skill-advisor" / "SKILL.md"

    data, issues = audit.parse_frontmatter(skill_md)

    assert issues == []
    assert data["name"] == "skill-advisor"
    assert audit.call_policy(data["name"], data["description"], data) == "router"


def test_high_cost_skill_names_are_suggest_confirm():
    audit = load_audit_module()

    for name in [
        "huashu-agent-swarm",
        "gstack-pair-agent",
        "gstack-retro",
        "gstack-setup-gbrain",
    ]:
        assert audit.call_policy(name, "", {}) == "suggest-confirm"


def test_regular_skill_is_auto_eligible():
    audit = load_audit_module()

    assert audit.call_policy("format-json", "Format JSON files safely.", {}) == "auto-eligible"

