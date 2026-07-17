from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_provider_run", ROOT / "scripts" / "agent_provider_run.py"
)
agent_run = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agent_run)

LEDGER_SPEC = importlib.util.spec_from_file_location(
    "agent_ledger", ROOT / "scripts" / "agent_ledger.py"
)
agent_ledger = importlib.util.module_from_spec(LEDGER_SPEC)
assert LEDGER_SPEC.loader is not None
LEDGER_SPEC.loader.exec_module(agent_ledger)


def ledger_row(
    event_id: str,
    *,
    from_seat: str = "human",
    to_seat: str = "codex-landing",
    decided: list[str] | None = None,
    intent_ref: str = "docs/intents/demo.md",
    taint: bool | str = False,
) -> dict:
    return {
        "intent_ref": intent_ref,
        "event_id": event_id,
        "from_seat": from_seat,
        "to_seat": to_seat,
        "worktree": "/tmp/demo @ main @ " + "a" * 40,
        "file_scope": {"own": [], "do_not_touch": []},
        "decided_rejected_open": {
            "decided": decided or [],
            "rejected": [],
            "open": [],
        },
        "verification": "pytest -q",
        "next_action": "none" if decided else "implement the demo",
        "taint": taint,
    }


def verified_producer_model(provider_id: str, model: str, family: str) -> dict:
    evidence = {
        "model_requested": model,
        "model_observed": model,
        "model_family": family,
    }
    if provider_id in {"cursor", "grok"}:
        evidence.update(
            {
                "session_status": "attributed-correlated-artifacts",
                "provider_health_evidence": {
                    "status": "verified-native-session-model",
                    "model_observed": model,
                },
            }
        )
    return evidence


def test_manifest_has_safe_existing_login_providers():
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    assert set(data["providers"]) == {"claude", "codex", "grok", "cursor"}
    assert data["providers"]["grok"]["billing_policy"] == "existing-login-only"
    assert data["providers"]["grok"]["run_policy"] == "enabled"
    assert data["providers"]["cursor"]["model_requested"] == "auto"
    assert data["provider_aliases"]["cursor-auto"] == "cursor"
    assert "XAI_API_KEY" in data["providers"]["grok"]["strip_environment"]
    assert "CURSOR_API_KEY" in data["providers"]["cursor"]["strip_environment"]
    assert data["journal"]["live_evidence_max_age_seconds"] == 21600
    assert data["journal"]["live_evidence_future_skew_seconds"] == 300


def test_manifest_rejects_second_routes_source(tmp_path):
    data = yaml.safe_load((ROOT / "agent-providers.yaml").read_text())
    data["routes"] = {"forbidden": {"provider": "codex"}}
    path = tmp_path / "agent-providers.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(agent_run.ProviderRunError, match="sole canon"):
        agent_run.load_manifest(path)


def test_execute_requires_explicit_allow_write(monkeypatch, capsys):
    code = agent_run.main(
        [
            "--manifest",
            str(ROOT / "agent-providers.yaml"),
            "run",
            "grok",
            "hello",
            "--seat",
            "codex-landing",
            "--mode",
            "execute",
        ]
    )
    assert code == 2
    assert "requires explicit --allow-write" in capsys.readouterr().err


def test_build_command_keeps_prompt_as_one_argument():
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    provider = data["providers"]["cursor"]
    cmd = agent_run.build_command(
        provider,
        "read-only",
        Path("/bin/echo"),
        Path("/tmp/work"),
        "a; $(bad)",
        "composer-2.5",
    )
    assert cmd[-1] == "a; $(bad)"
    assert cmd[0] == "/bin/echo"
    assert "--mode" in cmd and "ask" in cmd
    assert cmd[cmd.index("--model") + 1] == "composer-2.5"


def test_cursor_alias_and_discovered_model_validation(monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    assert agent_run.canonical_provider_id(data, "cursor-auto") == "cursor"
    monkeypatch.setattr(
        agent_run,
        "discover_provider_models",
        lambda _provider, _binary: {
            "status": "catalog-listed",
            "models": [{"id": "auto"}, {"id": "composer-2.5"}],
        },
    )
    agent_run.validate_provider_model(
        "cursor", data["providers"]["cursor"], Path("/bin/echo"), "composer-2.5"
    )
    with pytest.raises(agent_run.ProviderRunError, match="not listed"):
        agent_run.validate_provider_model(
            "cursor", data["providers"]["cursor"], Path("/bin/echo"), "invented-model"
        )


def test_model_discovery_strips_provider_billing_environment(monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    provider = data["providers"]["cursor"]
    monkeypatch.setenv("CURSOR_API_KEY", "must-not-reach-child")

    def fake_run(_command, **kwargs):
        assert "CURSOR_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(
            [], 0, stdout="Available models\n\nauto - Auto\n", stderr=""
        )

    monkeypatch.setattr(agent_run.subprocess, "run", fake_run)
    catalog = agent_run.discover_provider_models(provider, Path("/bin/echo"))
    assert catalog == {
        "status": "catalog-listed",
        "models": [{"id": "auto", "label": "Auto"}],
    }


def test_model_discovery_rejects_unknown_command_placeholders(monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    provider = dict(data["providers"]["cursor"])
    provider["model_discovery"] = {
        "command": ["{binary}", "{unknown_placeholder}"],
        "parser": "cursor-models-v1",
    }
    monkeypatch.setattr(
        agent_run.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed discovery configuration must fail before subprocess execution"
        ),
    )
    assert agent_run.discover_provider_models(provider, Path("/bin/echo")) == {
        "status": "discovery-config-invalid",
        "models": [],
    }


@pytest.mark.parametrize(
    "discovery",
    [
        None,
        "not-a-mapping",
        {"command": None, "parser": "cursor-models-v1"},
        {"command": [None], "parser": "cursor-models-v1"},
    ],
)
def test_model_discovery_rejects_invalid_shapes_before_subprocess(
    monkeypatch, discovery
):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    provider = dict(data["providers"]["cursor"])
    provider["model_discovery"] = discovery
    monkeypatch.setattr(
        agent_run.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed discovery configuration must fail before subprocess execution"
        ),
    )
    assert agent_run.discover_provider_models(provider, Path("/bin/echo")) == {
        "status": "discovery-config-invalid",
        "models": [],
    }


def test_model_discovery_rejects_unsupported_parser_before_subprocess(monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    provider = dict(data["providers"]["cursor"])
    provider["model_discovery"] = {
        "command": ["{binary}", "models"],
        "parser": "future-parser-v2",
    }
    monkeypatch.setattr(
        agent_run.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "unsupported parsers must fail before subprocess execution"
        ),
    )
    assert agent_run.discover_provider_models(provider, Path("/bin/echo")) == {
        "status": "discovery-parser-unsupported",
        "models": [],
    }
    provider["model_discovery"]["command"] = []
    assert agent_run.discover_provider_models(provider, Path("/bin/echo")) == {
        "status": "discovery-config-invalid",
        "models": [],
    }


def test_version_probe_strips_provider_billing_environment(monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    provider = data["providers"]["cursor"]
    monkeypatch.setenv("CURSOR_API_KEY", "must-not-reach-version-probe")

    def fake_run(_command, **kwargs):
        assert "CURSOR_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess([], 0, stdout="cursor-test\n", stderr="")

    monkeypatch.setattr(agent_run.subprocess, "run", fake_run)
    assert agent_run.binary_version(Path("/bin/echo"), provider) == "cursor-test"


def test_discover_public_seam_reports_dynamic_cursor_catalog(monkeypatch, capsys):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    monkeypatch.setattr(
        agent_run, "resolve_binary", lambda _provider: Path("/bin/echo")
    )
    monkeypatch.setattr(
        agent_run, "binary_version", lambda _binary, _provider: "test-cli"
    )
    monkeypatch.setattr(agent_run, "session_snapshot", lambda _provider: {})

    def fake_catalog(provider, _binary):
        if provider.get("display_name") == "Cursor":
            return {
                "status": "catalog-listed",
                "models": [{"id": "composer-2.5"}, {"id": "cursor-grok-4.5-high"}],
            }
        return {
            "status": "static-config",
            "models": [{"id": model} for model in provider.get("model_options", [])],
        }

    monkeypatch.setattr(agent_run, "discover_provider_models", fake_catalog)
    assert agent_run.discover(data) == 0
    output = json.loads(capsys.readouterr().out)
    cursor = next(row for row in output["providers"] if row["provider_id"] == "cursor")
    assert cursor["model_catalog"] == {
        "status": "catalog-listed",
        "models": [{"id": "composer-2.5"}, {"id": "cursor-grok-4.5-high"}],
    }


def test_run_public_seam_journals_private_instruction_bom(
    tmp_path, monkeypatch, capsys
):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path / "journal")
    monkeypatch.setattr(
        agent_run, "resolve_binary", lambda _provider: Path("/bin/echo")
    )
    monkeypatch.setattr(
        agent_run, "binary_version", lambda _binary, _provider: "test-cli"
    )
    snapshot_calls = []

    def snapshot_while_locked(_provider):
        lock_path = agent_run.serial_lock_path(
            "codex-family", Path(data["journal"]["root"])
        )
        with open(lock_path, "a+", encoding="utf-8") as handle:
            with pytest.raises(BlockingIOError):
                agent_run.fcntl.flock(
                    handle.fileno(), agent_run.fcntl.LOCK_EX | agent_run.fcntl.LOCK_NB
                )
        snapshot_calls.append("locked")
        return {}

    monkeypatch.setattr(agent_run, "session_snapshot", snapshot_while_locked)
    prompt = "private integration prompt that must never enter the receipt"
    args = SimpleNamespace(
        provider="codex",
        task_shape=None,
        model="gpt-5.6-terra",
        effort="medium",
        seat="codex-landing",
        producer_provider=None,
        producer_run_id=None,
        checkpoint_event=None,
        risk_trigger=[],
        cwd=str(tmp_path),
        mode="read-only",
        allow_write=False,
        skill=["auto"],
        show_stderr=False,
        no_provider_tools=False,
        no_skills=True,
        timeout_seconds=10,
        minimal_runtime=True,
        trust_workspace=False,
        prompt=prompt,
    )
    assert agent_run.run_provider(args, data) == 0
    capsys.readouterr()
    path = tmp_path / "journal" / f"{tmp_path.name}.jsonl"
    row = json.loads(path.read_text().splitlines()[-1])
    assert row["schema_version"] == 4
    assert row["instruction_bom_digest"] == row["instruction_bom"]["digest"]
    assert row["instruction_bom"]["privacy"]["contains_prompt_text"] is False
    assert row["instruction_bom"]["execution"]["mode"] == "read-only"
    assert row["stage_telemetry"]["serial_lock"]["status"] == "acquired"
    assert row["stage_telemetry"]["serial_lock"]["group"] == "codex-family"
    assert snapshot_calls == ["locked", "locked"]
    assert row["session_attribution"] in {"stream-json", "file-diff", "ambiguous"}
    assert prompt not in json.dumps(row)


def test_run_public_seam_named_cursor_health_unverified_exits_three(
    tmp_path, monkeypatch, capsys
):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path / "journal")
    monkeypatch.setattr(
        agent_run, "resolve_binary", lambda _provider: Path("/bin/echo")
    )
    monkeypatch.setattr(
        agent_run,
        "discover_provider_models",
        lambda _provider, _binary: {
            "status": "catalog-listed",
            "models": [{"id": "composer-2.5"}],
        },
    )
    monkeypatch.setattr(
        agent_run, "binary_version", lambda _binary, _provider: "test-cli"
    )
    monkeypatch.setattr(agent_run, "session_snapshot", lambda _provider: {})
    args = SimpleNamespace(
        provider="cursor",
        task_shape=None,
        model="composer-2.5",
        effort=None,
        seat="codex-landing",
        producer_provider=None,
        producer_run_id=None,
        checkpoint_event=None,
        risk_trigger=[],
        cwd=str(tmp_path),
        mode="read-only",
        allow_write=False,
        skill=["auto"],
        show_stderr=False,
        no_provider_tools=False,
        no_skills=True,
        timeout_seconds=10,
        minimal_runtime=False,
        trust_workspace=True,
        prompt="health contract",
    )
    assert agent_run.run_provider(args, data) == 3
    capsys.readouterr()
    path = tmp_path / "journal" / f"{tmp_path.name}.jsonl"
    row = json.loads(path.read_text().splitlines()[-1])
    assert row["run_status"] == "provider-health-unverified"
    assert row["failure_class"] == "provider-health-unverified"
    assert row["provider_health_evidence"]["status"] == "unverified"


def test_route_doctor_explains_catalog_quota_evidence_and_blockers(
    tmp_path,
    monkeypatch,
):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    active_now = agent_run.utc_now()
    (tmp_path / "agent-skill-advisor-layer.jsonl").write_text(
        json.dumps(
            {
                "provider_id": "grok",
                "model_requested": "grok-4.5",
                "run_status": "completed",
                "exit_code": 1,
                "failure_class": "quota-exhausted",
                "started_at": active_now,
            }
        )
        + "\n"
    )
    with (tmp_path / "agent-skill-advisor-layer.jsonl").open("a") as handle:
        for provider_id, model in (
            ("claude", "opus"),
            ("codex", "gpt-5.6-sol"),
        ):
            handle.write(
                json.dumps(
                    {
                        "provider_id": provider_id,
                        "model_requested": model,
                        "model_observed": model,
                        "run_status": "completed",
                        "exit_code": 0,
                        "failure_class": "none",
                        "started_at": active_now,
                    }
                )
                + "\n"
            )
    monkeypatch.setattr(
        agent_run, "resolve_binary", lambda _provider: Path("/bin/echo")
    )
    monkeypatch.setattr(
        agent_run, "binary_version", lambda _binary, _provider: "test-cli"
    )
    monkeypatch.setattr(agent_run, "session_snapshot", lambda _provider: {})

    def fake_catalog(provider, _binary):
        if provider.get("display_name") == "Cursor":
            return {
                "status": "catalog-listed",
                "models": [
                    {"id": "auto"},
                    {"id": "composer-2.5"},
                    {"id": "gemini-2.5-pro"},
                ],
            }
        return {
            "status": "static-config",
            "models": [{"id": model} for model in provider.get("model_options", [])],
        }

    monkeypatch.setattr(agent_run, "discover_provider_models", fake_catalog)
    report = agent_run.build_route_doctor(
        data, route_name="fable_final_review", repo="agent-skill-advisor-layer"
    )

    cursor = next(row for row in report["providers"] if row["provider_id"] == "cursor")
    assert cursor["catalog_status"] == "catalog-listed"
    assert cursor["catalog_model_count"] == 3
    assert len(cursor["catalog_sha256"]) == 64
    grok = next(row for row in report["providers"] if row["provider_id"] == "grok")
    assert grok["live_evidence"]["status"] == "quota-exhausted"
    assert grok["live_evidence"]["cooldown"] == "unknown"
    assert grok["model_evidence"]["grok-4.5"]["status"] == "quota-exhausted"

    route = report["routes"][0]
    assert route["route"] == "fable_final_review"
    assert route["status"] == "disabled"
    assert route["blockers"] == [
        {
            "code": "route-policy-disabled",
            "detail": "disabled-fable-live-canary-required",
        },
        {
            "code": "live-evidence-unverified",
            "detail": "no-live-evidence",
        },
    ]
    assert report["reviewer_graph"]["anthropic"] == ["openai"]
    assert report["reviewer_graph"]["google"] == ["anthropic", "openai"]

    quota_report = agent_run.build_route_doctor(
        data, route_name="final_review", repo="agent-skill-advisor-layer"
    )
    assert quota_report["routes"][0]["status"] == "blocked"
    assert quota_report["routes"][0]["blockers"] == [
        {
            "code": "live-quota-exhausted",
            "detail": "cooldown:unknown",
        }
    ]


def test_live_evidence_requires_verified_health_for_broker_model(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    active_now = agent_run.utc_now()
    path = tmp_path / "demo.jsonl"
    path.write_text(
        json.dumps(
            {
                "provider_id": "cursor",
                "model_requested": "composer-2.5",
                "model_observed": "composer-2.5",
                "run_status": "completed",
                "exit_code": 0,
                "session_status": "ambiguous-concurrent-artifacts",
                "provider_health_evidence": {"status": "unverified"},
                "started_at": active_now,
            }
        )
        + "\n"
    )
    assert (
        agent_run.latest_provider_evidence(data, "demo", "cursor", "composer-2.5")[
            "status"
        ]
        == "run-succeeded-health-unverified"
    )

    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "provider_id": "cursor",
                    "model_requested": "composer-2.5",
                    "model_observed": "composer-2.5",
                    "run_status": "completed",
                    "exit_code": 0,
                    "session_status": "attributed-correlated-artifacts",
                    "provider_health_evidence": {
                        "status": "verified-native-session-model",
                        "model_observed": "composer-2.5",
                    },
                    "started_at": active_now,
                }
            )
            + "\n"
        )
    assert (
        agent_run.latest_provider_evidence(data, "demo", "cursor", "composer-2.5")[
            "status"
        ]
        == "live-run-verified"
    )


@pytest.mark.parametrize("provider_id", ["claude", "codex"])
def test_live_evidence_requires_observed_model_for_every_provider(
    tmp_path, provider_id
):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    requested = "opus" if provider_id == "claude" else "gpt-5.6-sol"
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "provider_id": provider_id,
                "model_requested": requested,
                "model_observed": "unknown",
                "run_status": "completed",
                "exit_code": 0,
                "session_status": "attributed-single-artifact",
                "provider_health_evidence": {"status": "not-applicable"},
                "started_at": agent_run.utc_now(),
            }
        )
        + "\n"
    )
    assert (
        agent_run.latest_provider_evidence(data, "demo", provider_id, requested)[
            "status"
        ]
        == "run-succeeded-health-unverified"
    )


def test_stale_live_evidence_blocks_route(tmp_path, monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    data["journal"]["live_evidence_max_age_seconds"] = 60
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "provider_id": "grok",
                "model_requested": "grok-4.5",
                "run_status": "completed",
                "exit_code": 0,
                "failure_class": "none",
                "started_at": "2000-01-01T00:00:00Z",
            }
        )
        + "\n"
    )
    evidence = agent_run.latest_provider_evidence(data, "demo", "grok", "grok-4.5")
    assert evidence["status"] == "stale-live-evidence"
    assert evidence["max_age_seconds"] == 60

    monkeypatch.setattr(
        agent_run, "resolve_binary", lambda _provider: Path("/bin/echo")
    )
    monkeypatch.setattr(
        agent_run, "binary_version", lambda _binary, _provider: "test-cli"
    )
    monkeypatch.setattr(
        agent_run,
        "discover_provider_models",
        lambda provider, _binary: {
            "status": "static-config",
            "models": [{"id": model} for model in provider.get("model_options", [])],
        },
    )
    report = agent_run.build_route_doctor(data, route_name="final_review", repo="demo")
    assert report["routes"][0]["status"] == "degraded"
    assert report["routes"][0]["blockers"] == [
        {
            "code": "live-evidence-unverified",
            "detail": "stale-live-evidence",
        }
    ]


def test_future_dated_live_evidence_fails_closed(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    future = (
        agent_run.dt.datetime.now(agent_run.dt.timezone.utc)
        + agent_run.dt.timedelta(hours=1)
    ).isoformat()
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "provider_id": "grok",
                "model_requested": "grok-4.5",
                "run_status": "completed",
                "exit_code": 0,
                "failure_class": "none",
                "started_at": future,
            }
        )
        + "\n"
    )
    evidence = agent_run.latest_provider_evidence(data, "demo", "grok", "grok-4.5")
    assert evidence["status"] == "stale-live-evidence"
    assert evidence["reason"] == "future-timestamp"


def test_small_future_clock_skew_remains_usable(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    within_tolerance = (
        agent_run.dt.datetime.now(agent_run.dt.timezone.utc)
        + agent_run.dt.timedelta(seconds=299)
    ).isoformat()
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "provider_id": "codex",
                "model_requested": "gpt-5.6-sol",
                "model_observed": "gpt-5.6-sol",
                "run_status": "completed",
                "exit_code": 0,
                "failure_class": "none",
                "started_at": within_tolerance,
            }
        )
        + "\n"
    )
    evidence = agent_run.latest_provider_evidence(data, "demo", "codex", "gpt-5.6-sol")
    assert evidence["status"] == "live-run-verified"


def test_fractional_future_clock_skew_over_limit_fails_closed(tmp_path, monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    real_datetime = agent_run.dt.datetime
    now = real_datetime(2026, 7, 14, 12, 0, 0, tzinfo=agent_run.dt.timezone.utc)

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(agent_run.dt, "datetime", FixedDateTime)
    future = (now + agent_run.dt.timedelta(seconds=300.5)).isoformat()
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "provider_id": "codex",
                "model_requested": "gpt-5.6-sol",
                "model_observed": "gpt-5.6-sol",
                "run_status": "completed",
                "exit_code": 0,
                "failure_class": "none",
                "started_at": future,
            }
        )
        + "\n"
    )
    evidence = agent_run.latest_provider_evidence(data, "demo", "codex", "gpt-5.6-sol")
    assert evidence["status"] == "stale-live-evidence"
    assert evidence["reason"] == "future-timestamp"


def test_fractional_live_evidence_age_over_ttl_fails_closed(tmp_path, monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    real_datetime = agent_run.dt.datetime
    now = real_datetime(2026, 7, 14, 12, 0, 0, tzinfo=agent_run.dt.timezone.utc)

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(agent_run.dt, "datetime", FixedDateTime)
    expired = (
        now
        - agent_run.dt.timedelta(
            seconds=data["journal"]["live_evidence_max_age_seconds"] + 0.5
        )
    ).isoformat()
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "provider_id": "codex",
                "model_requested": "gpt-5.6-sol",
                "model_observed": "gpt-5.6-sol",
                "run_status": "completed",
                "exit_code": 0,
                "failure_class": "none",
                "started_at": expired,
            }
        )
        + "\n"
    )
    evidence = agent_run.latest_provider_evidence(data, "demo", "codex", "gpt-5.6-sol")
    assert evidence["status"] == "stale-live-evidence"


def test_route_doctor_rejects_unknown_task_shape(capsys):
    code = agent_run.main(
        [
            "--manifest",
            str(ROOT / "agent-providers.yaml"),
            "doctor",
            "--task-shape",
            "definitely-not-a-route",
        ]
    )
    assert code == 2
    assert "unknown route" in capsys.readouterr().err


def test_route_doctor_rejects_repo_paths_before_provider_discovery(monkeypatch, capsys):
    monkeypatch.setattr(
        agent_run,
        "build_route_doctor",
        lambda *_args, **_kwargs: pytest.fail(
            "provider discovery must not run for an invalid repo slug"
        ),
    )
    code = agent_run.main(
        [
            "--manifest",
            str(ROOT / "agent-providers.yaml"),
            "doctor",
            "--repo",
            "../outside",
        ]
    )
    assert code == 2
    assert "--repo must be a project slug" in capsys.readouterr().err


def test_ibom_command_reads_versioned_receipt_without_transcript(tmp_path, capsys):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    receipt = {
        "run_id": "run-ibom",
        "repo": "demo",
        "instruction_bom": {
            "version": 1,
            "digest": "d" * 64,
            "privacy": {"contains_prompt_text": False},
        },
    }
    (tmp_path / "demo.jsonl").write_text(json.dumps(receipt) + "\n")
    args = type(
        "Args",
        (),
        {
            "repo": "demo",
            "run_id": "run-ibom",
            "cwd": str(tmp_path),
        },
    )()
    assert agent_run.ibom(args, data) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"instruction_bom": receipt["instruction_bom"]}
    assert "transcript" not in json.dumps(output)


def test_explicit_task_shape_route_selects_model_effort_and_seat():
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    args = type(
        "Args",
        (),
        {
            "provider": "auto",
            "task_shape": "secondary_final_review",
            "model": None,
            "effort": None,
            "seat": None,
        },
    )()
    assert agent_run.resolve_route(args, data) == (
        "codex",
        "gpt-5.6-sol",
        "xhigh",
        "codex-final-review",
        "secondary_final_review",
    )


def test_auto_route_rejects_model_or_seat_override():
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    args = type(
        "Args",
        (),
        {
            "provider": "auto",
            "task_shape": "secondary_final_review",
            "model": "opus",
            "effort": None,
            "seat": "codex-final-review",
        },
    )()
    with pytest.raises(agent_run.ProviderRunError, match="immutable"):
        agent_run.resolve_route(args, data)


def test_explicit_provider_rejects_task_shape():
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    args = type(
        "Args",
        (),
        {
            "provider": "grok",
            "task_shape": "final_review",
            "model": "grok-4.5",
            "effort": "high",
            "seat": "codex-final-review",
        },
    )()
    with pytest.raises(agent_run.ProviderRunError, match="only with provider auto"):
        agent_run.resolve_route(args, data)


def test_final_review_requires_cross_family_producer(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "producer-1",
                "provider_id": "codex",
                **verified_producer_model("codex", "gpt-5.6-terra", "openai"),
                "seat": "codex-landing",
                "session_id": "session-1",
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": []},
            }
        )
        + "\n"
    )
    args = type(
        "Args", (), {"producer_provider": "codex", "producer_run_id": "producer-1"}
    )()
    policy, producer = agent_run.validate_review_independence(
        "claude_final_review", "claude", args, data, "demo"
    )
    assert policy == "cross-family"
    assert producer["run_id"] == "producer-1"
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "producer-2",
                "provider_id": "claude",
                **verified_producer_model("claude", "opus", "anthropic"),
                "seat": "claude-landing",
                "session_id": "session-2",
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": []},
            }
        )
        + "\n"
    )
    args.producer_provider = "claude"
    args.producer_run_id = "producer-2"
    with pytest.raises(agent_run.ProviderRunError, match="cross-family"):
        agent_run.validate_review_independence(
            "claude_final_review", "claude", args, data, "demo"
        )


def test_independent_supplement_only_accepts_canon_eligible_producer_route(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    producer = {
        "run_id": "same-family-producer",
        "provider_id": "codex",
        **verified_producer_model("codex", "gpt-5.6-terra", "openai"),
        "seat": "codex-landing",
        "session_id": "producer-session",
        "repo": "demo",
        "run_status": "completed",
        "exit_code": 0,
        "mode": "execute",
        "route": "ordinary_bug_fix",
        "risk_overlay": {"triggers": []},
    }
    journal = tmp_path / "demo.jsonl"
    journal.write_text(json.dumps(producer) + "\n")
    args = SimpleNamespace(
        producer_provider="codex", producer_run_id="same-family-producer"
    )
    policy, _reference = agent_run.validate_review_independence(
        "secondary_final_review", "codex", args, data, "demo"
    )
    assert policy == "independent-supplement"

    producer["route"] = "judgment"
    journal.write_text(json.dumps(producer) + "\n")
    with pytest.raises(agent_run.ProviderRunError, match="eligible producer route"):
        agent_run.validate_review_independence(
            "secondary_final_review", "codex", args, data, "demo"
        )


def test_cross_family_review_uses_cursor_model_family_not_broker(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "cursor-grok-producer",
                "provider_id": "cursor",
                **verified_producer_model("cursor", "cursor-grok-4.5-high", "xai"),
                "seat": "codex-landing",
                "session_id": "cursor-session",
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": []},
            }
        )
        + "\n"
    )
    args = type(
        "Args",
        (),
        {
            "producer_provider": "cursor",
            "producer_run_id": "cursor-grok-producer",
        },
    )()
    with pytest.raises(agent_run.ProviderRunError, match="both resolve to 'xai'"):
        agent_run.validate_review_independence(
            "final_review", "grok", args, data, "demo"
        )


@pytest.mark.parametrize("producer_family", ["undisclosed", "unknown", ""])
def test_cross_family_review_rejects_undisclosed_producer_family(
    tmp_path, producer_family
):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "cursor-auto-producer",
                "provider_id": "cursor",
                "model_requested": "auto",
                "model_observed": "auto",
                "model_family": producer_family,
                "session_status": "attributed-correlated-artifacts",
                "provider_health_evidence": {
                    "status": "verified-native-session-model",
                    "model_observed": "auto",
                },
                "seat": "codex-landing",
                "session_id": "cursor-session",
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": []},
            }
        )
        + "\n"
    )
    args = SimpleNamespace(
        producer_provider="cursor", producer_run_id="cursor-auto-producer"
    )
    with pytest.raises(agent_run.ProviderRunError, match="disclosed model families"):
        agent_run.validate_review_independence(
            "final_review", "grok", args, data, "demo"
        )


def test_cross_family_review_rejects_undisclosed_reviewer_family(tmp_path, monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "claude-producer",
                "provider_id": "claude",
                **verified_producer_model("claude", "opus", "anthropic"),
                "seat": "claude-landing",
                "session_id": "claude-session",
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": []},
            }
        )
        + "\n"
    )
    original_provider_family = agent_run.provider_family
    monkeypatch.setattr(
        agent_run,
        "provider_family",
        lambda provider_id, *args: (
            "undisclosed"
            if provider_id == "grok"
            else original_provider_family(provider_id, *args)
        ),
    )
    args = SimpleNamespace(
        producer_provider="claude", producer_run_id="claude-producer"
    )
    with pytest.raises(agent_run.ProviderRunError, match="disclosed model families"):
        agent_run.validate_review_independence(
            "final_review", "grok", args, data, "demo"
        )


def test_cross_family_review_rejects_broker_producer_without_model_identity(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "legacy-cursor-producer",
                "provider_id": "cursor",
                "seat": "codex-landing",
                "session_id": "cursor-session",
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": []},
            }
        )
        + "\n"
    )
    args = SimpleNamespace(
        producer_provider="cursor", producer_run_id="legacy-cursor-producer"
    )
    with pytest.raises(agent_run.ProviderRunError, match="observed model identity"):
        agent_run.validate_review_independence(
            "final_review", "grok", args, data, "demo"
        )


def test_cross_family_review_rejects_requested_only_broker_model_identity(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "legacy-requested-only-cursor-producer",
                "provider_id": "cursor",
                "model_requested": "composer-2.5",
                "model_family": "cursor",
                "seat": "codex-landing",
                "session_id": "cursor-session",
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": []},
            }
        )
        + "\n"
    )
    args = SimpleNamespace(
        producer_provider="cursor",
        producer_run_id="legacy-requested-only-cursor-producer",
    )
    with pytest.raises(agent_run.ProviderRunError, match="observed model identity"):
        agent_run.validate_review_independence(
            "claude_final_review", "claude", args, data, "demo"
        )


def test_cross_family_review_rejects_unverified_broker_model_observation(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "unverified-cursor-producer",
                "provider_id": "cursor",
                "model_requested": "composer-2.5",
                "model_observed": "composer-2.5",
                "model_family": "cursor",
                "seat": "codex-landing",
                "session_id": "cursor-session",
                "session_status": "ambiguous-concurrent-artifacts",
                "provider_health_evidence": {"status": "unverified"},
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": []},
            }
        )
        + "\n"
    )
    args = SimpleNamespace(
        producer_provider="cursor", producer_run_id="unverified-cursor-producer"
    )
    with pytest.raises(agent_run.ProviderRunError, match="verified model evidence"):
        agent_run.validate_review_independence(
            "claude_final_review", "claude", args, data, "demo"
        )


def test_cross_family_review_rejects_unknown_broker_session_status(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    receipt = {
        "run_id": "candidate-session-cursor-producer",
        "provider_id": "cursor",
        **verified_producer_model("cursor", "composer-2.5", "cursor"),
        "seat": "codex-landing",
        "session_id": "cursor-session",
        "session_status": "candidate",
        "repo": "demo",
        "run_status": "completed",
        "exit_code": 0,
        "mode": "execute",
        "risk_overlay": {"triggers": []},
    }
    (tmp_path / "demo.jsonl").write_text(json.dumps(receipt) + "\n")
    args = SimpleNamespace(
        producer_provider="cursor",
        producer_run_id="candidate-session-cursor-producer",
    )
    with pytest.raises(agent_run.ProviderRunError, match="verified model evidence"):
        agent_run.validate_review_independence(
            "claude_final_review", "claude", args, data, "demo"
        )


def test_cross_family_review_canonicalizes_legacy_broker_provider_alias(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    receipt = {
        "run_id": "legacy-alias-cursor-producer",
        "provider_id": "cursor-auto",
        **verified_producer_model("cursor", "composer-2.5", "cursor"),
        "seat": "codex-landing",
        "session_id": "cursor-session",
        "session_status": "candidate",
        "repo": "demo",
        "run_status": "completed",
        "exit_code": 0,
        "mode": "execute",
        "risk_overlay": {"triggers": []},
    }
    (tmp_path / "demo.jsonl").write_text(json.dumps(receipt) + "\n")
    args = SimpleNamespace(
        producer_provider=None,
        producer_run_id="legacy-alias-cursor-producer",
    )
    with pytest.raises(agent_run.ProviderRunError, match="verified model evidence"):
        agent_run.validate_review_independence(
            "claude_final_review", "claude", args, data, "demo"
        )


def test_live_evidence_rejects_unknown_broker_session_status(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "provider_id": "cursor",
                "model_requested": "composer-2.5",
                "model_observed": "composer-2.5",
                "run_status": "completed",
                "exit_code": 0,
                "session_status": "candidate",
                "provider_health_evidence": {"status": "verified-native-session-model"},
                "started_at": agent_run.utc_now(),
            }
        )
        + "\n"
    )
    evidence = agent_run.latest_provider_evidence(
        data, "demo", "cursor", "composer-2.5"
    )
    assert evidence["status"] == "run-succeeded-health-unverified"


def test_run_public_seam_rejects_requested_only_broker_model_identity(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    slug = tmp_path.name
    (tmp_path / f"{slug}.jsonl").write_text(
        json.dumps(
            {
                "run_id": "legacy-cursor-producer",
                "provider_id": "cursor",
                "model_requested": "composer-2.5",
                "model_family": "cursor",
                "seat": "codex-landing",
                "session_id": "cursor-session",
                "repo": slug,
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": []},
            }
        )
        + "\n"
    )
    args = SimpleNamespace(
        provider="auto",
        task_shape="final_review",
        model=None,
        effort=None,
        seat=None,
        producer_provider="cursor",
        producer_run_id="legacy-cursor-producer",
        checkpoint_event=None,
        risk_trigger=[],
        cwd=str(tmp_path),
        mode="read-only",
        allow_write=False,
        skill=["auto"],
        show_stderr=False,
        no_provider_tools=False,
        no_skills=True,
        timeout_seconds=10,
        minimal_runtime=False,
        trust_workspace=False,
        prompt="review legacy producer",
    )
    with pytest.raises(agent_run.ProviderRunError, match="observed model identity"):
        agent_run.run_provider(args, data)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"repo": "other"}, "repo does not match"),
        ({"run_status": "failed", "exit_code": 1}, "did not complete successfully"),
        ({"mode": "read-only"}, "not a write-capable execution"),
    ],
)
def test_final_review_rejects_invalid_producer_record(tmp_path, overrides, message):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    row = {
        "run_id": "producer-1",
        "provider_id": "codex",
        **verified_producer_model("codex", "gpt-5.6-terra", "openai"),
        "seat": "codex-landing",
        "session_id": "session-1",
        "repo": "demo",
        "run_status": "completed",
        "exit_code": 0,
        "mode": "execute",
        "risk_overlay": {"triggers": []},
    }
    row.update(overrides)
    (tmp_path / "demo.jsonl").write_text(json.dumps(row) + "\n")
    args = type(
        "Args", (), {"producer_provider": "codex", "producer_run_id": "producer-1"}
    )()
    with pytest.raises(agent_run.ProviderRunError, match=message):
        agent_run.validate_review_independence(
            "claude_final_review", "claude", args, data, "demo"
        )


def test_risk_review_uses_normalized_governance_effort(tmp_path):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "producer-risk",
                "provider_id": "codex",
                **verified_producer_model("codex", "gpt-5.6-terra", "openai"),
                "seat": "codex-landing",
                "session_id": "session-1",
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": ["money"]},
            }
        )
        + "\n"
    )
    args = type(
        "Args",
        (),
        {
            "producer_provider": "codex",
            "producer_run_id": "producer-risk",
        },
    )()
    policy, _producer = agent_run.validate_review_independence(
        "final_review", "grok", args, data, "demo"
    )
    assert policy == "cross-family"
    (tmp_path / "demo.jsonl").write_text(
        json.dumps(
            {
                "run_id": "producer-risk-claude",
                "provider_id": "claude",
                **verified_producer_model("claude", "opus", "anthropic"),
                "seat": "claude-landing",
                "session_id": "session-2",
                "repo": "demo",
                "run_status": "completed",
                "exit_code": 0,
                "mode": "execute",
                "risk_overlay": {"triggers": ["money"]},
            }
        )
        + "\n"
    )
    args.producer_provider = "claude"
    args.producer_run_id = "producer-risk-claude"
    policy, _producer = agent_run.validate_review_independence(
        "codex_final_review", "codex", args, data, "demo"
    )
    assert policy == "cross-family"


def test_grok_review_route_is_enabled_but_fable_routes_fail_closed():
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    args = type(
        "Args",
        (),
        {
            "provider": "auto",
            "task_shape": "final_review",
            "model": None,
            "effort": None,
            "seat": None,
        },
    )()
    assert agent_run.resolve_route(args, data) == (
        "grok",
        "grok-4.5",
        "high",
        "codex-final-review",
        "final_review",
    )
    args.task_shape = "codex_final_review"
    assert agent_run.resolve_route(args, data) == (
        "codex",
        "gpt-5.6-sol",
        "xhigh",
        "codex-final-review",
        "codex_final_review",
    )
    assert (
        agent_run.route_binding(data, "claude_final_review")["governance_effort"]
        == "xhigh"
    )
    for shape in ("fable_final_review", "arbitration"):
        args = type(
            "Args",
            (),
            {
                "provider": "auto",
                "task_shape": shape,
                "model": None,
                "effort": None,
                "seat": None,
            },
        )()
        with pytest.raises(agent_run.ProviderRunError, match="disabled"):
            agent_run.resolve_route(args, data)


def test_environment_strips_api_billing_keys(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "secret")
    env, stripped = agent_run.scrub_environment({"strip_environment": ["XAI_API_KEY"]})
    assert "XAI_API_KEY" not in env
    assert stripped == ["XAI_API_KEY"]


def test_failure_classifies_quota_without_storing_error_body():
    assert (
        agent_run.classify_failure(
            "completed", 1, "402 Payment Required: spending-limit; run out of credits"
        )
        == "quota-exhausted"
    )
    assert (
        agent_run.classify_failure("timed-out", 124, "review text mentions 402")
        == "timeout"
    )
    assert (
        agent_run.classify_failure(
            "completed", 0, "auxiliary title request returned 402"
        )
        == "none"
    )
    assert (
        agent_run.classify_failure(
            "provider-health-unverified",
            3,
            "auxiliary title request returned 402 spending-limit",
        )
        == "provider-health-unverified"
    )
    assert (
        agent_run.classify_failure(
            "review-independence-violation",
            3,
            "auxiliary title request returned 402 spending-limit",
        )
        == "review-independence-violation"
    )
    assert (
        agent_run.classify_failure(
            "completed",
            1,
            "429 subscription:free-usage-exhausted rolling 24-hour limit",
        )
        == "quota-exhausted"
    )
    assert (
        agent_run.classify_failure(
            "completed",
            1,
            "ActionRequiredError: Review Data Policy You must acknowledge Fable 5 retention policy",
        )
        == "action-required-data-policy"
    )
    assert (
        agent_run.classify_failure(
            "completed", 1, "", stdout="HTTP 529 overloaded upstream"
        )
        == "upstream-overload"
    )
    assert (
        agent_run.classify_failure(
            "completed", 1, "", stdout="429 Too Many Requests retry later"
        )
        == "rate-limited"
    )
    assert (
        agent_run.classify_failure(
            "completed", 1, "ignored", stdout="401 Unauthorized invalid token"
        )
        == "auth-expired"
    )
    assert (
        agent_run.classify_failure(
            "completed", 1, "", stdout="upstream deadline exceeded"
        )
        == "timeout"
    )
    assert (
        agent_run.classify_failure(
            "completed", 1, "token expired; login required", stdout=""
        )
        == "auth-expired"
    )
    assert (
        agent_run.classify_failure(
            "completed", 1, "", stdout="quota exceeded: insufficient credits"
        )
        == "quota-exhausted"
    )


def test_effective_timeout_uses_route_canon(monkeypatch, tmp_path):
    canon = tmp_path / "routing-policy.yaml"
    canon.write_text(
        """
version: 1
task_shapes:
  judgment:
    execution_seat: [claude]
    execution_model: {claude: opus}
    execution_effort: high
    execution_mode: careful
    review_effort_floor: high
runtime_routes:
  codex_final_review:
    provider: codex
    model: gpt-5.6-sol
    effort: xhigh
    seat: codex-final-review
    timeout_seconds: 900
"""
    )
    config = {"routing_canon": str(canon)}
    args = argparse.Namespace(timeout_seconds=None)
    assert agent_run.effective_timeout_seconds(args, "codex_final_review", config) == 900
    args_default_explicit = argparse.Namespace(timeout_seconds=300)
    assert (
        agent_run.effective_timeout_seconds(
            args_default_explicit, "codex_final_review", config
        )
        == 300
    )
    args_explicit = argparse.Namespace(timeout_seconds=120)
    assert (
        agent_run.effective_timeout_seconds(args_explicit, "codex_final_review", config)
        == 120
    )


def test_extract_claude_session_from_stream_events():
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-abc"},
        {"type": "assistant", "message": {}},
    ]
    assert agent_run.extract_claude_session_from_events(events) == "sess-abc"


def test_extract_codex_session_and_claude_result_from_stream_events(tmp_path):
    command = ["claude", "--print", "--output-format", "text", "prompt"]
    assert agent_run.configure_claude_stream_json("claude", command) is True
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command
    codex_command = ["codex", "exec", "--json", "prompt"]
    assert agent_run.configure_claude_stream_json("codex", codex_command) is False
    assert "--verbose" not in codex_command
    assert (
        agent_run.extract_codex_session_from_events(
            [{"type": "thread.started", "thread_id": "thread-abc"}]
        )
        == "thread-abc"
    )
    assert (
        agent_run.extract_claude_agent_message(
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "draft"}]},
                },
                {"type": "result", "result": "final"},
            ]
        )
        == "final"
    )
    transcript = tmp_path / "sess-abc.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "assistant", "message": {"model": "claude-opus-test"}}
        )
        + "\n"
    )
    record = agent_run.stream_session_record(
        "claude", "sess-abc", {str(transcript): (1, 1)}
    )
    assert record["session_id"] == "sess-abc"
    assert record["model_observed"] == "claude-opus-test"
    assert record["session_status"] == "attributed-stream-json"


def test_serial_group_for_provider():
    assert agent_run.serial_group_for_provider("claude") == "claude-family"
    assert (
        agent_run.serial_group_for_provider("claude", {"serial_group": "custom"})
        == "custom"
    )
    assert agent_run.serial_group_for_provider("cursor", {}) is None
    assert agent_run.serial_group_for_provider("cursor") is None
    assert (
        agent_run.serial_group_for_provider(
            "claude", {"provider": "claude", "timeout_seconds": 600}
        )
        is None
    )


def test_kill_process_tree_kills_group_when_leader_exited(monkeypatch):
    calls = []

    class ExitedLeader:
        pid = 123

        def poll(self):
            return 0

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            return 0

    monkeypatch.setattr(
        agent_run.os,
        "killpg",
        lambda pgid, sig: calls.append(("killpg", pgid, sig)),
    )
    agent_run.kill_process_tree(ExitedLeader())  # type: ignore[arg-type]
    assert ("killpg", 123, agent_run.signal.SIGKILL) in calls


def test_kill_process_tree_uses_process_group(monkeypatch):
    calls = []

    class Running:
        pid = 123

        def poll(self):
            return None

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            return -9

    monkeypatch.setattr(
        agent_run.os,
        "killpg",
        lambda pgid, sig: calls.append(("killpg", pgid, sig)),
    )
    agent_run.kill_process_tree(Running())  # type: ignore[arg-type]
    assert ("killpg", 123, agent_run.signal.SIGKILL) in calls


def test_kill_process_tree_swallows_process_lookup_error(monkeypatch):
    class Running:
        pid = 123

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(
        agent_run.os,
        "killpg",
        lambda _pgid, _sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    agent_run.kill_process_tree(Running())  # type: ignore[arg-type]


def test_kill_process_tree_reaps_orphans_when_leader_exits_first():
    proc = subprocess.Popen(
        ["bash", "-c", "sleep 120 & exit 0"],
        start_new_session=True,
    )
    pgid = proc.pid
    proc.wait(timeout=5)
    assert proc.poll() == 0
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        pytest.skip("orphan child already reaped")
    agent_run.kill_process_tree(proc)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_provider_serial_lock_times_out_and_releases(tmp_path):
    with agent_run.ProviderSerialLock(
        "claude-family", journal_root=tmp_path, wait_seconds=1
    ) as held:
        assert held.telemetry["status"] == "acquired"
        with pytest.raises(agent_run.SerialLockTimeout):
            with agent_run.ProviderSerialLock(
                "claude-family", journal_root=tmp_path, wait_seconds=0
            ):
                pass
    with agent_run.ProviderSerialLock(
        "claude-family", journal_root=tmp_path, wait_seconds=0
    ) as reacquired:
        assert reacquired.telemetry["status"] == "acquired"


def test_serial_lock_timeout_is_journaled(tmp_path, monkeypatch, capsys):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    journal_root = tmp_path / "journal"
    data["journal"]["root"] = str(journal_root)
    monkeypatch.setattr(agent_run, "resolve_binary", lambda _provider: Path("/bin/echo"))
    monkeypatch.setattr(
        agent_run, "binary_version", lambda _binary, _provider: "test-cli"
    )
    monkeypatch.setattr(agent_run, "repo_slug", lambda _cwd: tmp_path.name)
    monkeypatch.setattr(
        agent_run,
        "session_snapshot",
        lambda _provider: pytest.fail("lock timeout must not attribute a session"),
    )
    monkeypatch.setattr(
        agent_run,
        "discover_provider_models",
        lambda _provider, _binary: {
            "status": "static-config",
            "models": [{"id": "gpt-5.6-terra"}],
        },
    )
    monkeypatch.setattr(
        agent_run.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("provider must not spawn without the lock"),
    )
    args = SimpleNamespace(
        provider="codex",
        task_shape=None,
        model="gpt-5.6-terra",
        effort="medium",
        seat="codex-landing",
        producer_provider=None,
        producer_run_id=None,
        checkpoint_event=None,
        risk_trigger=[],
        cwd=str(tmp_path),
        mode="read-only",
        allow_write=False,
        skill=["auto"],
        show_stderr=False,
        no_provider_tools=False,
        no_skills=True,
        timeout_seconds=1,
        minimal_runtime=False,
        trust_workspace=False,
        prompt="lock timeout receipt",
    )
    with agent_run.ProviderSerialLock(
        "codex-family", journal_root=journal_root, wait_seconds=1
    ):
        assert agent_run.run_provider(args, data) == 75
    capsys.readouterr()
    path = journal_root / f"{tmp_path.name}.jsonl"
    row = json.loads(path.read_text().splitlines()[-1])
    assert row["run_status"] == "serial-lock-timeout"
    assert row["failure_class"] == "serial-lock-timeout"
    assert row["stage_telemetry"]["serial_lock"]["status"] == "timed-out"


def test_run_blocking_process_starts_new_session(monkeypatch, tmp_path):
    observed = {}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            observed["timeout"] = timeout
            return "ok", ""

    def fake_popen(*args, **kwargs):
        observed["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(agent_run.subprocess, "Popen", fake_popen)
    proc, status, _telemetry = agent_run.run_blocking_process(
        ["provider"], cwd=tmp_path, env={}, timeout_seconds=12
    )
    assert status == "completed"
    assert proc.stdout == "ok"
    assert observed["kwargs"]["start_new_session"] is True
    assert observed["timeout"] == 12


def test_run_claude_stream_json_extracts_result_and_session(monkeypatch, tmp_path):
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "sess-claude",
            }
        )
        + "\n",
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-opus-test",
                    "content": [{"type": "text", "text": "draft"}],
                },
            }
        )
        + "\n",
        json.dumps({"type": "result", "result": "APPROVE"}) + "\n",
    ]
    observed = {}

    class QueueStream:
        def __init__(self, queued):
            self._lines = list(queued)

        def readline(self):
            return self._lines.pop(0) if self._lines else ""

    class FakeProc:
        def __init__(self):
            self.stdout = QueueStream(lines)
            self.stderr = QueueStream([])
            self.returncode = None

        def poll(self):
            return 0 if not self.stdout._lines else None

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    def fake_popen(*args, **kwargs):
        observed["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(agent_run.subprocess, "Popen", fake_popen)

    class RecordingSelector:
        def __init__(self):
            self._streams = []

        def register(self, stream, _mask, label):
            self._streams.append((stream, label))

        def unregister(self, stream):
            self._streams = [(s, l) for s, l in self._streams if s is not stream]

        def select(self, timeout=None):
            for stream, label in list(self._streams):
                if stream._lines:
                    key = type("K", (), {"fileobj": stream, "data": label})()
                    return [(key, None)]
            if self._streams:
                stream, label = self._streams[0]
                key = type("K", (), {"fileobj": stream, "data": label})()
                return [(key, None)]
            return []

    import selectors as _selectors

    monkeypatch.setattr(_selectors, "DefaultSelector", RecordingSelector)
    proc, status, telemetry, events = agent_run.run_claude_stream_json_process(
        ["claude", "--print", "--output-format", "stream-json", "hi"],
        cwd=tmp_path,
        env={},
        timeout_seconds=10,
    )
    assert status == "completed"
    assert proc.returncode == 0
    assert proc.stdout == "APPROVE"
    assert telemetry["stream_mode"] == "claude-stream-json"
    assert agent_run.extract_claude_session_from_events(events) == "sess-claude"
    assert agent_run.extract_claude_model_from_events(events) == "claude-opus-test"
    assert observed["kwargs"]["start_new_session"] is True


def test_cursor_hex_meta_is_decoded(tmp_path):
    db = tmp_path / "store.db"
    conn = sqlite3.connect(db)
    conn.execute("create table meta (key text, value blob)")
    payload = {"agentId": "cursor-123", "createdAt": 123, "name": "New Agent"}
    conn.execute(
        "insert into meta values ('0', ?)", (json.dumps(payload).encode().hex(),)
    )
    conn.commit()
    conn.close()
    assert agent_run.decode_cursor_meta(db)["agentId"] == "cursor-123"


def test_cursor_session_parser_reads_model_only_from_native_blob_metadata(tmp_path):
    db = tmp_path / "store.db"
    conn = sqlite3.connect(db)
    conn.execute("create table meta (key text, value blob)")
    conn.execute("create table blobs (id text primary key, data blob)")
    meta = {"agentId": "cursor-blob-session"}
    conn.execute("insert into meta values ('0', ?)", (json.dumps(meta).encode().hex(),))
    message = {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "private response",
                "providerOptions": {"cursor": {"modelName": "composer-2.5-fast"}},
            }
        ],
    }
    conn.execute(
        "insert into blobs values ('message', ?)",
        (("a" * 64 + json.dumps(message)).encode(),),
    )
    conn.commit()
    conn.close()

    parsed = agent_run.parse_session("cursor", db, "attributed-single-artifact")
    assert parsed["session_id"] == "cursor-blob-session"
    assert parsed["model_observed"] == "composer-2.5-fast"
    assert "private response" not in json.dumps(parsed)


def test_cursor_attribution_rejects_concurrent_sessions_even_when_model_matches(
    tmp_path,
):
    def make_session(name, model):
        db = tmp_path / name / "store.db"
        db.parent.mkdir()
        conn = sqlite3.connect(db)
        conn.execute("create table meta (key text, value blob)")
        conn.execute("create table blobs (id text primary key, data blob)")
        conn.execute(
            "insert into meta values ('0', ?)",
            (json.dumps({"agentId": name}).encode().hex(),),
        )
        payload = {"providerOptions": {"cursor": {"modelName": model}}}
        conn.execute(
            "insert into blobs values ('message', ?)",
            (("a" * 64 + json.dumps(payload)).encode(),),
        )
        conn.commit()
        conn.close()
        return db

    composer = make_session("composer-session", "composer-2.5")
    grok = make_session("grok-session", "cursor-grok-4.5-high")
    transcript = tmp_path / "composer-session.jsonl"
    transcript.write_text("{}\n")
    after = {
        str(composer): agent_run.file_fingerprint(composer),
        str(grok): agent_run.file_fingerprint(grok),
        str(transcript): agent_run.file_fingerprint(transcript),
    }
    session, changed = agent_run.attribute_session(
        "cursor", {}, after, requested_model="composer-2.5"
    )
    assert changed == 3
    assert session["session_id"] == "unknown"
    assert session["session_status"] == "ambiguous-concurrent-artifacts"


def test_cursor_attribution_does_not_hijack_concurrent_same_model_store(tmp_path):
    current = tmp_path / "current-session.jsonl"
    current.write_text("{}\n")
    concurrent = tmp_path / "concurrent-session" / "store.db"
    concurrent.parent.mkdir()
    conn = sqlite3.connect(concurrent)
    conn.execute("create table meta (key text, value blob)")
    conn.execute("create table blobs (id text primary key, data blob)")
    conn.execute(
        "insert into meta values ('0', ?)",
        (json.dumps({"agentId": "concurrent-session"}).encode().hex(),),
    )
    payload = {"providerOptions": {"cursor": {"modelName": "composer-2.5"}}}
    conn.execute(
        "insert into blobs values ('message', ?)",
        (("a" * 64 + json.dumps(payload)).encode(),),
    )
    conn.commit()
    conn.close()
    after = {
        str(current): agent_run.file_fingerprint(current),
        str(concurrent): agent_run.file_fingerprint(concurrent),
    }
    session, changed = agent_run.attribute_session(
        "cursor", {}, after, requested_model="composer-2.5"
    )
    assert changed == 2
    assert session["session_id"] == "unknown"
    assert session["session_status"] == "ambiguous-concurrent-artifacts"


def test_cursor_attribution_rejects_complete_concurrent_pair_when_ours_is_unflushed(
    tmp_path,
):
    def make_store(session_id: str, model: str | None) -> Path:
        db = tmp_path / "chats" / session_id / "store.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("create table meta (key text, value blob)")
        conn.execute("create table blobs (id text primary key, data blob)")
        conn.execute(
            "insert into meta values ('0', ?)",
            (json.dumps({"agentId": session_id}).encode().hex(),),
        )
        if model:
            payload = {"providerOptions": {"cursor": {"modelName": model}}}
            conn.execute(
                "insert into blobs values ('message', ?)",
                (("a" * 64 + json.dumps(payload)).encode(),),
            )
        conn.commit()
        conn.close()
        return db

    ours = "ours-unflushed"
    other = "other-complete"
    ours_db = make_store(ours, None)
    other_db = make_store(other, "composer-2.5")
    ours_jsonl = tmp_path / ours / f"{ours}.jsonl"
    other_jsonl = tmp_path / other / f"{other}.jsonl"
    ours_jsonl.parent.mkdir()
    other_jsonl.parent.mkdir()
    ours_jsonl.write_text("{}\n")
    other_jsonl.write_text("{}\n")
    after = {
        str(path): agent_run.file_fingerprint(path)
        for path in (ours_db, other_db, ours_jsonl, other_jsonl)
    }
    session, changed = agent_run.attribute_session(
        "cursor", {}, after, requested_model="composer-2.5"
    )
    assert changed == 4
    assert session["session_id"] == "unknown"
    assert session["session_status"] == "ambiguous-concurrent-artifacts"
    assert agent_run.provider_health_evidence("cursor", "composer-2.5", session) == {
        "status": "unverified",
        "reason": "session-unattributed",
    }


def test_grok_session_parser_reads_identity_without_content(tmp_path):
    session = tmp_path / "session-id"
    session.mkdir()
    (session / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": "grok-123"},
                "current_model_id": "grok-4.5",
                "generated_title": "private",
            }
        )
    )
    parsed = agent_run.parse_session("grok", session, "attributed-single-artifact")
    assert parsed["session_id"] == "grok-123"
    assert parsed["model_observed"] == "grok-4.5"
    assert "private" not in json.dumps(parsed)


def test_grok_health_requires_primary_model_and_zero_session_errors(tmp_path):
    session = tmp_path / "session-id"
    session.mkdir()
    (session / "summary.json").write_text(json.dumps({"current_model_id": "grok-4.5"}))
    (session / "signals.json").write_text(
        json.dumps(
            {
                "primaryModelId": "grok-4.5",
                "errorCount": 0,
            }
        )
    )
    evidence = agent_run.provider_health_evidence(
        "grok",
        "grok-4.5",
        {
            "session_id": "grok-123",
            "session_ref": str(session),
        },
    )
    assert evidence["status"] == "verified-primary-session"
    (session / "signals.json").write_text(
        json.dumps(
            {
                "primaryModelId": "grok-build",
                "errorCount": 1,
            }
        )
    )
    evidence = agent_run.provider_health_evidence(
        "grok",
        "grok-4.5",
        {
            "session_id": "grok-123",
            "session_ref": str(session),
        },
    )
    assert evidence["status"] == "unverified"


def test_cursor_health_requires_requested_model_to_match_native_session():
    matching = agent_run.provider_health_evidence(
        "cursor",
        "composer-2.5",
        {
            "session_id": "cursor-1",
            "session_ref": "~/.cursor/chats/1/store.db",
            "model_observed": "composer-2.5",
        },
    )
    assert matching["status"] == "verified-native-session-model"

    distinct_fast = agent_run.provider_health_evidence(
        "cursor",
        "composer-2.5",
        {
            "session_id": "cursor-fast",
            "session_ref": "~/.cursor/chats/fast/store.db",
            "model_observed": "composer-2.5-fast",
        },
    )
    assert distinct_fast == {
        "status": "unverified",
        "reason": "native-session-model-mismatch",
        "model_observed": "composer-2.5-fast",
    }

    mismatch = agent_run.provider_health_evidence(
        "cursor",
        "composer-2.5",
        {
            "session_id": "cursor-2",
            "session_ref": "~/.cursor/chats/2/store.db",
            "model_observed": "cursor-grok-4.5-high",
        },
    )
    assert mismatch == {
        "status": "unverified",
        "reason": "native-session-model-mismatch",
        "model_observed": "cursor-grok-4.5-high",
    }


def test_cursor_auto_health_accepts_concrete_native_model_observation():
    evidence = agent_run.provider_health_evidence(
        "cursor",
        "auto",
        {
            "session_id": "cursor-auto-concrete",
            "session_ref": "~/.cursor/chats/auto/store.db",
            "model_observed": "composer-2.5-fast",
        },
    )
    assert evidence == {
        "status": "verified-native-session-model",
        "model_observed": "composer-2.5-fast",
    }


def test_changed_session_attributes_only_one_artifact(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.write_text("x")
    new.write_text("y")
    os.utime(old, ns=(1, 1))
    os.utime(new, ns=(2, 2))
    before = {str(old): agent_run.file_fingerprint(old)}
    after = {
        str(old): agent_run.file_fingerprint(old),
        str(new): agent_run.file_fingerprint(new),
    }
    assert agent_run.changed_session(before, after) == (
        new,
        "attributed-single-artifact",
        1,
    )


def test_changed_session_is_ambiguous_under_concurrency(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.write_text("x")
    two.write_text("y")
    assert agent_run.changed_session(
        {},
        {
            str(one): agent_run.file_fingerprint(one),
            str(two): agent_run.file_fingerprint(two),
        },
    ) == (None, "ambiguous-concurrent-artifacts", 2)


def test_cursor_correlates_jsonl_and_db_for_same_session(tmp_path):
    session_id = "00000000-0000-4000-8000-000000000003"
    transcript = tmp_path / session_id / f"{session_id}.jsonl"
    transcript.parent.mkdir()
    transcript.write_text("")
    db = tmp_path / "chats" / session_id / "store.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("create table meta (key text, value blob)")
    conn.execute(
        "insert into meta values ('0', ?)",
        (json.dumps({"agentId": session_id}).encode().hex(),),
    )
    conn.commit()
    conn.close()
    parsed, count = agent_run.attribute_session(
        "cursor",
        {},
        {
            str(transcript): agent_run.file_fingerprint(transcript),
            str(db): agent_run.file_fingerprint(db),
        },
    )
    assert count == 2
    assert parsed["session_id"] == session_id
    assert parsed["session_status"] == "attributed-correlated-artifacts"


def test_codex_session_parser_keeps_full_uuid(tmp_path):
    path = (
        tmp_path
        / "rollout-2026-07-14T07-47-01-00000000-0000-4000-8000-000000000001.jsonl"
    )
    path.write_text("")
    parsed = agent_run.parse_session("codex", path, "attributed-single-artifact")
    assert parsed["session_id"] == "00000000-0000-4000-8000-000000000001"


def test_journal_is_append_only_private_and_0600(tmp_path):
    path = tmp_path / "runs.jsonl"
    one = {"run_id": "1", "prompt_sha256": "abc", "stdout_sha256": "def"}
    two = {"run_id": "2", "prompt_sha256": "ghi", "stdout_sha256": "jkl"}
    agent_run.append_journal(path, one)
    agent_run.append_journal(path, two)
    assert [json.loads(line)["run_id"] for line in path.read_text().splitlines()] == [
        "1",
        "2",
    ]
    assert path.stat().st_mode & 0o777 == 0o600


def test_checkpoint_requires_existing_open_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ledger = tmp_path / ".agent-ledger" / "demo.jsonl"
    ledger.parent.mkdir()
    event_id = "evt-demo-human"
    rows = [
        ledger_row(event_id),
        ledger_row(
            "evt-claim",
            from_seat="codex-landing",
            to_seat="codex-landing",
            decided=[f"claimed:{event_id} — work"],
        ),
    ]
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    assert (
        agent_run.validate_checkpoint("demo", event_id, "codex-landing")["owner"]
        == "codex-landing"
    )
    with pytest.raises(agent_run.ProviderRunError, match="does not match"):
        agent_run.validate_checkpoint("demo", event_id, "claude-direction")


def test_checkpoint_rejects_malformed_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ledger = tmp_path / ".agent-ledger" / "demo.jsonl"
    ledger.parent.mkdir()
    ledger.write_text('{"event_id":"incomplete"}\n')
    with pytest.raises(agent_run.ProviderRunError, match="exact 10-field schema"):
        agent_run.validate_checkpoint("demo", "incomplete", "codex-landing")


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                ledger_row("evt-demo"),
                ledger_row(
                    "evt-prefix",
                    from_seat="codex-landing",
                    decided=["claimed:evt-demo-extra — wrong target"],
                ),
            ],
            "transition target is not a prior pending event",
        ),
        (
            [
                ledger_row("evt-demo"),
                ledger_row(
                    "evt-cross",
                    from_seat="codex-landing",
                    decided=["claimed:evt-demo — cross intent"],
                    intent_ref="docs/intents/other.md",
                ),
            ],
            "cross-intent transition target",
        ),
        (
            [
                ledger_row("evt-demo"),
                ledger_row(
                    "evt-close",
                    from_seat="codex-landing",
                    decided=["closed:evt-demo — done"],
                ),
                ledger_row(
                    "evt-reopen",
                    from_seat="codex-landing",
                    decided=["claimed:evt-demo — reopen"],
                ),
            ],
            "transition occurs after target closure",
        ),
        (
            [
                ledger_row("evt-demo"),
                ledger_row(
                    "evt-claim",
                    from_seat="codex-landing",
                    decided=["claimed:evt-demo — valid"],
                ),
                ledger_row(
                    "evt-target-transition",
                    from_seat="codex-landing",
                    decided=["claimed:evt-claim — invalid"],
                ),
            ],
            "transition target is not a prior pending event",
        ),
        (
            [ledger_row("evt-demo", taint="false")],
            "taint must be true/false",
        ),
        (
            [
                ledger_row("evt-demo"),
                ledger_row(
                    "evt-multi",
                    from_seat="codex-landing",
                    decided=["claimed:evt-demo", "closed:evt-demo"],
                ),
            ],
            "multiple transition markers",
        ),
        (
            [
                ledger_row("evt-demo"),
                ledger_row(
                    "evt-bad-marker",
                    from_seat="codex-landing",
                    decided=["claimed:evt demo"],
                ),
            ],
            "malformed transition marker",
        ),
    ],
)
def test_checkpoint_rejects_invalid_transition_history(
    tmp_path,
    monkeypatch,
    rows,
    message,
):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ledger = tmp_path / ".agent-ledger" / "demo.jsonl"
    ledger.parent.mkdir()
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(agent_run.ProviderRunError, match=message):
        agent_run.validate_checkpoint("demo", "evt-demo", "codex-landing")


def test_ledger_fold_reports_non_object_without_crashing(monkeypatch, capsys):
    monkeypatch.setattr(agent_ledger, "load", lambda _slug: ["corrupt-row"])
    args = type("Args", (), {"slug": "demo"})()
    agent_ledger.cmd_fold(args)
    output = capsys.readouterr().out
    assert "VIOLATION line 1 (?)" in output
    assert "no open events" in output


def test_skill_evidence_contains_digests_not_paths():
    selection = {
        "manifest_sha256": "m",
        "available_count": 1,
        "chosen": [
            {
                "name": "codebase-design",
                "digest": "d",
                "source_group": "mattpocock-skills",
                "call_policy": "auto-eligible",
                "selection_source": "auto",
            }
        ],
        "deferred": [],
    }
    evidence = agent_run.sanitized_skill_evidence(selection)
    raw = json.dumps(evidence)
    assert "codebase-design" in raw
    assert "/Users/" not in raw
    assert evidence["read_or_invoked"] == [
        {"name": "codebase-design", "status": "unknown"}
    ]


def test_augment_prompt_hashes_exact_skill_bytes(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: demo\n---\nbody\n")
    selection = {
        "chosen": [{"name": "demo", "digest": "tree-digest"}],
        "entries": {"demo": {"skill_md": str(skill)}},
        "trusted_content_roots": [str(tmp_path)],
    }
    delivered = agent_run.augment_prompt("task", selection, 10_000)
    expected = agent_run.sha256_bytes(skill.read_bytes())
    assert selection["chosen"][0]["content_sha256"] == expected
    assert f'content_sha256="{expected}"' in delivered


def test_augment_prompt_rejects_skill_outside_trusted_roots(tmp_path):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    selection = {
        "chosen": [{"name": "bad", "digest": "tree"}],
        "entries": {"bad": {"skill_md": str(outside)}},
        "trusted_content_roots": [str(trusted)],
    }
    with pytest.raises(agent_run.ProviderRunError, match="outside trusted roots"):
        agent_run.augment_prompt("task", selection, 10_000)


def test_high_cost_auto_candidate_is_deferred(tmp_path, monkeypatch):
    skill = tmp_path / "research" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: research\n---\n")
    manifest = tmp_path / "skills.json"
    manifest.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "name": "research",
                        "runtime": "codex",
                        "frontmatter_ok": True,
                        "skill_md": str(skill),
                        "tree_hash": "d",
                        "source_group": "local",
                        "call_policy": "suggest-confirm",
                    }
                ]
            }
        )
    )
    config = {
        "skills": {
            "manifest": str(manifest),
            "router_hook": "unused",
            "auto_select_policies": ["auto-eligible", "router"],
        }
    }
    monkeypatch.setattr(
        agent_run, "auto_skill_names", lambda *_args: (["research"], "ok")
    )
    selected = agent_run.select_skills("research this", tmp_path, ["auto"], config)
    assert selected["chosen"] == []
    assert [row["name"] for row in selected["deferred"]] == ["research"]


def test_risk_overlay_fails_closed_for_non_restricted_route():
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    args = type("Args", (), {"risk_trigger": ["money"]})()
    with pytest.raises(agent_run.ProviderRunError, match="restricted_zone"):
        agent_run.validate_risk_overlay(
            args, "standard_feature", "medium", "not-applicable", data
        )
    applied = agent_run.validate_risk_overlay(
        args, "restricted_zone", "high", "not-applicable", data
    )
    assert applied["required_review_effort_floor"] == "xhigh"


def test_status_rejects_repo_path_traversal(capsys):
    code = agent_run.main(
        [
            "--manifest",
            str(ROOT / "agent-providers.yaml"),
            "status",
            "--repo",
            "../secrets",
        ]
    )
    assert code == 2
    assert "project slug" in capsys.readouterr().err


def test_ibom_rejects_repo_path_traversal(capsys):
    code = agent_run.main(
        [
            "--manifest",
            str(ROOT / "agent-providers.yaml"),
            "ibom",
            "--repo",
            "../secrets",
        ]
    )
    assert code == 2
    assert "project slug" in capsys.readouterr().err


def test_parse_session_reads_codex_and_claude_model_from_jsonl(tmp_path):
    codex = tmp_path / "rollout-00000000-0000-4000-8000-000000000002.jsonl"
    codex.write_text(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-terra", "turn_id": "t1"},
            }
        )
        + "\n"
    )
    parsed = agent_run.parse_session("codex", codex)
    assert parsed["model_observed"] == "gpt-5.6-terra"
    assert parsed["model_observation_reason"] == "codex-jsonl-turn-context"
    assert parsed["session_id"] == "00000000-0000-4000-8000-000000000002"

    claude = tmp_path / "abc.jsonl"
    claude.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"model": "claude-opus-4-8", "content": []},
            }
        )
        + "\n"
    )
    parsed = agent_run.parse_session("claude", claude)
    assert parsed["model_observed"] == "claude-opus-4-8"
    assert parsed["model_observation_reason"] == "claude-jsonl-assistant-message"


def test_parse_session_keeps_unknown_when_model_absent(tmp_path):
    missing = tmp_path / "empty.jsonl"
    missing.write_text(json.dumps({"type": "turn.started"}) + "\n")
    parsed = agent_run.parse_session("codex", missing)
    assert parsed["model_observed"] == "unknown"
    assert parsed["model_observation_reason"] == "codex-jsonl-model-missing"


def test_classify_route_status_ready_degraded_blocked_disabled():
    assert agent_run.classify_route_status([]) == "ready"
    assert (
        agent_run.classify_route_status(
            [{"code": "live-evidence-unverified", "detail": "no-live-evidence"}]
        )
        == "degraded"
    )
    assert (
        agent_run.classify_route_status(
            [{"code": "live-quota-exhausted", "detail": "cooldown:unknown"}]
        )
        == "blocked"
    )
    assert (
        agent_run.classify_route_status(
            [
                {"code": "route-policy-disabled", "detail": "x"},
                {"code": "live-evidence-unverified", "detail": "y"},
            ]
        )
        == "disabled"
    )


def test_classify_failure_uses_timeout_class():
    assert agent_run.classify_failure("timed-out", 124, "") == "timeout"
    assert (
        agent_run.classify_failure(
            "timed-out", 124, "", timeout_class="timeout_first_event"
        )
        == "timeout_first_event"
    )


def test_run_codex_json_process_classifies_first_event_timeout(monkeypatch):
    class _FakeStream:
        def readline(self):
            return ""

        def read(self):
            return ""

    class FakeProc:
        def __init__(self):
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.returncode = None

        def poll(self):
            return None

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

    monkeypatch.setattr(agent_run.subprocess, "Popen", lambda *a, **k: FakeProc())

    class FakeSelector:
        def register(self, *a, **k):
            return None

        def unregister(self, *a, **k):
            return None

        def select(self, timeout=None):
            return []

    import selectors as _selectors

    monkeypatch.setattr(_selectors, "DefaultSelector", FakeSelector)

    proc, status, telemetry, events = agent_run.run_codex_json_process(
        ["codex", "exec", "--json", "hi"],
        cwd=Path("."),
        env={},
        timeout_seconds=5,
        first_event_seconds=0,
        idle_seconds=30,
    )
    assert status == "timed-out"
    assert telemetry["timeout_class"] == "timeout_first_event"
    assert proc.returncode == 124
    assert events == []


def test_extract_codex_agent_message_from_events():
    text = agent_run.extract_codex_agent_message(
        [
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "OK"},
            },
            {"type": "turn.completed", "usage": {}},
        ]
    )
    assert text == "OK"


def test_codex_command_template_includes_json():
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    cmd = agent_run.build_command(
        data["providers"]["codex"],
        "read-only",
        Path("/bin/codex"),
        Path("/tmp"),
        "hello",
        "gpt-5.6-terra",
        "low",
    )
    assert cmd[1] == "exec"
    assert cmd[2] == "--json"


def test_run_codex_json_process_classifies_idle_timeout(monkeypatch):
    class _QueueStream:
        def __init__(self, lines):
            self._lines = list(lines)

        def readline(self):
            if self._lines:
                return self._lines.pop(0)
            return ""

        def read(self):
            return ""

    class FakeProc:
        def __init__(self, stdout_lines):
            self.stdout = _QueueStream(stdout_lines)
            self.stderr = _QueueStream([])
            self.returncode = None

        def poll(self):
            return None

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

    event_line = (
        json.dumps({"type": "turn.started"}) + "\n"
    )

    def popen_idle(*_a, **_k):
        return FakeProc([event_line])

    monkeypatch.setattr(agent_run.subprocess, "Popen", popen_idle)

    class RecordingSelector:
        def __init__(self):
            self._streams = []

        def register(self, stream, _mask, label):
            self._streams.append((stream, label))

        def unregister(self, _stream):
            return None

        def select(self, timeout=None):
            # Deliver one stdout event, then idle.
            for stream, label in list(self._streams):
                if label == "stdout" and getattr(stream, "_lines", None):
                    return [((type("K", (), {"fileobj": stream, "data": label})()), None)]
            return []

    import selectors as _selectors

    monkeypatch.setattr(_selectors, "DefaultSelector", RecordingSelector)

    _proc, status, telemetry, events = agent_run.run_codex_json_process(
        ["codex", "exec", "--json", "hi"],
        cwd=Path("."),
        env={},
        timeout_seconds=30,
        first_event_seconds=10,
        idle_seconds=0,
    )
    assert status == "timed-out"
    assert telemetry["timeout_class"] == "timeout_idle"
    assert events and events[0]["type"] == "turn.started"

def test_run_codex_json_process_classifies_total_timeout(monkeypatch):
    class _FakeStream:
        def readline(self):
            return ""

        def read(self):
            return ""

    class FakeProc:
        def __init__(self):
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.returncode = None

        def poll(self):
            return None

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

    monkeypatch.setattr(agent_run.subprocess, "Popen", lambda *a, **k: FakeProc())

    class FakeSelector:
        def register(self, *a, **k):
            return None

        def unregister(self, *a, **k):
            return None

        def select(self, timeout=None):
            return []

    import selectors as _selectors

    monkeypatch.setattr(_selectors, "DefaultSelector", FakeSelector)
    # No events; first/idle budgets are high so the total deadline wins.
    _proc, status, telemetry, events = agent_run.run_codex_json_process(
        ["codex", "exec", "--json", "hi"],
        cwd=Path("."),
        env={},
        timeout_seconds=1,
        first_event_seconds=100,
        idle_seconds=100,
    )
    assert status == "timed-out"
    assert telemetry["timeout_class"] == "timeout_total"
    assert events == []




def test_run_codex_json_process_spawn_failure_is_not_timeout(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("codex binary missing")

    monkeypatch.setattr(agent_run.subprocess, "Popen", boom)
    proc, status, telemetry, events = agent_run.run_codex_json_process(
        ["codex", "exec", "--json", "hi"],
        cwd=Path("."),
        env={},
        timeout_seconds=5,
    )
    assert status == "provider-start-failed"
    assert telemetry["timeout_class"] is None
    assert proc.returncode == 127
    assert "provider-start-failed" in proc.stderr
    assert events == []
    assert (
        agent_run.classify_failure(status, proc.returncode, proc.stderr)
        == "provider-start-failed"
    )


def test_run_codex_json_process_success_extracts_message_and_telemetry(monkeypatch):
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n",
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "OK"},
            }
        )
        + "\n",
        json.dumps({"type": "turn.completed", "usage": {}}) + "\n",
    ]

    class _QueueStream:
        def __init__(self, queued):
            self._lines = list(queued)

        def readline(self):
            return self._lines.pop(0) if self._lines else ""

        def read(self):
            return ""

    class FakeProc:
        def __init__(self):
            self.stdout = _QueueStream(lines)
            self.stderr = _QueueStream([])
            self.returncode = None

        def poll(self):
            return 0 if not self.stdout._lines else None

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    observed = {}

    def fake_popen(*args, **kwargs):
        observed["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(agent_run.subprocess, "Popen", fake_popen)

    class RecordingSelector:
        def __init__(self):
            self._streams = []

        def register(self, stream, _mask, label):
            self._streams.append((stream, label))

        def unregister(self, stream):
            self._streams = [(s, l) for s, l in self._streams if s is not stream]

        def select(self, timeout=None):
            for stream, label in list(self._streams):
                if label == "stdout" and stream._lines:
                    return [((type("K", (), {"fileobj": stream, "data": label})()), None)]
            return []

    import selectors as _selectors

    monkeypatch.setattr(_selectors, "DefaultSelector", RecordingSelector)
    proc, status, telemetry, events = agent_run.run_codex_json_process(
        ["codex", "exec", "--json", "hi"],
        cwd=Path("."),
        env={},
        timeout_seconds=10,
    )
    assert status == "completed"
    assert proc.returncode == 0
    assert proc.stdout.strip() == "OK"
    assert telemetry["turn_completed_at"]
    assert telemetry["first_provider_event_at"]
    assert telemetry["provider_event_count"] >= 3
    assert observed["kwargs"]["start_new_session"] is True
    assert agent_run.extract_codex_model_from_events(
        [{"type": "x", "model": "gpt-5.6-sol"}]
    ) == "gpt-5.6-sol"


def test_doctor_task_focus_and_reviewer_graph_gaps(tmp_path, monkeypatch):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path)
    monkeypatch.setattr(
        agent_run, "resolve_binary", lambda _provider: Path("/bin/echo")
    )
    monkeypatch.setattr(
        agent_run, "binary_version", lambda _binary, _provider: "test-cli"
    )

    def fake_catalog(provider, _binary):
        return {
            "status": "static-config",
            "models": [{"id": model} for model in provider.get("model_options", [])],
        }

    monkeypatch.setattr(agent_run, "discover_provider_models", fake_catalog)
    report = agent_run.build_route_doctor(data, route_name="judgment", repo="demo")
    assert report["task_focus"]["task_shape"] == "judgment"
    assert report["task_focus"]["required_routes"] == ["judgment"]
    assert "fable_final_review" in report["task_focus"]["optional_or_disabled_routes"]
    assert report["routes"][0]["status"] == "degraded"
    assert isinstance(report["reviewer_graph_gaps"], dict)
    assert "anthropic" in report["reviewer_graph_gaps"]


def test_no_skills_does_not_require_local_skill_manifest(tmp_path, monkeypatch, capsys):
    data = agent_run.load_manifest(ROOT / "agent-providers.yaml")
    data["journal"]["root"] = str(tmp_path / "journal")
    data["skills"]["manifest"] = str(tmp_path / "missing-skills-manifest.json")
    monkeypatch.setattr(
        agent_run, "resolve_binary", lambda _provider: Path("/bin/echo")
    )
    monkeypatch.setattr(
        agent_run, "binary_version", lambda _binary, _provider: "test-cli"
    )
    monkeypatch.setattr(agent_run, "session_snapshot", lambda _provider: {})
    monkeypatch.setattr(
        agent_run,
        "discover_provider_models",
        lambda _provider, _binary: {
            "status": "static-config",
            "models": [{"id": "gpt-5.6-terra"}],
        },
    )
    monkeypatch.setattr(
        agent_run.subprocess,
        "run",
        lambda *a, **k: type(
            "R",
            (),
            {"returncode": 0, "stdout": "ok", "stderr": ""},
        )(),
    )
    args = SimpleNamespace(
        provider="codex",
        task_shape=None,
        model="gpt-5.6-terra",
        effort="medium",
        seat="codex-landing",
        producer_provider=None,
        producer_run_id=None,
        checkpoint_event=None,
        risk_trigger=[],
        cwd=str(tmp_path),
        mode="read-only",
        allow_write=False,
        skill=["auto"],
        show_stderr=False,
        no_provider_tools=False,
        no_skills=True,
        timeout_seconds=10,
        minimal_runtime=False,
        trust_workspace=False,
        prompt="ci no-skills path",
    )
    assert agent_run.run_provider(args, data) == 0
    journals = list((tmp_path / "journal").glob("*.jsonl"))
    assert len(journals) == 1, journals
    row = json.loads(journals[0].read_text().strip().splitlines()[-1])
    assert row["skill_evidence"]["routing_status"] == "explicitly-disabled-for-run"
