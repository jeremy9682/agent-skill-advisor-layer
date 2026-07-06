from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_routing_module():
    path = ROOT / "scripts" / "routing_eval.py"
    spec = importlib.util.spec_from_file_location("routing_eval", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_skill(root: Path, name: str, description: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{description}"\n---\n\n# {name}\n'
    )


def build_fixture(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    make_skill(
        skills,
        "fixture-ppt",
        "Use this skill when the user wants a slide deck, PPT, 幻灯片, or 汇报页面.",
    )
    make_skill(
        skills,
        "fixture-debug",
        "Use when the user reports a bug, 500 error, 报错, or unexpected behavior.",
    )
    make_skill(
        skills,
        "fixture-ship",
        "Ship the project to production with release gate and ci watch. "
        "Suggest only; requires explicit approval.",
    )
    return skills


def test_tokenize_handles_cjk_bigrams():
    routing = load_routing_module()
    tokens = routing.tokenize("修复 bug")
    assert "bug" in tokens
    assert "修" in tokens and "修复" in tokens


def test_eval_recall_and_gate_detection(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills = routing.collect_skills(audit, build_fixture(tmp_path))

    by_name = {s["name"]: s for s in skills}
    assert by_name["fixture-ship"]["policy"] == "suggest-confirm"
    assert all(len(s["sha256"]) == 16 for s in skills)

    cases = [
        {"id": "ppt", "prompt": "帮我做一个汇报用的幻灯片", "expect": ["fixture-ppt"]},
        {"id": "bug", "prompt": "接口报 500 了帮我查一下", "expect": ["fixture-debug"]},
        {
            "id": "ship",
            "prompt": "ship this project to production",
            "expect": ["fixture-ship"],
            "high_cost_ok": ["fixture-ship"],
        },
        {"id": "missing", "prompt": "anything", "expect": ["not-installed-skill"]},
    ]
    report = routing.run_eval(skills, cases)

    assert report["recall_total"] == 3  # missing-skill case is skipped, not failed
    assert report["recall_at_k"] == 1.0
    assert report["unexpected_high_cost_candidates"] == []
    assert any(e["skill"] == "fixture-ship" for e in report["gate_dependency_events"])

    by_id = {c["id"]: c for c in report["cases"]}
    assert by_id["missing"]["skipped_missing_skill"] == ["not-installed-skill"]


def test_unexpected_high_cost_candidate_is_flagged(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills = routing.collect_skills(audit, build_fixture(tmp_path))

    cases = [
        {
            "id": "sneaky",
            "prompt": "production release gate ci watch ship",
            "expect": [],
            # no high_cost_ok: fixture-ship surfacing must be flagged
        }
    ]
    # Mechanism test: force the firing bar to zero so the tiny fixture
    # fleet's low IDF ceiling cannot mask the flagging logic itself.
    routing.FIRE_THRESHOLD = 0.0
    report = routing.run_eval(skills, cases)
    assert any(
        e["skill"] == "fixture-ship"
        for e in report["unexpected_high_cost_candidates"]
    )


def test_sub_threshold_sighting_is_exposure_not_violation(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills = routing.collect_skills(audit, build_fixture(tmp_path))

    cases = [{"id": "faint", "prompt": "production release gate ci watch ship", "expect": []}]
    # With an unreachably high bar, the sighting stays visible in
    # gate_dependency_events but is not a violation (production never fires).
    routing.FIRE_THRESHOLD = 999.0
    report = routing.run_eval(skills, cases)
    assert report["unexpected_high_cost_candidates"] == []
    assert any(e["skill"] == "fixture-ship" for e in report["gate_dependency_events"])


def test_known_leak_is_reported_but_not_a_violation(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills = routing.collect_skills(audit, build_fixture(tmp_path))

    cases = [
        {
            "id": "documented",
            "prompt": "production release gate ci watch ship",
            "expect": [],
            "known_leaks": ["fixture-ship"],
        }
    ]
    report = routing.run_eval(skills, cases)
    assert report["unexpected_high_cost_candidates"] == []
    assert any(e["skill"] == "fixture-ship" for e in report["known_leaks"])
    assert any(e["skill"] == "fixture-ship" for e in report["gate_dependency_events"])


def test_supply_chain_evidence_present_in_skills_and_lint(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills_dir = tmp_path / "skills"
    make_skill(skills_dir, "no-trigger", "This skill does various things nicely.")
    skills = routing.collect_skills(audit, skills_dir)

    for skill in skills:
        assert skill["path"].endswith("SKILL.md")
        assert len(skill["sha256"]) == 16

    findings = routing.run_lint(skills)
    assert findings, "fixture should produce at least one finding"
    for finding in findings:
        assert finding["path"].endswith("SKILL.md")
        assert len(finding["sha256"]) == 16
        assert finding["root"]
        assert isinstance(finding["frontmatter_issues"], list)


def test_lint_flags_missing_trigger_and_confirm_language(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills_dir = tmp_path / "skills"
    make_skill(skills_dir, "no-trigger", "This skill does various things nicely.")
    make_skill(
        skills_dir,
        "silent-autonomous-pipeline",
        "Hands-off autonomous pipeline from plan to PR and ci watch.",
    )
    skills = routing.collect_skills(audit, skills_dir)
    findings = {f["name"]: f["issues"] for f in routing.run_lint(skills)}

    assert "L2_no_trigger_clause" in findings["no-trigger"]
    assert "L4_high_cost_without_confirm_language" in findings["silent-autonomous-pipeline"]


def test_repo_cases_file_parses():
    routing = load_routing_module()
    data = routing.parse_cases(ROOT / "routing-evals" / "cases.yaml")
    cases = data.get("cases", [])
    assert len(cases) >= 15
    assert all(c.get("id") and c.get("prompt") for c in cases)
