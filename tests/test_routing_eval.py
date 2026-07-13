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
    assert "修复" in tokens
    # 2026-07-11 契约变更：单字 CJK token 不再发出（零区分度碎片在
    # 英文为主的技能池里获得虚高 IDF，实测把纯执行指令路由到 huashu-design
    # 13.43 分）。真实中文信号由 bigram 承载。
    assert "修" not in tokens
    # 1-2 位纯数字同理（"codex 5.6" 的 5/6 曾命中 "5 维度评审"）；
    # 3 位以上保留（报错码 500 是真信号）。
    assert "5" not in routing.tokenize("codex 5.6 审核")
    assert "500" in routing.tokenize("接口报 500 了")


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


def test_system_injection_prefixes_skip_routing():
    """Harness/system text injected as a user turn must bypass routing — it was
    the biggest measured noise source (task-notifications scored dense workflow
    language into `huashu-design` etc.). The `<task-notification>` tag sits past
    AGENT_TO_AGENT_PATTERN_WINDOW behind the "[SYSTEM NOTIFICATION…]" preamble,
    so anchoring on the opening prefix is what actually catches it.
    """
    routing = load_routing_module()
    injections = [
        "[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task event, NOT a message from the user.\nNo human input has been received since the last genuine user message.\n<task-notification>\n<task-id>x</task-id>",
        "<system-reminder>\nAs you answer, you can use the following context:",
        "<local-command-caveat>Caveat: generated while running local commands.</local-command-caveat>\n<command-name>/model</command-name>",
        "[skill-router] 本条任务可能匹配以下已安装 skill：\n- huashu-design\n- social-monitor",
        "<task-notification>\n<task-id>y</task-id>\n<status>completed</status>",
        "=== OPENCLAW PREAMBLE ===\nTIMESTAMP=2026-07-13T10:00:00Z\nGIT_REPO=false",
    ]
    for prompt in injections:
        assert routing.should_skip_prompt(prompt) == "system_injection", prompt[:40]

    # Must NOT swallow a genuine prompt that merely mentions these words.
    genuine = [
        "把这个系统通知的格式改成 JSON",
        "解释一下 command-name 这个字段是干嘛的",
        "帮我给 skill-router 写段文档",
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


def test_model_route_policy_keeps_small_fix_small():
    routing = load_routing_module()

    policy = routing.model_route_policy({
        "task_shape": "small_fix",
        "risk_zone": "low",
        "repo_profile": "restricted-zone-heavy",
        "mechanical": True,
    })

    assert policy["direction_seat"] == "codex"
    assert policy["landing_seat"] == "codex"
    assert policy["final_review_seat"] == "none"
    assert policy["effort"] == "medium-fast"
    assert policy["gates"] == ["focused_verification"]
    assert policy["hot_path"] is False


def test_model_route_policy_escalates_restricted_feature():
    routing = load_routing_module()

    policy = routing.model_route_policy({
        "task_shape": "feature",
        "risk_zone": "restricted",
        "repo_profile": "restricted-zone-heavy",
    })

    assert policy["direction_seat"] == "claude"
    assert policy["landing_seat"] == "implementation_owner"
    assert policy["final_review_seat"] == "codex"
    assert policy["effort"] == "xhigh"
    assert policy["gates"] == [
        "intent",
        "plan_gate",
        "blind_plan_review",
        "final_diff_review",
    ]
    assert policy["hot_path"] is False


def test_model_routing_eval_flags_policy_mismatch():
    routing = load_routing_module()

    report = routing.run_model_routing_eval([
        {
            "id": "bad-small-fix",
            "task_shape": "small_fix",
            "risk_zone": "low",
            "repo_profile": "default",
            "expect_policy": {"effort": "xhigh"},
        }
    ])

    assert report["total"] == 1
    assert report["hits"] == 0
    assert report["failures"][0]["id"] == "bad-small-fix"
    assert report["failures"][0]["mismatches"]["effort"]["actual"] == "medium-fast"


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
    model_cases = data.get("model_routing_cases", [])
    assert len(cases) >= 15
    assert all(c.get("id") and c.get("prompt") for c in cases)
    assert len(model_cases) >= 5
    assert all(c.get("id") and c.get("expect_policy") for c in model_cases)


def _load_hook_module():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "scripts" / "skill_router_hook.py"
    spec = importlib.util.spec_from_file_location("skill_router_hook", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_hot_route_exclude_default_and_filter(monkeypatch, tmp_path):
    # #2 (2026-07-13): content-creation attractors are excluded from the hot
    # auto-suggest surface; the exclude only removes, never adds.
    hook = _load_hook_module()
    monkeypatch.setattr(hook, "GOV_DIR", tmp_path)  # no override file → default set
    excl = hook.load_hot_route_exclude()
    assert "huashu-design" in excl and "social-monitor" in excl
    # grilling: top-level explicit-only (fleet-independent membership assertion so
    # this cannot pass vacuously on a bare CI runner with no live skill fleet)
    assert "grilling" in excl
    # a genuine engineering skill is NOT excluded
    assert "investigate" not in excl and "dev-workflow" not in excl
    # filtering semantics: excluded names drop, others survive, order preserved
    chosen = [("huashu-design", 9.0), ("investigate", 5.0), ("social-monitor", 4.5)]
    kept = [(n, s) for n, s in chosen if n not in excl]
    assert kept == [("investigate", 5.0)]


def test_hot_route_exclude_config_override(monkeypatch, tmp_path):
    hook = _load_hook_module()
    monkeypatch.setattr(hook, "GOV_DIR", tmp_path)
    (tmp_path / "hot-route-exclude.json").write_text('["only-this"]')
    excl = hook.load_hot_route_exclude()
    assert excl == {"only-this"}
    # an empty list restores pre-shrink behavior (nothing excluded)
    (tmp_path / "hot-route-exclude.json").write_text('[]')
    assert hook.load_hot_route_exclude() == set()
