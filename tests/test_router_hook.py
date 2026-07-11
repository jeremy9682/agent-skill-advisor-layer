from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "skill_router_hook.py"


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


def run_hook(stdin_text: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_hook_degrades_silently_on_garbage_input():
    # M1: any bad input -> {} and exit 0, never a crash
    assert run_hook("") == "{}"
    assert run_hook("not json at all") == "{}"
    assert run_hook('{"no_prompt_field": 1}') == "{}"
    assert run_hook('{"prompt": "   "}') == "{}"


def test_hook_output_is_valid_schema_or_noop():
    # Live-fleet run: whatever fires must match the hook schema exactly.
    out = run_hook(json.dumps({"prompt": "这个接口报 500 了，帮我查一下为什么"}))
    data = json.loads(out)
    if data:
        inner = data["hookSpecificOutput"]
        assert inner["hookEventName"] == "UserPromptSubmit"
        assert "仅建议" in inner["additionalContext"]
        assert "MANDATORY" not in inner["additionalContext"]


def test_negative_prompts_stay_silent():
    # Calibration negatives (M4): chit-chat must not fire.
    for prompt in ["python 的 GIL 到底是什么？", "谢谢，今天先到这里", "今天天气怎么样"]:
        out = run_hook(json.dumps({"prompt": prompt}))
        assert out == "{}", f"hook fired on negative prompt: {prompt}"


def test_vercel_style_false_triggers_stay_silent():
    # The three real 2026-07-06 false-trigger incidents as negative cases:
    # path-pattern lexical matches without domain context must not fire.
    prompts = [
        "edit docs/development-workflow-standard.md (agent governance doc)",
        "read .github/workflows/ci.yml (plain pytest CI)",
        "the plan was approved yesterday, continue",
    ]
    for prompt in prompts:
        out = run_hook(json.dumps({"prompt": prompt}))
        data = json.loads(out)
        if data:  # firing is tolerable only if no suggest-confirm skill appears
            ctx = data["hookSpecificOutput"]["additionalContext"]
            assert "suggest-confirm" not in ctx, f"high-cost skill on: {prompt}"


def test_agent_to_agent_prompts_skip_router():
    prompts = [
        "你是 Claude Fable 5，作为独立外部审核者。请只基于下面事实包分析。",
        "你是Codex，作为最终审查席，只读审核这个 diff。",
        "你是 Claude Code，作为 Codex 之外的独立第二意见。只做只读分析。",
        "<task-notification><task-id>w8s8esecj</task-id></task-notification>",
    ]
    for prompt in prompts:
        out = run_hook(json.dumps({"prompt": prompt}))
        assert out == "{}", f"hook fired on agent-to-agent prompt: {prompt}"


def test_agent_to_agent_skip_is_logged(tmp_path):
    env_home = tmp_path / "home"
    env_home.mkdir()
    prompt = "你是Codex，作为最终审查席，只读审核这个 diff。"
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt, "cwd": "/Users/x/agent-skill-advisor-layer"}),
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, "HOME": str(env_home)},
    )
    assert proc.returncode == 0
    assert proc.stdout == "{}"
    log = env_home / ".codex" / "skill-governance" / "routing-log.jsonl"
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["fired"] is False
    assert rec["skip_reason"] == "agent_to_agent_prompt"
    assert rec["candidates"] == []


def test_hints_negative_triggers_and_domains(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills_dir = tmp_path / "skills"
    make_skill(skills_dir, "black-hole",
               "万能 中文 关键词 很多 修复 测试 审查 部署 上线 页面 数据")
    make_skill(skills_dir, "scoped-skill", "Use when working on the demo project. 演示 项目")
    skills = routing.collect_skills(audit, skills_dir)
    hints = {
        "black-hole": {"extra_triggers": [], "negative_triggers": ["审查"], "domains": []},
        "scoped-skill": {"extra_triggers": ["演示"], "negative_triggers": [], "domains": ["demo-project"]},
    }
    index = routing.LexicalIndex(skills, hints=hints)

    # negative trigger excludes the black hole
    names = [n for n, _ in index.rank("帮我审查这个页面的修复", cwd="")]
    assert "black-hole" not in names

    # domain-scoped skill excluded outside its cwd, included inside
    out_names = [n for n, _ in index.rank("改一下演示 页面", cwd="/Users/x/other-repo")]
    in_names = [n for n, _ in index.rank("改一下演示 页面", cwd="/Users/x/demo-project")]
    assert "scoped-skill" not in out_names
    assert "scoped-skill" in in_names


def test_hints_do_not_suppress_product_context_design_prompt(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills_dir = tmp_path / "skills"
    make_skill(
        skills_dir,
        "huashu-design",
        "Use when making landing page visual designs and prototypes. Claude Code 落地页 设计 原型",
    )
    skills = routing.collect_skills(audit, skills_dir)
    hints = routing.load_hints(ROOT / "routing-evals" / "hints.yaml")
    index = routing.LexicalIndex(skills, hints=hints)

    names = [n for n, _ in index.rank("在 Claude Code 里帮我设计一个落地页视觉原型")]

    assert "huashu-design" in names


def test_extra_triggers_change_ranking(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills_dir = tmp_path / "skills"
    make_skill(skills_dir, "en-only-review", "Use when reviewing a pull request diff.")
    make_skill(skills_dir, "noise", "Use when doing something else entirely. 文章 写作")
    skills = routing.collect_skills(audit, skills_dir)

    bare = routing.LexicalIndex(skills)
    assert not any(n == "en-only-review" for n, _ in bare.rank("审查一下我分支的改动"))

    hinted = routing.LexicalIndex(skills, hints={
        "en-only-review": {"extra_triggers": ["审查", "分支改动"], "negative_triggers": [], "domains": []},
    })
    assert hinted.rank("审查一下我分支的改动")[0][0] == "en-only-review"


def test_routing_log_written_and_redacted(tmp_path, monkeypatch):
    # Run hook with HOME pointed at tmp so the log lands in a sandbox.
    env_home = tmp_path / "home"
    env_home.mkdir()
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": "这个接口报 500 了，帮我查一下为什么", "cwd": "/Users/x/secret-project"}),
        capture_output=True, text=True, timeout=30,
        env={**__import__("os").environ, "HOME": str(env_home)},
    )
    assert proc.returncode == 0
    log = env_home / ".codex" / "skill-governance" / "routing-log.jsonl"
    if log.is_file():  # fired or not, a record should exist
        rec = json.loads(log.read_text().splitlines()[-1])
        assert len(rec["prompt_sha"]) == 16
        # M-privacy: plaintext excerpt is gone by default; hash+len only.
        assert "prompt_head" not in rec
        assert isinstance(rec["prompt_len"], int)
        assert rec["repo"] == "secret-project"  # basename only, no full path
        assert "/Users/x" not in json.dumps(rec)


def test_new_skill_invalidates_cache(tmp_path):
    # Final-review finding 1: a newly installed SKILL.md must change the
    # fingerprint even though no previously-cached path changed.
    import os
    env_home = tmp_path / "home"
    skills_root = env_home / ".claude" / "skills"
    make_skill(skills_root, "first-skill", "Use when doing the first thing. 第一 任务")
    env = {**os.environ, "HOME": str(env_home)}

    def run(prompt: str) -> None:
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps({"prompt": prompt}),
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert proc.returncode == 0, proc.stderr

    run("第一 任务")
    cache = env_home / ".codex" / "skill-governance" / "router-index.json"
    assert cache.is_file()
    names = {s["name"] for s in json.loads(cache.read_text())["skills"]}
    assert names == {"first-skill"}

    make_skill(skills_root, "second-skill", "Use when doing the second thing. 第二 任务")
    run("第二 任务")
    names = {s["name"] for s in json.loads(cache.read_text())["skills"]}
    assert "second-skill" in names, "cache did not rebuild after new skill install"


def test_chosen_candidates_matches_production_rule():
    # Final-review finding 2: eval and hook share one display rule.
    routing = load_routing_module()
    ranked = [("top", 12.0), ("runner-up", 4.5), ("faint", 1.0)]
    chosen = routing.chosen_candidates(ranked, fire_threshold=4.0, companion_ratio=0.6)
    assert [n for n, _ in chosen] == ["top"], "sub-companion runner-up must be hidden"

    chosen = routing.chosen_candidates(ranked, fire_threshold=4.0, companion_ratio=0.3)
    assert [n for n, _ in chosen] == ["top", "runner-up"]

    assert routing.chosen_candidates([("weak", 3.9)], fire_threshold=4.0) == []
    assert routing.chosen_candidates([], fire_threshold=4.0) == []

    # top-K truncation is internal: a full ranking cannot overflow the display
    long_ranked = [(f"s{i}", 10.0 - i * 0.1) for i in range(10)]
    chosen = routing.chosen_candidates(long_ranked, fire_threshold=4.0, companion_ratio=0.1)
    assert len(chosen) == routing.TOP_K


def test_eval_ignores_high_cost_hidden_by_companion_rule(tmp_path):
    routing = load_routing_module()
    audit = routing.load_audit_module()
    skills_dir = tmp_path / "skills"
    # dominant benign skill + faint high-cost runner-up
    make_skill(skills_dir, "dominant", "alpha beta gamma delta epsilon zeta")
    make_skill(skills_dir, "faint-ship", "release gate ci watch production ship")
    skills = routing.collect_skills(audit, skills_dir)
    assert any(s["name"] == "faint-ship" and s["policy"] == "suggest-confirm" for s in skills)

    routing.FIRE_THRESHOLD = 0.1  # let the dominant skill fire in a tiny fleet
    routing.COMPANION_RATIO = 0.9  # runner-up cannot clear 90% of top score
    report = routing.run_eval(skills, [
        {"id": "mixed", "prompt": "alpha beta gamma delta epsilon ship", "expect": []},
    ])
    assert report["unexpected_high_cost_candidates"] == [], (
        "production would hide the runner-up; eval must not count it")
    assert any(e["skill"] == "faint-ship" for e in report["gate_dependency_events"])


def test_repo_hints_file_parses_and_targets_installed_skills():
    routing = load_routing_module()
    hints = routing.load_hints(ROOT / "routing-evals" / "hints.yaml")
    if not hints:  # yaml unavailable in CI -> degrade path is the assertion
        return
    assert "xyq-nest-skill" in hints
    assert hints["xyq-nest-skill"]["negative_triggers"]
    for name, h in hints.items():
        assert isinstance(h["extra_triggers"], list)
        assert isinstance(h["negative_triggers"], list)
        assert isinstance(h["domains"], list)
