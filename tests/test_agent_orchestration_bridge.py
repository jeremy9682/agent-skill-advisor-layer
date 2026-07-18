from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "orchestration_bridge", ROOT / "scripts" / "orchestration" / "bridge.py"
)
bridge_mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge_mod)


def base_task(tmp_path: Path) -> dict:
    return {
        "task_id": "task-1",
        "task_shape": "ordinary_bug_fix",
        "checkpoint_event": "evt-20260718T000000.000000Z-codex-landing",
        "cwd": str(tmp_path),
        "mode": "read-only",
        "deadline_seconds": 300,
        "compiled_seat": "codex-landing",
        "prompt": "Inspect the frozen fixture.",
        "context_fragments": ["one", "one", "two"],
    }


def receipt(**overrides) -> dict:
    value = {
        "run_id": "provider-run-1",
        "provider": "codex",
        "seat": "codex-landing",
        "exit_code": 0,
        "failure_class": "none",
        "duration_ms": 12,
        "session_id": "session-1",
        "session_status": "attributed-stream-json",
        "model": "gpt-5.6-terra",
    }
    value.update(overrides)
    return value


def executable_wrapper(tmp_path: Path, body: str) -> Path:
    wrapper = tmp_path / f"wrapper-{len(list(tmp_path.glob('wrapper-*')))}.py"
    wrapper.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    wrapper.chmod(0o700)
    return wrapper


def test_build_command_uses_only_governed_task_shape_and_no_native_override(tmp_path):
    adapter = bridge_mod.NativeAgentRunBridge(artifact_root=tmp_path / "artifacts")
    task = base_task(tmp_path)
    prompt, _ = adapter.prepare_prompt(task)
    command = adapter.build_command(task, prompt)
    assert Path(command[1]).resolve() == (ROOT / "scripts" / "agent_provider_run.py").resolve()
    assert command[2:4] == ["run", "auto"]
    assert "--task-shape" in command
    assert not (set(command) & bridge_mod.FORBIDDEN_ARG_TOKENS)
    for key in bridge_mod.FORBIDDEN_TASK_OVERRIDES:
        bad = dict(task, **{key: "override"})
        with pytest.raises(bridge_mod.BridgeError, match="may not override"):
            adapter.build_command(bad, prompt)


def test_parse_requires_one_complete_attributed_receipt():
    line = json.dumps({"agent_run": receipt()})
    parsed, answer = bridge_mod.parse_agent_run_output(line + "\nANSWER\n")
    assert parsed["run_id"] == "provider-run-1"
    assert answer == "ANSWER"
    with pytest.raises(bridge_mod.BridgeError, match="found 0"):
        bridge_mod.parse_agent_run_output("no receipt")
    with pytest.raises(bridge_mod.BridgeError, match="found 2"):
        bridge_mod.parse_agent_run_output(line + "\n" + line)
    bad = json.dumps({"agent_run": receipt(session_status="ambiguous")})
    with pytest.raises(bridge_mod.BridgeError, match="unacceptable attribution"):
        bridge_mod.parse_agent_run_output(bad)


def preflight_rejection_receipt(**overrides) -> dict:
    value = {
        "receipt_kind": "preflight-rejection-v1",
        "run_id": "preflight-run-1",
        "provider": "cursor",
        "seat": "cursor-producer",
        "model": "composer-2.5-fast",
        "exit_code": 2,
        "failure_class": "provider-catalog-unavailable",
        "session_status": "not-started",
        "preflight_stage": "model-catalog",
        "catalog_status": "catalog-unavailable",
        "catalog_attempts": 2,
    }
    value.update(overrides)
    return value


def test_bridge_accepts_only_strict_identity_complete_sessionless_catalog_rejection(
    tmp_path,
):
    line = json.dumps({"agent_run": preflight_rejection_receipt()})
    parsed, answer = bridge_mod.parse_agent_run_output(line)
    assert parsed["session_status"] == "not-started"
    assert answer == ""

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr=line)

    result = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", runner=runner
    ).run_task(
        base_task(tmp_path), run_id="catalog-preflight", attempt_id="attempt-1", generation=1
    )
    assert result["status"] == "failed"
    assert result["failure_class"] == "provider-preflight-transient"
    assert result["session_status"] == "not-started"
    assert "session_id" not in result
    assert base_task(tmp_path)["prompt"] not in json.dumps(result)

    malformed = dict(preflight_rejection_receipt())
    malformed["session_id"] = "must-not-exist"
    with pytest.raises(bridge_mod.BridgeError, match="strict preflight rejection"):
        bridge_mod.parse_agent_run_output(json.dumps({"agent_run": malformed}))


def test_bridge_accepts_only_strict_sessionless_router_timeout_rejection(tmp_path):
    router_receipt = {
        "receipt_kind": "preflight-rejection-v1",
        "run_id": "router-preflight-run-1",
        "provider": "codex",
        "seat": "codex-landing",
        "model": "gpt-5.6-terra",
        "exit_code": 2,
        "failure_class": "provider-skill-router-timeout",
        "session_status": "not-started",
        "preflight_stage": "skill-router",
        "router_status": "router-timeout",
        "router_attempts": 2,
    }
    line = json.dumps({"agent_run": router_receipt})

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr=line)

    result = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", runner=runner
    ).run_task(
        base_task(tmp_path), run_id="router-preflight", attempt_id="attempt-1", generation=1
    )
    assert result["status"] == "failed"
    assert result["failure_class"] == "provider-preflight-transient"
    assert result["session_status"] == "not-started"
    assert "session_id" not in result

    malformed = dict(router_receipt)
    malformed["router_status"] = "router-malformed-output"
    with pytest.raises(bridge_mod.BridgeError, match="strict preflight rejection"):
        bridge_mod.parse_agent_run_output(json.dumps({"agent_run": malformed}))


def test_unknown_exit_two_without_machine_rejection_stays_failed_unsafe(tmp_path):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="catalog failed")

    result = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", runner=runner
    ).run_task(
        base_task(tmp_path), run_id="unknown-exit-two", attempt_id="attempt-1", generation=1
    )
    assert result["status"] == "failed-unsafe"
    assert result["failure_class"] == "unattributed-wrapper-exit"


def test_live_unknown_exit_two_without_machine_rejection_stays_failed_unsafe(tmp_path):
    wrapper = executable_wrapper(tmp_path, "raise SystemExit(2)")
    bridge = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", binary=str(wrapper)
    )
    launch = bridge.launch_task(
        base_task(tmp_path), run_id="unknown-live-exit-two", attempt_id="attempt-1", generation=1
    )
    result = bridge.collect_task(launch)
    assert result["status"] == "failed-unsafe"
    assert result["failure_class"] == "unattributed-wrapper-exit"


@pytest.mark.parametrize(
    ("provider_failure", "orchestration_failure"),
    [
        ("rate-limited", "provider-rate-limit"),
        ("upstream-overload", "provider-transient"),
        ("provider-error", "provider-transient"),
        ("provider-start-failed", "provider-transient"),
    ],
)
def test_live_receipt_transients_map_to_compiled_retry_taxonomy(
    tmp_path, provider_failure, orchestration_failure
):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps(
                {
                    "agent_run": receipt(
                        exit_code=1,
                        failure_class=provider_failure,
                    )
                }
            ),
            stderr="",
        )

    result = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", runner=runner
    ).run_task(
        base_task(tmp_path),
        run_id="orch-transient",
        attempt_id=f"attempt-{provider_failure}",
        generation=1,
    )

    assert result["status"] == "failed"
    assert result["failure_class"] == orchestration_failure


def test_run_task_persists_private_answer_and_returns_only_pointer_and_metrics(tmp_path):
    def runner(command, **kwargs):
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"agent_run": receipt()}) + "\nprivate answer\n",
            stderr="",
        )

    adapter = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", runner=runner
    )
    result = adapter.run_task(
        base_task(tmp_path), run_id="orch-1", attempt_id="attempt-1", generation=1
    )
    artifact = Path(result["artifact_path"])
    assert artifact.read_text() == "private answer"
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert result["status"] == "succeeded"
    assert result["provider_run_id"] == "provider-run-1"
    assert result["delivered_prompt_bytes"] > 0
    assert result["deduplicated_shared_context_bytes"] == len(b"one\n\ntwo")
    assert "prompt" not in result and "answer" not in result


@pytest.mark.parametrize(
    ("answer", "failure_class"),
    [
        ("Findings\nAGENT_RUN_REVIEW_VERDICT: FAIL", "review-verdict-fail"),
        ("Findings without a machine verdict", "review-verdict-missing"),
        (
            "AGENT_RUN_REVIEW_VERDICT: PASS\nAGENT_RUN_REVIEW_VERDICT: PASS",
            "review-verdict-ambiguous",
        ),
        ("AGENT_RUN_REVIEW_VERDICT: PASS\nTrailing prose", "review-verdict-missing"),
    ],
)
def test_reviewer_invocation_success_requires_one_final_pass_verdict(
    tmp_path, answer, failure_class
):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"agent_run": receipt()}) + "\n" + answer + "\n",
            stderr="",
        )

    task = base_task(tmp_path)
    task["reviewer_for"] = ["producer"]
    result = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", runner=runner
    ).run_task(task, run_id="orch-review", attempt_id="attempt-review", generation=1)

    assert result["status"] == "failed"
    assert result["failure_class"] == failure_class
    assert Path(result["artifact_path"]).read_text() == answer


def test_reviewer_final_pass_verdict_is_accepted(tmp_path):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(json.dumps({"agent_run": receipt()}) + "\nFindings\n"
                    "AGENT_RUN_REVIEW_VERDICT: PASS\n"),
            stderr="",
        )

    task = base_task(tmp_path)
    task["reviewer_for"] = ["producer"]
    result = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", runner=runner
    ).run_task(task, run_id="orch-review-pass", attempt_id="attempt-review-pass", generation=1)

    assert result["status"] == "succeeded"
    assert result["failure_class"] == "none"


def test_execute_mode_requires_no_bridge_bypass_and_adds_only_wrapper_allow_write(tmp_path):
    task = base_task(tmp_path)
    task["mode"] = "execute"
    adapter = bridge_mod.NativeAgentRunBridge(artifact_root=tmp_path / "artifacts")
    prompt, _ = adapter.prepare_prompt(task)
    command = adapter.build_command(task, prompt)
    assert "--allow-write" in command
    assert not (set(command) & bridge_mod.FORBIDDEN_ARG_TOKENS)


def test_review_bundle_flags_are_runtime_bound_and_hash_checked(tmp_path):
    bundle = tmp_path / "bundle.json"
    bundle.write_text("{}\n", encoding="utf-8")
    bundle.chmod(0o600)
    digest = __import__("hashlib").sha256(bundle.read_bytes()).hexdigest()
    task = {
        **base_task(tmp_path),
        "producer_review_bundle": str(bundle),
        "producer_review_bundle_sha256": digest,
        "orchestration_run_id": "orch",
        "orchestration_generation": 1,
        "orchestration_fencing_token": "fence",
        "orchestration_reviewer_task_id": "task-1",
        "orchestration_reviewer_attempt_id": "attempt-1",
    }
    adapter = bridge_mod.NativeAgentRunBridge(artifact_root=tmp_path / "artifacts")
    command = adapter.build_command(task, task["prompt"])
    assert command[command.index("--producer-review-bundle") + 1] == str(bundle)
    assert command[command.index("--orchestration-fencing-token") + 1] == "fence"
    bundle.write_text('{"drift":true}\n', encoding="utf-8")
    with pytest.raises(bridge_mod.BridgeError, match="digest drift"):
        adapter.build_command(task, task["prompt"])


def test_receipt_exit_mismatch_fails_closed(tmp_path):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps({"agent_run": receipt(exit_code=0)}) + "\nanswer",
            stderr="",
        )

    adapter = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", runner=runner
    )
    with pytest.raises(bridge_mod.BridgeError, match="does not match"):
        adapter.run_task(
            base_task(tmp_path), run_id="orch-1", attempt_id="attempt-1", generation=1
        )


def test_two_phase_launch_journals_real_identity_and_private_attempt_evidence(tmp_path):
    line = json.dumps({"agent_run": receipt()})
    wrapper = executable_wrapper(
        tmp_path,
        f"import sys\nprint({line!r})\nprint('private answer')\nsys.stdout.flush()",
    )
    bridge = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", binary=str(wrapper)
    )
    launch = bridge.launch_task(
        base_task(tmp_path), run_id="orch-live", attempt_id="attempt-live",
        generation=1,
    )
    evidence = launch.journal_evidence()
    assert evidence["wrapper_pid"] == launch.process.pid
    assert evidence["wrapper_start_fingerprint"]
    manifest = json.loads(launch.manifest_path.read_text())
    assert manifest["status"] == "running"
    assert manifest["wrapper_pid"] == launch.process.pid
    assert "command" not in manifest and "prompt" not in manifest
    for path in (launch.manifest_path, launch.stdout_path, launch.stderr_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    result = bridge.collect_task(launch)
    assert result["status"] == "succeeded"
    assert result["wrapper_pid"] == evidence["wrapper_pid"]
    assert result["process_cleanup"] == {"status": "succeeded", "residual": False}
    assert Path(result["artifact_path"]).read_text() == "private answer"


def test_hard_deadline_terminates_process_group_and_verifies_no_residual(tmp_path):
    wrapper = executable_wrapper(
        tmp_path,
        "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)",
    )
    task = base_task(tmp_path)
    task["deadline_seconds"] = 1
    bridge = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", binary=str(wrapper),
        terminate_grace_seconds=0.05, poll_seconds=0.01,
    )
    launch = bridge.launch_task(
        task, run_id="orch-timeout", attempt_id="attempt-timeout", generation=1
    )
    result = bridge.collect_task(launch)
    assert result["status"] == "timed-out"
    assert result["failure_class"] == "deadline-exceeded"
    assert result["process_cleanup"]["status"] == "succeeded"
    assert result["process_cleanup"]["residual"] is False
    assert result["process_cleanup"]["kill_sent"] is True
    assert not bridge_mod._group_alive(launch.process_group_id)


def test_process_group_permission_error_is_contained_as_failed_cleanup(
    tmp_path, monkeypatch
):
    bridge = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts",
        terminate_grace_seconds=0,
        poll_seconds=0,
    )
    launch = type("Launch", (), {"process_group_id": 999999})()
    monkeypatch.setattr(bridge_mod, "_group_alive", lambda _pgid: True)
    monkeypatch.setattr(
        bridge_mod.os,
        "killpg",
        lambda _pgid, _signal: (_ for _ in ()).throw(PermissionError()),
    )

    cleanup = bridge._terminate_group(launch)

    assert cleanup["status"] == "failed"
    assert cleanup["residual"] is True
    assert cleanup["term_permission_denied"] is True
    assert cleanup["kill_permission_denied"] is True


def test_unexpected_descendant_is_cleaned_but_attempt_stays_failed_unsafe(tmp_path):
    line = json.dumps({"agent_run": receipt()})
    wrapper = executable_wrapper(
        tmp_path,
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
        f"print({line!r})\nprint('answer')\nsys.stdout.flush()",
    )
    bridge = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", binary=str(wrapper),
        terminate_grace_seconds=0.05, poll_seconds=0.01,
    )
    launch = bridge.launch_task(
        base_task(tmp_path), run_id="orch-residual", attempt_id="attempt-residual",
        generation=1,
    )
    result = bridge.collect_task(launch)
    assert result["status"] == "failed-unsafe"
    assert result["failure_class"] == "residual-provider-process"
    assert result["process_cleanup"]["unexpected_residual"] is True
    assert result["process_cleanup"]["residual"] is False


def test_short_lived_descendant_naturally_exits_before_residual_cleanup(
    tmp_path, monkeypatch
):
    """A child that exits just after its wrapper must not be killed or blamed."""

    line = json.dumps({"agent_run": receipt()})
    wrapper = executable_wrapper(
        tmp_path,
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(0.03)'])\n"
        f"print({line!r})\nprint('answer')\nsys.stdout.flush()",
    )
    bridge = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", binary=str(wrapper),
        natural_exit_settle_seconds=0.15, terminate_grace_seconds=0.01,
        poll_seconds=0.005,
    )
    monkeypatch.setattr(
        bridge,
        "_terminate_group",
        lambda _launch: (_ for _ in ()).throw(AssertionError("must not send TERM")),
    )

    launch = bridge.launch_task(
        base_task(tmp_path), run_id="orch-short-child", attempt_id="attempt-short-child",
        generation=1,
    )
    result = bridge.collect_task(launch)

    assert result["status"] == "succeeded"
    assert result["process_cleanup"] == {"status": "succeeded", "residual": False}


def test_persistent_descendant_waits_briefly_then_keeps_failed_unsafe_cleanup(tmp_path):
    """The settle window is bounded; a real descendant remains a safety failure."""

    line = json.dumps({"agent_run": receipt()})
    wrapper = executable_wrapper(
        tmp_path,
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
        f"print({line!r})\nprint('answer')\nsys.stdout.flush()",
    )
    bridge = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", binary=str(wrapper),
        natural_exit_settle_seconds=0.12, terminate_grace_seconds=0.01,
        poll_seconds=0.005,
    )
    launch = bridge.launch_task(
        base_task(tmp_path), run_id="orch-persistent-child", attempt_id="attempt-persistent-child",
        generation=1,
    )
    started = time.monotonic()
    result = bridge.collect_task(launch)

    assert time.monotonic() - started >= 0.08
    assert result["status"] == "failed-unsafe"
    assert result["failure_class"] == "residual-provider-process"
    assert result["process_cleanup"]["unexpected_residual"] is True
    assert result["process_cleanup"]["term_sent"] is True


def test_reconcile_terminal_attempt_from_private_streams_never_relaunches(tmp_path):
    line = json.dumps({"agent_run": receipt()})
    wrapper = executable_wrapper(
        tmp_path,
        f"print({line!r})\nprint('recovered answer')",
    )
    bridge = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", binary=str(wrapper)
    )
    task = base_task(tmp_path)
    launch = bridge.launch_task(
        task, run_id="orch-restart", attempt_id="attempt-stable", generation=1
    )
    assert launch.process.wait(timeout=2) == 0
    recovered = bridge.reconcile_task(
        task, run_id="orch-restart", attempt_id="attempt-stable", generation=2,
        prior_state={"status": "running"},
    )
    assert recovered["status"] == "succeeded"
    assert recovered["replayed"] is False
    assert recovered["reconciled"] is True
    assert recovered["wrapper_pid"] == launch.pid
    assert Path(recovered["artifact_path"]).read_text() == "recovered answer"


def test_reconcile_reapplies_reviewer_verdict_contract_from_sealed_answer(tmp_path):
    line = json.dumps({"agent_run": receipt()})
    wrapper = executable_wrapper(
        tmp_path,
        f"print({line!r})\nprint('findings')\nprint('AGENT_RUN_REVIEW_VERDICT: FAIL')",
    )
    bridge = bridge_mod.NativeAgentRunBridge(
        artifact_root=tmp_path / "artifacts", binary=str(wrapper)
    )
    task = base_task(tmp_path)
    task["reviewer_for"] = ["producer"]
    launch = bridge.launch_task(
        task, run_id="orch-review-restart", attempt_id="attempt-review-stable",
        generation=1,
    )
    assert launch.process.wait(timeout=2) == 0

    recovered = bridge.reconcile_task(
        task, run_id="orch-review-restart", attempt_id="attempt-review-stable",
        generation=2, prior_state={"status": "running"},
    )

    assert recovered["status"] == "failed"
    assert recovered["failure_class"] == "review-verdict-fail"
    assert recovered["reconciled"] is True
    assert Path(recovered["artifact_path"]).read_text().endswith(
        "AGENT_RUN_REVIEW_VERDICT: FAIL"
    )


def test_reconcile_missing_manifest_and_reused_pid_fail_closed(tmp_path, monkeypatch):
    bridge = bridge_mod.NativeAgentRunBridge(artifact_root=tmp_path / "artifacts")
    task = base_task(tmp_path)
    missing = bridge.reconcile_task(
        task, run_id="orch-missing", attempt_id="attempt-missing", generation=2,
        prior_state={"status": "dispatch-intent"},
    )
    assert missing == {
        "status": "failed-unsafe", "failure_class": "attempt-manifest-missing",
        "replayed": False,
    }

    created_dir, created_manifest, created_stdout, created_stderr = bridge._attempt_paths(
        "orch-created", "task-1", "attempt-created"
    )
    created_dir.mkdir(parents=True)
    created_manifest.write_text(json.dumps({
        "version": 1, "run_id": "orch-created", "task_id": "task-1",
        "attempt_id": "attempt-created", "generation": 1, "status": "created",
    }))
    created_manifest.chmod(0o600)
    created = bridge.reconcile_task(
        task, run_id="orch-created", attempt_id="attempt-created", generation=2,
        prior_state={"status": "dispatch-intent"},
    )
    assert created["status"] == "failed-unsafe"
    assert created["failure_class"] == "launch-window-unverifiable-orphan-preserved"
    assert created["resource_preserved"] is True

    directory, manifest, stdout_path, stderr_path = bridge._attempt_paths(
        "orch-stale", "task-1", "attempt-stale"
    )
    directory.mkdir(parents=True)
    stdout_path.write_bytes(b"")
    stderr_path.write_bytes(b"")
    manifest.write_text(json.dumps({
        "version": 1, "run_id": "orch-stale", "task_id": "task-1",
        "attempt_id": "attempt-stale", "generation": 1, "status": "running",
        "deadline_at": "2099-01-01T00:00:00.000Z", "wrapper_pid": 424242,
        "wrapper_start_fingerprint": "original", "process_group_id": 424242,
    }))
    for path in (manifest, stdout_path, stderr_path):
        path.chmod(0o600)
    monkeypatch.setattr(bridge_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(bridge_mod, "_start_fingerprint", lambda _pid: "different")
    stale = bridge.reconcile_task(
        task, run_id="orch-stale", attempt_id="attempt-stale", generation=2,
        prior_state={"status": "running"},
    )
    assert stale["failure_class"] == "stale-or-reused-wrapper-pid"
    assert stale["status"] == "failed-unsafe"
