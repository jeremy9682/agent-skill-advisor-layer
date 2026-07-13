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
        "research",
        "code-review",
        "improve-codebase-architecture",
        "wayfinder",
    ]:
        assert audit.call_policy(name, "", {}) == "suggest-confirm"

    # Local high-cost policy must override an upstream explicit-only hint.
    assert audit.call_policy(
        "wayfinder", "", {"disable-model-invocation": True}
    ) == "suggest-confirm"


def test_mattpocock_published_bundle_is_fully_registered_and_pinned():
    audit = load_audit_module()
    expected = {
        "ask-matt", "code-review", "codebase-design", "diagnosing-bugs",
        "domain-modeling", "grill-with-docs", "grill-me", "grilling",
        "handoff", "implement", "improve-codebase-architecture", "prototype",
        "research", "setup-matt-pocock-skills", "tdd", "teach", "to-spec",
        "to-tickets", "triage", "wayfinder", "writing-great-skills",
    }

    assert audit.MATTPOCOCK_SKILLS == expected
    assert audit.REGISTERED_PINS["mattpocock-skills"] == (
        "391a2701dd948f94f56a39f7533f8eea9a859c87"
    )
    for name in expected:
        path = audit.SKILL_ROOTS["codex"] / name
        assert audit.source_group(name, path, {}) == "mattpocock-skills"


def test_regular_skill_is_auto_eligible():
    audit = load_audit_module()

    assert audit.call_policy("format-json", "Format JSON files safely.", {}) == "auto-eligible"


def test_grilling_local_entry_override_preserves_upstream_sync_policy():
    audit = load_audit_module()

    assert audit.call_policy(
        "grilling", "Grill the user about a plan.", {}
    ) == "explicit-only"
    assert audit.update_policy(
        "grilling", "mattpocock-skills", {}, Path("/fake/grilling")
    ) == "merge-only"


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


def test_tree_hash_covers_symlinked_files(tmp_path):
    # BUG FIX: a skill dir whose files are symlinks pointing OUTSIDE the dir
    # (a link-farm) used to hash as the empty-input sha256 — no drift detection.
    audit = load_audit_module()
    target = tmp_path / "checkout" / "SKILL.md"
    target.parent.mkdir()
    target.write_text("v1")
    farm = tmp_path / "farm"
    farm.mkdir()
    (farm / "SKILL.md").symlink_to(target)

    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    h1 = audit.tree_hash(farm)
    assert h1 != empty and h1
    target.write_text("v2")            # drift at the symlink TARGET
    assert audit.tree_hash(farm) != h1  # must be detected


def test_discover_skills_resolves_linkfarm_provenance(tmp_path, monkeypatch):
    # BUG FIX: a wrapper dir outside any checkout, whose SKILL.md symlinks into
    # a git checkout, must inherit that checkout's git identity (git_head).
    import subprocess
    audit = load_audit_module()
    repo = tmp_path / "repo"
    (repo / "skills" / "myskill").mkdir(parents=True)
    (repo / "skills" / "myskill" / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: test skill for provenance\n---\nbody\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    root = tmp_path / "skillroot"
    wrapper = root / "myskill"
    wrapper.mkdir(parents=True)
    (wrapper / "SKILL.md").symlink_to(repo / "skills" / "myskill" / "SKILL.md")

    monkeypatch.setattr(audit, "SKILL_ROOTS", {"testrt": root})
    entries = audit.discover_skills()
    assert len(entries) == 1
    assert audit._is_sha(entries[0].get("git_head") or "")  # inherited from the checkout


def test_pin_check_frozen_legacy_match_and_drift():
    audit = load_audit_module()
    path = "/fake/frozen/skill"
    audit_frozen_backup = dict(audit.FROZEN_LEGACY)
    try:
        audit.FROZEN_LEGACY.clear()
        audit.FROZEN_LEGACY[path] = "f" * 64
        base = {"name": "frozen-skill", "runtime": "agents", "source_group": "frontend-design",
                "path": path, "is_symlink": False, "git_head": ""}
        # hash matches the frozen value → pinned by exception
        ok = audit.pin_check([dict(base, tree_hash="f" * 64)])
        assert ok["unpinned_count"] == 0
        # hash drifted → violation with the drift reason
        bad = audit.pin_check([dict(base, tree_hash="0" * 64)])
        assert bad["unpinned_count"] == 1
        assert "DRIFTED" in bad["unpinned"][0]["reason"]
        # EXCLUSIVE PRIORITY (sol review): a frozen entry that ALSO carries a
        # valid git_head OR a registered pin must STILL fail on drift — the
        # frozen hash is checked first and exclusively, no other pin masks it.
        masked = audit.pin_check([dict(base, tree_hash="0" * 64, git_head="a" * 40)])
        assert masked["unpinned_count"] == 1  # git_head does not rescue drifted frozen bytes
        audit.REGISTERED_PINS_backup = dict(audit.REGISTERED_PINS)
        try:
            audit.REGISTERED_PINS["frontend-design"] = "b" * 40
            masked2 = audit.pin_check([dict(base, tree_hash="0" * 64)])
            assert masked2["unpinned_count"] == 1  # registered pin does not rescue it either
        finally:
            audit.REGISTERED_PINS.clear()
            audit.REGISTERED_PINS.update(audit.REGISTERED_PINS_backup)
    finally:
        audit.FROZEN_LEGACY.clear()
        audit.FROZEN_LEGACY.update(audit_frozen_backup)


def test_tree_hash_follows_directory_symlinks(tmp_path):
    # SOL REVIEW: tree_hash must descend INTO a subdir that is itself a symlink
    # pointing outside the skill dir (a link-farm's `bin → ~/gstack/bin`), or
    # drift inside that subtree is invisible. Also asserts cycle safety.
    audit = load_audit_module()
    external = tmp_path / "external"
    (external / "sub").mkdir(parents=True)
    (external / "sub" / "tool.sh").write_text("v1")
    farm = tmp_path / "farm"
    farm.mkdir()
    (farm / "SKILL.md").write_text("skill")
    (farm / "bin").symlink_to(external / "sub")   # a DIRECTORY symlink out of farm

    h1 = audit.tree_hash(farm)
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert h1 and h1 != empty
    (external / "sub" / "tool.sh").write_text("v2")   # drift INSIDE the symlinked dir
    assert audit.tree_hash(farm) != h1                # must be detected

    # cycle: a symlink pointing back at its own ancestor must not hang
    (farm / "loop").symlink_to(farm)
    assert audit.tree_hash(farm)  # returns (does not infinite-loop)
