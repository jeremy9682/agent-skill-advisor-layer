from __future__ import annotations

import importlib.util
from pathlib import Path
import json


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
        "no-mistakes",
        "lfg",
        "ship",
        "overnight-execution",
    ]:
        assert audit.call_policy(name, "", {}) == "suggest-confirm"


def test_regular_skill_is_auto_eligible():
    audit = load_audit_module()

    assert audit.call_policy("format-json", "Format JSON files safely.", {}) == "auto-eligible"


def test_usage_estimate_ignores_injected_skill_lists(tmp_path):
    audit = load_audit_module()
    old_home = audit.HOME
    audit.HOME = tmp_path
    try:
        entries = [
            {"name": "huashu-design", "dir_name": "huashu-design"},
            {"name": "review", "dir_name": "gstack-review"},
            {"name": "qa", "dir_name": "qa"},
        ]
        codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "07"
        codex_dir.mkdir(parents=True)
        (codex_dir / "rollout.jsonl").write_text(
            "\n".join([
                json.dumps({
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "/Users/zihan/.codex/skills/huashu-design/SKILL.md"}],
                    },
                }),
                json.dumps({
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "arguments": json.dumps({"cmd": "sed -n '1,20p' /Users/zihan/.codex/skills/huashu-design/SKILL.md && gstack-review"}),
                    },
                }),
            ]) + "\n"
        )
        claude_dir = tmp_path / ".claude" / "projects" / "-tmp"
        claude_dir.mkdir(parents=True)
        (claude_dir / "session.jsonl").write_text(
            "\n".join([
                json.dumps({
                    "message": {
                        "content": [{
                            "type": "tool_use",
                            "name": "Skill",
                            "input": {"skill": "qa"},
                        }]
                    }
                }),
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [{
                            "type": "text",
                            "text": "我会使用 `review` skill 做审查。",
                        }]
                    },
                }),
            ]) + "\n"
        )
        timeline_dir = tmp_path / ".gstack" / "projects" / "repo"
        timeline_dir.mkdir(parents=True)
        (timeline_dir / "timeline.jsonl").write_text(json.dumps({"skill": "review"}) + "\n")

        usage = audit.estimate_usage(entries, days=30, limit=20, max_bytes=100_000)

        assert usage["huashu-design"]["skill_file_read"] == 1
        assert usage["huashu-design"]["actual_skill_invocation"] == 0
        assert usage["gstack-review"]["actual_skill_invocation"] == 1
        assert usage["review"]["gstack_timeline"] == 1
        assert usage["review"]["assistant_announcement"] == 1
        assert usage["qa"]["actual_skill_invocation"] == 1
    finally:
        audit.HOME = old_home


def test_report_usage_summary_counts_aliases_once(monkeypatch):
    audit = load_audit_module()
    entries = [
        {"name": "review", "dir_name": "gstack-review", "frontmatter_ok": True, "skill_lines": 10, "update_policy": "manual"},
        {"name": "review", "dir_name": "gstack-review-copy", "frontmatter_ok": True, "skill_lines": 10, "update_policy": "manual"},
    ]
    usage = {alias: audit.empty_usage() for alias in ["review", "gstack-review", "gstack-review-copy"]}
    usage["review"]["gstack_timeline"] = 1
    usage["gstack-review"]["actual_skill_invocation"] = 1

    monkeypatch.setattr(audit, "load_previous_manifest", lambda _path: {})
    monkeypatch.setattr(audit, "previous_entries", lambda _manifest: {})
    monkeypatch.setattr(audit, "discover_skills", lambda: [dict(e) for e in entries])
    monkeypatch.setattr(audit, "estimate_usage", lambda *_args: usage)
    monkeypatch.setattr(audit, "ls_remote", lambda *_args: {"ok": True})
    monkeypatch.setattr(audit, "dependency_checks", lambda: {})
    monkeypatch.setattr(audit, "check_huashu_design", lambda _entries: {})
    monkeypatch.setattr(audit, "script_syntax_checks", lambda _entries: [])

    class Args:
        usage_days = 30
        usage_file_limit = 20
        usage_size_limit = 100_000
        sync_safe = False
        dry_run_sync = False
        syntax_check = False

    report = audit.build_report(Args())

    assert report["usage_summary"]["actual_skill_invocation"] == 1
    assert report["usage_summary"]["gstack_timeline"] == 1
    assert [e["usage_recent_total_evidence"] for e in report["entries"]] == [2, 1]


def _pin_entry(name, group, git_head="", is_symlink=False, runtime="codex"):
    return {
        "name": name,
        "runtime": runtime,
        "source_group": group,
        "path": f"/fake/{runtime}/{name}",
        "is_symlink": is_symlink,
        "git_head": git_head,
    }


def test_pin_check_classifies_immutable_identifiers():
    audit = load_audit_module()
    entries = [
        # first-party: exempt entirely
        _pin_entry("my-local-skill", "local-manual"),
        # external + local commit sha (a real 40-hex sha): pinned
        _pin_entry("superpowers-x", "superpowers", git_head="a" * 40),
        # external + registered pin SHA but no local commit: pinned
        _pin_entry("huashu-agent-swarm", "huashu-skills"),
        # external, no commit, not registered: UNPINNED violation
        _pin_entry("stray-copy", "gstack"),
    ]
    result = audit.pin_check(entries)
    assert result["external_count"] == 3  # local-manual excluded
    assert result["unpinned_count"] == 1
    assert {u["name"] for u in result["unpinned"]} == {"stray-copy"}
    # huashu-skills is pinned via a fixed SHA in REGISTERED_PINS, not a mutable branch
    assert "huashu-skills" in audit.REGISTERED_PINS
    assert len(audit.REGISTERED_PINS["huashu-skills"]) == 40  # a real commit sha
    assert result["by_group"]["huashu-skills"]["pinned"] == 1
    assert result["by_group"]["gstack"]["unpinned"] == 1


def test_pin_check_url_branch_is_not_a_pin():
    # A source group known only by URL + mutable branch (KNOWN_REMOTES) must NOT
    # count as pinned — only a fixed SHA (REGISTERED_PINS) or a local commit does.
    audit = load_audit_module()
    # a group in KNOWN_REMOTES but deliberately absent from REGISTERED_PINS
    entries = [_pin_entry("url-only", "gstack")]  # gstack: not in REGISTERED_PINS
    result = audit.pin_check(entries)
    assert result["unpinned_count"] == 1


def test_pin_check_tree_hash_is_not_sufficient_alone():
    # An external skill with neither commit nor registered pin is a violation
    # even though every skill carries a tree_hash (integrity != provenance).
    audit = load_audit_module()
    entries = [_pin_entry("frontend-design", "frontend-design")]
    result = audit.pin_check(entries)
    assert result["unpinned_count"] == 1
    assert result["unpinned"][0]["reason"].startswith("external skill with no local commit")


def test_pin_check_missing_source_group_is_unpinned_not_exempt():
    # Fail-closed: a malformed entry with no source_group must be treated as an
    # external, unpinned violation — never silently exempted as first-party.
    audit = load_audit_module()
    entry = {"name": "malformed", "runtime": "codex"}  # no source_group at all
    result = audit.pin_check([entry])
    assert result["external_count"] == 1
    assert result["unpinned_count"] == 1
    assert result["unpinned"][0]["source_group"] == "(missing)"


def test_pin_check_rejects_non_sha_identifiers():
    # A git_head or a REGISTERED_PINS value that is not a full 40-hex sha must
    # NOT count as pinned — a branch name / partial / garbage fakes immutability.
    audit = load_audit_module()
    assert audit._is_sha("a" * 40) is True
    assert audit._is_sha("branch-main") is False
    assert audit._is_sha("not-a-sha") is False
    assert audit._is_sha("abc123") is False          # too short
    assert audit._is_sha("A" * 40) is False          # uppercase not hex-normalized
    # a bogus git_head string must not pin the entry
    entries = [_pin_entry("fake", "gstack", git_head="not-a-sha")]
    result = audit.pin_check(entries)
    assert result["unpinned_count"] == 1
    # every real REGISTERED_PINS value must be a valid sha
    assert all(audit._is_sha(v) for v in audit.REGISTERED_PINS.values())
