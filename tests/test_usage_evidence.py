import argparse
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("skill_audit_usage", ROOT / "scripts" / "skill_audit.py")
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit)


def test_usage_kinds_keep_invocation_reads_and_self_reads_separate(tmp_path):
    session = tmp_path / "session.jsonl"
    items = [
        {"type": "response_item", "payload": {"type": "function_call", "arguments": json.dumps({
            "cmd": "sed -n '1,20p' /Users/zihan/.codex/skills/demo/SKILL.md"
        })}},
        {"type": "response_item", "payload": {"type": "function_call", "arguments": json.dumps({
            "cmd": "python3 scripts/skill_audit.py --report; sed -n '1,20p' /Users/zihan/.codex/skills/demo/SKILL.md"
        })}},
        {"type": "response_item", "payload": {"type": "function_call", "arguments": json.dumps({
            "cmd": "true; gstack-demo"
        })}},
    ]
    session.write_text("\n".join(json.dumps(item) for item in items))
    counts = {"demo": audit.empty_usage(), "gstack-demo": audit.empty_usage()}

    audit.scan_codex_session(session, counts, set(counts), 100_000)

    assert counts["demo"]["skill_file_read"] == 1
    assert counts["demo"]["self_audit_read"] == 1
    assert counts["gstack-demo"]["actual_skill_invocation"] == 1


def test_doctor_and_selftune_batch_reads_are_excluded():
    valid = {"alpha", "beta"}
    counts = {name: audit.empty_usage() for name in valid}
    command = (
        "python3 scripts/router_selftune.py && python3 scripts/routing_eval.py --doctor "
        "/Users/zihan/.codex/skills/alpha/SKILL.md "
        "/Users/zihan/.codex/skills/beta/SKILL.md"
    )

    audit.record_skill_paths(command, counts, valid)

    assert sum(row["skill_file_read"] for row in counts.values()) == 0
    assert sum(row["self_audit_read"] for row in counts.values()) == 2


def test_report_exposes_excluded_count_without_counting_it_as_evidence(monkeypatch):
    entry = {
        "dir_name": "demo", "name": "demo", "frontmatter_ok": True,
        "skill_lines": 10, "update_policy": "manual",
    }
    monkeypatch.setattr(audit, "load_previous_manifest", lambda _path: {})
    monkeypatch.setattr(audit, "previous_entries", lambda _manifest: {})
    monkeypatch.setattr(audit, "discover_skills", lambda: [entry])
    monkeypatch.setattr(audit, "estimate_usage", lambda *_args: {
        "demo": {
            "actual_skill_invocation": 1,
            "skill_file_read": 2,
            "self_audit_read": 5,
            "gstack_timeline": 0,
            "assistant_announcement": 0,
        }
    })
    monkeypatch.setattr(audit, "ls_remote", lambda *_args: "")
    monkeypatch.setattr(audit, "dependency_checks", lambda: {})
    monkeypatch.setattr(audit, "check_huashu_design", lambda _entries: {})
    args = argparse.Namespace(
        usage_days=30, usage_file_limit=10, usage_size_limit=1000,
        sync_safe=False, dry_run_sync=False, syntax_check=False,
    )

    report = audit.build_report(args)

    assert report["usage_summary"]["actual_skill_invocation"] == 1
    assert report["usage_summary"]["skill_file_read"] == 2
    assert report["usage_summary"]["self_read_excluded"] == 5
    assert report["entries"][0]["usage_recent_total_evidence"] == 3
