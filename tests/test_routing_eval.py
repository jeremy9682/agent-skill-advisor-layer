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


def test_displayed_recall_tracks_firing_not_just_rank(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills = routing.collect_skills(audit, build_fixture(tmp_path))
    cases = [{"id": "ppt", "prompt": "帮我做一个汇报用的幻灯片", "expect": ["fixture-ppt"]}]

    # At a reachable threshold the expected skill both ranks AND displays.
    low = routing.run_eval(skills, cases, fire_threshold=0.1)
    assert low["recall_at_k"] == 1.0
    assert low["displayed_recall"] == 1.0

    # Raise the bar above any score: still ranked (recall ok) but never shown.
    high = routing.run_eval(skills, cases, fire_threshold=999.0)
    assert high["recall_at_k"] == 1.0
    assert high["displayed_recall"] == 0.0


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


def test_negative_precision_flags_visible_false_positive(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills_dir = tmp_path / "skills"
    make_skill(skills_dir, "chatty", "Use when discussing ordinary python concepts. python GIL")
    skills = routing.collect_skills(audit, skills_dir)

    report = routing.run_eval(
        skills,
        [{"id": "plain", "prompt": "python 的 GIL 到底是什么？", "expect": []}],
        fire_threshold=0.0,
    )
    assert report["negative_total"] == 1
    assert report["negative_precision"] == 0.0
    assert report["false_positive_candidates"][0]["case"] == "plain"


def test_agent_to_agent_prompt_guard_keeps_eval_silent(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills_dir = tmp_path / "skills"
    make_skill(skills_dir, "huashu-design", "Use when making design visuals. Claude Fable 审核 分析")
    skills = routing.collect_skills(audit, skills_dir)

    report = routing.run_eval(
        skills,
        [{"id": "fable", "prompt": "你是 Claude Fable 5，作为独立外部审核者。", "expect": []}],
        fire_threshold=0.0,
    )
    case = report["cases"][0]
    assert case["skip_reason"] == "agent_to_agent_prompt"
    assert case["top"] == []
    assert report["false_positive_candidates"] == []

    codex_report = routing.run_eval(
        skills,
        [{"id": "codex", "prompt": "你是 Codex，作为最终审查席，只读审核这个 diff。", "expect": []}],
        fire_threshold=0.0,
    )
    assert codex_report["cases"][0]["skip_reason"] == "agent_to_agent_prompt"
    assert codex_report["false_positive_candidates"] == []

    nospace_report = routing.run_eval(
        skills,
        [{"id": "nospace", "prompt": "你是Claude Fable 5，作为独立外部审核者。", "expect": []}],
        fire_threshold=0.0,
    )
    assert nospace_report["cases"][0]["skip_reason"] == "agent_to_agent_prompt"


def test_seat_brief_skips_but_genuine_seat_word_does_not():
    """Role-assignment briefs skip; genuine prompts that merely mention a seat
    word must NOT (this repo's own domain is agent seats — bare-substring seat
    markers over-suppressed them, the regression caught in the 2026-07-08 review).
    """
    routing = load_routing_module()

    # "你是 … 席" role briefs: skip even when words sit between 你是 and the model
    # name (defeats the plain "你是 Claude" substring).
    briefs = [
        "你是本项目的 Claude 动态工作流调度/判断席。请只读文件，不要修改文件。评估当前分支。",
        "你是 Fable5 反方终审席。请只读，不要修改文件。对下面的方案做对抗性复核。",
    ]
    for prompt in briefs:
        assert routing.should_skip_prompt(prompt) == "agent_to_agent_prompt", prompt

    # Genuine user requests mentioning a seat word must fall through to scoring.
    genuine = [
        "帮我设计一个判断席评分面板的落地页",
        "解释一下判断席、落地席、终审席这三席是什么意思",
        "重构一下终审席打分的这段代码",
        "做一个展示判断席轮换规则的可视化页面",
        "你是不是能帮我看看判断席面板怎么设计",  # colloquial 你是不是, not a role brief
    ]
    for prompt in genuine:
        assert routing.should_skip_prompt(prompt) == "", prompt


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
    assert report["negative_total"] == 0
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
