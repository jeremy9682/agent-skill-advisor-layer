from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_routing_module():
    path = ROOT / "scripts" / "routing_eval.py"
    spec = importlib.util.spec_from_file_location("routing_eval_governance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_fixture(path: Path, skills: list[str], *, native: bool = False) -> None:
    rows = ["# High-Cost Skill governance", "", "| Skill | Trigger |", "|---|---|"]
    rows.extend(f"| `{skill}`（候选） | fixture |" for skill in skills)
    if native:
        rows.append("| `agent-teams`（原生功能，非 skill） | fixture |")
    path.write_text("\n".join(rows) + "\n")


def test_consistent_lists_pass(tmp_path):
    routing = load_routing_module()
    paths = {name: tmp_path / f"{name}.md" for name in ("claude", "advisor", "codex")}
    for path in paths.values():
        write_fixture(path, ["ship", "overnight-execution"], native=True)

    result = routing.check_governance_consistency(paths)

    assert result["status"] == "passed"
    assert result["sets"]["claude"] == ["overnight-execution", "ship"]


def test_inconsistent_lists_fail_with_differences(tmp_path):
    routing = load_routing_module()
    paths = {name: tmp_path / f"{name}.md" for name in ("claude", "advisor", "codex")}
    write_fixture(paths["claude"], ["ship", "retro"])
    write_fixture(paths["advisor"], ["ship"])
    write_fixture(paths["codex"], ["ship"])

    result = routing.check_governance_consistency(paths)

    assert result["status"] == "failed"
    assert result["differences"]["claude - advisor"] == ["retro"]
    assert result["differences"]["claude - codex"] == ["retro"]


def test_missing_file_skips_check(tmp_path):
    routing = load_routing_module()
    paths = {name: tmp_path / f"{name}.md" for name in ("claude", "advisor", "codex")}
    write_fixture(paths["claude"], ["ship"])
    write_fixture(paths["advisor"], ["ship"])

    result = routing.check_governance_consistency(paths)

    assert result["status"] == "skipped"
    assert result["missing_files"] == {"codex": str(paths["codex"])}
    assert result["differences"] == {}
