import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("skill_audit_scan_health", ROOT / "scripts" / "skill_audit.py")
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit)

ENTRIES = [{"name": "prototype", "dir_name": "prototype"}]


def test_estimate_usage_reports_scan_health_for_recent_files(tmp_path, monkeypatch):
    # A recent, well-formed session file is both found and scanned. This is the
    # signal _adoption needs to trust a zero: a real scan actually ran.
    monkeypatch.setattr(audit, "HOME", tmp_path)
    sessions = tmp_path / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.jsonl").write_text(json.dumps({"type": "other"}) + "\n")

    health = {}
    audit.estimate_usage(ENTRIES, 7, 400, 3_000_000, health=health)

    assert health["files_found"] == 1
    assert health["files_scanned"] == 1


def test_estimate_usage_scan_health_zero_when_no_sources(tmp_path, monkeypatch):
    # No transcript dirs at all → nothing found or scanned, so a resulting 0 is
    # a scan gap the caller must treat as unavailable, not an observed zero.
    monkeypatch.setattr(audit, "HOME", tmp_path)

    health = {}
    audit.estimate_usage(ENTRIES, 7, 400, 3_000_000, health=health)

    assert health["files_found"] == 0
    assert health["files_scanned"] == 0


def test_estimate_usage_scan_health_counts_only_successful_scans(tmp_path, monkeypatch):
    # The key case that the earlier liveness proxy could not see: sources present
    # but every scan throws. files are found, none scanned → total scan failure
    # is detectable (files_found > 0, files_scanned == 0) instead of a silent 0.
    monkeypatch.setattr(audit, "HOME", tmp_path)
    sessions = tmp_path / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.jsonl").write_text(json.dumps({"type": "other"}) + "\n")

    def boom(*_args, **_kwargs):
        raise RuntimeError("scan failed")
    monkeypatch.setattr(audit, "scan_codex_session", boom)

    health = {}
    audit.estimate_usage(ENTRIES, 7, 400, 3_000_000, health=health)

    assert health["files_found"] == 1
    assert health["files_scanned"] == 0
