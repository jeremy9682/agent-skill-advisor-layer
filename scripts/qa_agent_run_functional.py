#!/usr/bin/env python3
"""Functional QA for agent-run stability hardening.

Exercises real code paths with fake filesystem/subprocess fixtures.
No live Fable/Codex API calls. Safe to run locally; cleans up /tmp/agent-run-qa-*.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from multiprocessing import Process
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_provider_run", ROOT / "scripts" / "agent_provider_run.py"
)
agent_run = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agent_run)

QA_ROOT = Path(tempfile.mkdtemp(prefix="agent-run-qa-"))
MANIFEST = ROOT / "agent-providers.yaml"
SERIAL_SH = ROOT / "scripts" / "agent_run_serial.sh"
MONITOR_SH = ROOT / "scripts" / "monitor-agent-runs.sh"

PASS = 0
FAIL = 0
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    RESULTS.append((name, ok, detail))
    if ok:
        PASS += 1
        print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""), file=sys.stderr)


def assert_eq(name: str, got, expected, detail: str = "") -> None:
    record(name, got == expected, detail or f"got={got!r} expected={expected!r}")


def assert_true(name: str, cond: bool, detail: str = "") -> None:
    record(name, bool(cond), detail)


# --- 1. Serial lock — real flock behavior -----------------------------------


def _lock_holder(journal_root: str, hold_seconds: float) -> None:
    with agent_run.ProviderSerialLock(
        "qa-test-group", journal_root=Path(journal_root), wait_seconds=10
    ):
        time.sleep(hold_seconds)


def _lock_contender(journal_root: str, wait_seconds: int, out_path: str) -> None:
    result = {"acquired": False, "error": None}
    try:
        with agent_run.ProviderSerialLock(
            "qa-test-group", journal_root=Path(journal_root), wait_seconds=wait_seconds
        ):
            result["acquired"] = True
    except agent_run.SerialLockTimeout as exc:
        result["error"] = str(exc)
    Path(out_path).write_text(json.dumps(result))


def test_serial_lock_concurrent_flock() -> None:
    journal = QA_ROOT / "journal-serial"
    journal.mkdir()
    out = QA_ROOT / "contender-result.json"
    holder = Process(target=_lock_holder, args=(str(journal), 2.5))
    holder.start()
    time.sleep(0.3)
    contender = Process(
        target=_lock_contender, args=(str(journal), 1, str(out))
    )
    contender.start()
    holder.join(timeout=10)
    contender.join(timeout=10)
    data = json.loads(out.read_text())
    assert_true(
        "serial_lock: second acquirer times out while first holds",
        not data["acquired"] and data["error"] is not None,
        str(data),
    )
    lock_path = agent_run.serial_lock_path("qa-test-group", journal)
    assert_true("serial_lock: lock file created under journal root", lock_path.is_file())
    with agent_run.ProviderSerialLock(
        "qa-test-group", journal_root=journal, wait_seconds=0
    ) as lock:
        assert_eq(
            "serial_lock: re-acquire after release",
            lock.telemetry["status"],
            "acquired",
        )


def test_agent_run_serial_no_shell_flock() -> None:
    text = SERIAL_SH.read_text(encoding="utf-8")
    code_lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    flock_in_code = any("flock" in line for line in code_lines)
    assert_true(
        "serial_sh: passthrough exec without shell flock",
        not flock_in_code and "exec agent-run run" in text,
        "found flock in executable lines" if flock_in_code else "passthrough ok",
    )


# --- 2. Session attribution — fake session artifacts ------------------------


def _fake_claude_root(base: Path) -> Path:
    root = base / "claude-projects"
    root.mkdir(parents=True)
    return root


def _fake_codex_rollout(base: Path, session_uuid: str, model: str) -> Path:
    root = base / "codex-sessions"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rollout-2026-07-14T00-00-00-{session_uuid}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {"model": model, "turn_id": "t1"},
            }
        )
        + "\n"
    )
    return path


def _fake_claude_transcript(base: Path, session_id: str, model: str) -> Path:
    root = _fake_claude_root(base)
    path = root / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"model": model, "content": []},
            }
        )
        + "\n"
    )
    return path


def test_session_attribution_single_artifact() -> None:
    base = QA_ROOT / "sessions-single"
    path = _fake_codex_rollout(
        base, "00000000-0000-4000-8000-000000000001", "gpt-5.6-terra"
    )
    before: dict[str, tuple[int, int]] = {}
    after = {str(path): agent_run.file_fingerprint(path)}
    session, changed = agent_run.attribute_session("codex", before, after)
    assert_eq("session_attr: single codex artifact count", changed, 1)
    assert_eq(
        "session_attr: single codex session_id",
        session["session_id"],
        "00000000-0000-4000-8000-000000000001",
    )
    assert_eq(
        "session_attr: single codex status",
        session["session_status"],
        "attributed-single-artifact",
    )


def test_session_attribution_ambiguous() -> None:
    base = QA_ROOT / "sessions-ambiguous"
    base.mkdir(parents=True, exist_ok=True)
    one = base / "one.jsonl"
    two = base / "two.jsonl"
    one.write_text("{}\n")
    two.write_text("{}\n")
    after = {
        str(one): agent_run.file_fingerprint(one),
        str(two): agent_run.file_fingerprint(two),
    }
    session, changed = agent_run.attribute_session("claude", {}, after)
    assert_eq("session_attr: ambiguous changed count", changed, 2)
    assert_eq(
        "session_attr: ambiguous status",
        session["session_status"],
        "ambiguous-concurrent-artifacts",
    )


def test_session_attribution_stream_json_path() -> None:
    base = QA_ROOT / "sessions-stream"
    session_id = "sess-stream-qa"
    transcript = _fake_claude_transcript(base, session_id, "claude-opus-qa")
    artifacts = {str(transcript): agent_run.file_fingerprint(transcript)}
    record = agent_run.stream_session_record("claude", session_id, artifacts)
    assert_eq("session_attr: stream-json session_id", record["session_id"], session_id)
    assert_eq(
        "session_attr: stream-json model",
        record["model_observed"],
        "claude-opus-qa",
    )
    assert_eq(
        "session_attr: stream-json status",
        record["session_status"],
        "attributed-stream-json",
    )


def test_session_snapshot_before_after() -> None:
    base = QA_ROOT / "sessions-snapshot"
    provider = {
        "session": {"adapter": "codex", "roots": [str(base / "codex-sessions")]}
    }
    snap_before = agent_run.session_snapshot(provider)
    path = _fake_codex_rollout(
        base, "00000000-0000-4000-8000-000000000002", "gpt-5.6-sol"
    )
    snap_after = agent_run.session_snapshot(provider)
    changed_path, status, count = agent_run.changed_session(snap_before, snap_after)
    assert_true("session_snapshot: detects new artifact", count >= 1)
    assert_eq("session_snapshot: single new artifact status", status, "attributed-single-artifact")
    assert_true("session_snapshot: changed path matches", changed_path == path)


# --- 3. Stream-json parsing — fake event files ------------------------------


def test_stream_json_extractors() -> None:
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-claude-qa"},
        {
            "type": "assistant",
            "message": {
                "model": "claude-opus-qa",
                "content": [{"type": "text", "text": "draft"}],
            },
        },
        {"type": "result", "result": "APPROVE"},
    ]
    assert_eq(
        "stream_json: claude session_id",
        agent_run.extract_claude_session_from_events(events),
        "sess-claude-qa",
    )
    assert_eq(
        "stream_json: claude model",
        agent_run.extract_claude_model_from_events(events),
        "claude-opus-qa",
    )
    assert_eq(
        "stream_json: claude agent message",
        agent_run.extract_claude_agent_message(events),
        "APPROVE",
    )
    codex_events = [
        {"type": "thread.started", "thread_id": "thread-qa"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "OK"},
        },
    ]
    assert_eq(
        "stream_json: codex session_id",
        agent_run.extract_codex_session_from_events(codex_events),
        "thread-qa",
    )
    assert_eq(
        "stream_json: codex agent message",
        agent_run.extract_codex_agent_message(codex_events),
        "OK",
    )


# --- 4. classify_failure — fake stdout/stderr samples -----------------------


def test_classify_failure_samples() -> None:
    cases = [
        (
            "classify_failure: auth-expired",
            agent_run.classify_failure(
                "completed", 1, "", stdout="401 Unauthorized invalid token"
            ),
            "auth-expired",
        ),
        (
            "classify_failure: rate-limited",
            agent_run.classify_failure(
                "completed", 1, "", stdout="429 Too Many Requests retry later"
            ),
            "rate-limited",
        ),
        (
            "classify_failure: serial-lock-timeout passthrough",
            agent_run.classify_failure("serial-lock-timeout", 75, ""),
            "serial-lock-timeout",
        ),
        (
            "classify_failure: timeout from run_status",
            agent_run.classify_failure("timed-out", 124, ""),
            "timeout",
        ),
        (
            "classify_failure: upstream-overload",
            agent_run.classify_failure(
                "completed", 1, "", stdout="HTTP 529 overloaded upstream"
            ),
            "upstream-overload",
        ),
        (
            "classify_failure: quota-exhausted",
            agent_run.classify_failure(
                "completed", 1, "402 Payment Required: spending-limit"
            ),
            "quota-exhausted",
        ),
    ]
    for name, got, expected in cases:
        assert_eq(name, got, expected)


# --- 5. effective_timeout + serial_group via CLI ----------------------------


def test_effective_timeout_and_serial_group_canon() -> None:
    config = agent_run.load_manifest(MANIFEST)
    args_mechanical = argparse.Namespace(timeout_seconds=None)
    args_review = argparse.Namespace(timeout_seconds=None)
    assert_eq(
        "canon: mechanical default timeout (300 fallback)",
        agent_run.effective_timeout_seconds(args_mechanical, "mechanical", config),
        agent_run.DEFAULT_RUN_TIMEOUT_SECONDS,
    )
    assert_eq(
        "canon: fable_final_review timeout 900",
        agent_run.effective_timeout_seconds(args_review, "fable_final_review", config),
        900,
    )
    assert_eq(
        "canon: mechanical serial_group None",
        agent_run.serial_group_for_provider(
            "cursor", agent_run.route_binding(config, "mechanical")
        ),
        None,
    )
    assert_eq(
        "canon: fable_final_review serial_group claude-family",
        agent_run.serial_group_for_provider(
            "claude", agent_run.route_binding(config, "fable_final_review")
        ),
        "claude-family",
    )
    proc = subprocess.run(
        ["agent-run", "routes"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert_eq("cli: agent-run routes exit 0", proc.returncode, 0)
    routes = json.loads(proc.stdout)["routes"]
    assert_true(
        "cli: routes lists mechanical without serial lock",
        routes["mechanical"]["serial_lock_enabled"] is False,
    )
    assert_eq(
        "cli: routes fable_final_review serial_group",
        routes["fable_final_review"]["serial_group"],
        "claude-family",
    )
    assert_eq(
        "cli: routes fable_final_review timeout_seconds",
        routes["fable_final_review"]["timeout_seconds"],
        900,
    )


# --- 6. kill_process_tree — controlled subprocess ---------------------------


def test_kill_process_tree_reaps_children() -> None:
    proc = subprocess.Popen(
        ["bash", "-c", "sleep 120 & sleep 120; wait"],
        cwd=QA_ROOT,
        start_new_session=True,
    )
    time.sleep(0.2)
    assert_true("kill_tree: process running before kill", proc.poll() is None)
    pgid = os.getpgid(proc.pid)
    agent_run.kill_process_tree(proc)
    assert_true("kill_tree: process reaped", proc.poll() is not None)
    try:
        os.killpg(pgid, 0)
        record("kill_tree: process group gone", False, f"pgid {pgid} still exists")
    except ProcessLookupError:
        record("kill_tree: process group gone", True)


# --- 7. Monitor script — fake journal ---------------------------------------


def test_monitor_fake_journal() -> None:
    fake_home = QA_ROOT / "fake-home"
    journal_dir = fake_home / ".agent-runs"
    journal_dir.mkdir(parents=True)
    rows = [
        {
            "ended_at": "2026-07-17T10:00:00Z",
            "provider_id": "claude",
            "seat": "fable-final-review",
            "exit_code": 0,
            "failure_class": "none",
            "session_status": "attributed-single-artifact",
        },
        {
            "ended_at": "2026-07-17T10:01:00Z",
            "provider_id": "codex",
            "seat": "codex-landing",
            "exit_code": 1,
            "failure_class": "auth-expired",
            "session_status": "",
        },
        {
            "ended_at": "2026-07-17T10:02:00Z",
            "provider_id": "cursor",
            "seat": "claude-landing",
            "exit_code": 0,
            "failure_class": "none",
            "session_status": "ambiguous-concurrent-artifacts",
        },
        {
            "ended_at": "2026-07-17T10:03:00Z",
            "provider_id": "codex",
            "seat": "codex-final-review",
            "exit_code": 75,
            "failure_class": "serial-lock-timeout",
            "session_status": "",
        },
        {
            "ended_at": "2026-07-17T10:04:00Z",
            "provider_id": "grok",
            "seat": "codex-final-review",
            "exit_code": 1,
            "failure_class": "rate-limited",
            "session_status": "",
        },
    ]
    (journal_dir / "qa-repo.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    proc = subprocess.run(
        ["bash", str(MONITOR_SH), "5"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert_eq("monitor: exit 0", proc.returncode, 0, proc.stderr[:200])
    out = proc.stdout
    assert_true("monitor: reports auth-expired count", "auth-expired" in out)
    assert_true("monitor: reports session_ambiguous", "session_ambiguous" in out)
    assert_true("monitor: reports serial-lock-timeout", "serial-lock-timeout" in out)
    assert_true("monitor: recent non-success section", "Recent non-success:" in out)
    # Rows sorted by ended_at; last 5 should all appear in summary
    assert_true("monitor: includes rate-limited row", "rate-limited" in out)


# --- 8. End-to-end CLI dry paths --------------------------------------------


def test_cli_doctor_and_fail_closed() -> None:
    proc = subprocess.run(
        ["agent-run", "doctor"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert_eq("cli: agent-run doctor exit 0", proc.returncode, 0, proc.stderr[:300])
    assert_true("cli: doctor returns route_doctor JSON", "route_doctor" in proc.stdout)
    bad = subprocess.run(
        ["agent-run", "run", "codex", "hello"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert_eq("cli: missing seat fails closed exit 2", bad.returncode, 2)
    assert_true(
        "cli: missing seat clear error",
        "--seat is required" in bad.stderr,
        bad.stderr[:200],
    )


def cleanup_qa_dirs() -> None:
    for path in Path("/tmp").glob("agent-run-qa-*"):
        shutil.rmtree(path, ignore_errors=True)


def main() -> int:
    print(f"Functional QA root: {QA_ROOT}")
    print("=" * 60)
    tests = [
        test_serial_lock_concurrent_flock,
        test_agent_run_serial_no_shell_flock,
        test_session_attribution_single_artifact,
        test_session_attribution_ambiguous,
        test_session_attribution_stream_json_path,
        test_session_snapshot_before_after,
        test_stream_json_extractors,
        test_classify_failure_samples,
        test_effective_timeout_and_serial_group_canon,
        test_kill_process_tree_reaps_children,
        test_monitor_fake_journal,
        test_cli_doctor_and_fail_closed,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:
            record(test.__name__, False, f"uncaught: {exc}")
    print("=" * 60)
    print(f"Functional QA: {PASS} passed, {FAIL} failed (total {PASS + FAIL})")
    cleanup_qa_dirs()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
