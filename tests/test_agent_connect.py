"""O11 prototype: coding-agent connector registry, login probe and login guide.

Every probe here runs a real subprocess against a fake CLI written into
``tmp_path``; nothing touches the operator's real codex / kimi login.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agent_connect.py"
FAKE_SECRET = "sk-FAKESECRET-DO-NOT-ECHO-1234567890"


def load_module():
    spec = importlib.util.spec_from_file_location("agent_connect_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ac = load_module()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def write_cli(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def fake_codex(tmp_path: Path, status_out: str, status_exit: int) -> Path:
    body = f"""
if [ "$1" = "login" ] && [ "$2" = "status" ]; then
  printf '%s\\n' "{status_out}"
  exit {status_exit}
fi
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  echo "Run Codex non-interactively"; exit 0
fi
if [ "$1" = "login" ] && [ "$2" = "--help" ]; then
  echo "      --device-auth"; exit 0
fi
echo "unexpected: $*" >&2
exit 64
"""
    return write_cli(tmp_path / "codex", body)


def fake_kimi(tmp_path: Path, list_out: str, list_exit: int = 0) -> Path:
    body = f"""
if [ "$1" = "provider" ] && [ "$2" = "list" ]; then
  printf '%s\\n' "{list_out}"
  exit {list_exit}
fi
if [ "$1" = "acp" ] && [ "$2" = "--help" ]; then
  echo "Run kimi-code as an Agent Client Protocol (ACP) server over stdio."; exit 0
fi
if [ "$1" = "login" ] && [ "$2" = "--help" ]; then
  echo "Authenticate with Kimi Code CLI via the device-code flow."; exit 0
fi
if [ "$1" = "--help" ]; then
  echo "  -p, --prompt <prompt>  Run one prompt non-interactively"; exit 0
fi
echo "unexpected: $*" >&2
exit 64
"""
    return write_cli(tmp_path / "kimi", body)


def providers_manifest(tmp_path: Path, codex_bin: Path) -> Path:
    data = {
        "version": 1,
        "providers": {
            "codex": {
                "display_name": "Codex CLI",
                "family": "openai",
                "binary_candidates": [str(codex_bin)],
                "billing_policy": "existing-subscription-login-only",
                "strip_environment": ["OPENAI_API_KEY", "OPENAI_BASE_URL"],
            }
        },
    }
    path = tmp_path / "agent-providers.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def connectors_data(kimi_bin: Path) -> dict:
    """The real manifest, with kimi's binary pointed at the fake CLI."""

    data = yaml.safe_load((ROOT / "agent-connectors.yaml").read_text())
    data["connectors"]["kimi"]["binary_candidates"] = [str(kimi_bin)]
    return data


def write_connectors(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "agent-connectors.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path


@pytest.fixture()
def world(tmp_path):
    """Both CLIs installed and signed in with a subscription."""

    codex = fake_codex(tmp_path, "Logged in using ChatGPT", 0)
    kimi = fake_kimi(tmp_path, "managed:kimi-code  type=kimi  models=4  source=oauth")
    providers = providers_manifest(tmp_path, codex)
    connectors = write_connectors(tmp_path, connectors_data(kimi))
    return {"tmp": tmp_path, "providers": providers, "connectors": connectors}


def run_cli(world, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--manifest",
        str(world["connectors"]),
        "--providers-manifest",
        str(world["providers"]),
        *args,
    ]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, env=env or os.environ.copy()
    )


def status_of(world, connector: str) -> dict:
    manifest = ac.load_manifest(world["connectors"], world["providers"])
    return ac.connector_status(manifest, connector)


# --------------------------------------------------------------------------
# the shipped manifest itself
# --------------------------------------------------------------------------

def test_shipped_manifest_validates_against_shipped_providers():
    manifest = ac.load_manifest(ROOT / "agent-connectors.yaml", ROOT / "agent-providers.yaml")
    assert set(manifest["connectors"]) == {"codex", "kimi"}
    # codex reuses the dispatch canon instead of re-declaring its binary.
    assert manifest["connectors"]["codex"]["provider_ref"] == "codex"
    assert "binary_candidates" not in ac.raw_connector(ROOT / "agent-connectors.yaml", "codex")


# --------------------------------------------------------------------------
# login classification (positive)
# --------------------------------------------------------------------------

def test_both_logged_in_with_subscription_is_login_configured(world):
    for name in ("codex", "kimi"):
        result = status_of(world, name)
        assert result["login_verdict"] == "login_configured", result
        checks = {c["check"]: c for c in result["checks"]}
        assert checks["binary"]["result"] == "pass"
        assert checks["login"]["result"] == "pass"
        assert checks["billing_policy"]["result"] == "pass"
        # A configured login is not a proven working turn: never claimed.
        assert checks["live_turn"]["result"] == "not_checked"


def test_login_evidence_strength_is_reported_honestly(world):
    codex = {c["check"]: c for c in status_of(world, "codex")["checks"]}
    kimi = {c["check"]: c for c in status_of(world, "kimi")["checks"]}
    assert codex["login"]["evidence"] == "official-status-command"
    # kimi has no status subcommand; provider list only proves configuration.
    assert kimi["login"]["evidence"] == "config-inference"


def test_login_only_exit_zero_only_when_every_connector_is_configured(world):
    proc = run_cli(world, "status", "--login-only", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["scope"] == "login-only"
    assert [c["connector"] for c in payload["connectors"]] == ["codex", "kimi"]
    assert all(c["login_verdict"] == "login_configured" for c in payload["connectors"])


# --------------------------------------------------------------------------
# overall readiness: every applicable check must pass (no skip-means-pass)
# --------------------------------------------------------------------------

def test_full_status_is_nonzero_while_live_turn_is_not_checked(world):
    # Login and every declared capability pass, but no real turn was run:
    # the overall status must not report success.
    proc = run_cli(world, "status", "--json")
    assert proc.returncode == 1, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["scope"] == "full"
    for c in payload["connectors"]:
        assert c["login_verdict"] == "login_configured"
        assert c["verdict"] == "live_turn_not_checked"


def test_capability_probe_failure_blocks_overall_status(world):
    fake = world["tmp"] / "codex"
    fake.write_text(fake.read_text().replace("Run Codex non-interactively", "something else"))
    result = status_of(world, "codex")
    caps = {c["name"]: c for c in result["capabilities"]}
    assert caps["headless_prompt"]["result"] == "fail"
    assert result["login_verdict"] == "login_configured"
    assert result["verdict"] == "capability_failed"
    assert run_cli(world, "status", "--connector", "codex").returncode == 1


def test_capability_probe_timeout_blocks_overall_status(world, monkeypatch):
    fake = world["tmp"] / "kimi"
    fake.write_text(
        fake.read_text().replace(
            'if [ "$1" = "acp" ] && [ "$2" = "--help" ]; then',
            'if [ "$1" = "acp" ] && [ "$2" = "--help" ]; then\n  sleep 5',
        )
    )
    monkeypatch.setattr(ac, "PROBE_TIMEOUT_CAP_SECONDS", 1)
    result = status_of(world, "kimi")
    caps = {c["name"]: c for c in result["capabilities"]}
    assert caps["acp_native"]["result"] == "not_checked"
    assert result["verdict"] == "capability_not_checked"


def test_login_only_does_not_run_capability_probes(world, tmp_path):
    marker = tmp_path / "cap-probe-ran"
    fake = world["tmp"] / "codex"
    fake.write_text(
        fake.read_text().replace(
            'if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then',
            f'if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then\n  touch "{marker}"',
        )
    )
    proc = run_cli(world, "status", "--login-only", "--connector", "codex", "--json")
    assert proc.returncode == 0, proc.stdout
    assert not marker.exists()
    row = json.loads(proc.stdout)["connectors"][0]
    assert all(c["result"] == "not_checked" for c in row["capabilities"] if c["declared"])
    # The overall verdict stays honest even in login-only scope.
    assert row["verdict"] == "capability_not_checked"


def test_login_only_still_fails_on_policy_mismatch(world):
    fake_codex(world["tmp"], "Logged in using an API key - sk-***", 0)
    assert run_cli(world, "status", "--login-only", "--connector", "codex").returncode == 1


# --------------------------------------------------------------------------
# negatives: not logged in / not installed / unknown output / timeout
# --------------------------------------------------------------------------

def test_codex_not_logged_in_needs_login_and_prints_guide(world):
    fake_codex(world["tmp"], "Not logged in", 1)
    assert status_of(world, "codex")["verdict"] == "needs_login"
    proc = run_cli(world, "status", "--connector", "codex")
    assert proc.returncode == 1
    assert "codex login" in proc.stdout
    assert "--device-auth" in proc.stdout


def test_kimi_no_providers_needs_login_and_prints_guide(world):
    fake_kimi(world["tmp"], "No providers configured.")
    assert status_of(world, "kimi")["verdict"] == "needs_login"
    proc = run_cli(world, "status", "--connector", "kimi")
    assert proc.returncode == 1
    assert "kimi login --region mainland-cn" in proc.stdout


def test_missing_binary_needs_install_and_never_probes_login(world):
    (world["tmp"] / "kimi").unlink()
    result = status_of(world, "kimi")
    assert result["verdict"] == "needs_install"
    checks = {c["check"]: c for c in result["checks"]}
    assert checks["binary"]["result"] == "fail"
    assert checks["login"]["result"] == "not_checked"


def test_unrecognised_output_is_unknown_not_logged_in(world):
    fake_codex(world["tmp"], "Something new from a future CLI", 0)
    result = status_of(world, "codex")
    checks = {c["check"]: c for c in result["checks"]}
    assert checks["login"]["result"] == "not_checked"
    assert result["verdict"] == "unknown"
    assert run_cli(world, "status", "--connector", "codex").returncode == 1


def test_exit_code_must_match_rule_not_just_text(world):
    # "Logged in using ChatGPT" but the CLI failed: not evidence of a login.
    fake_codex(world["tmp"], "Logged in using ChatGPT", 3)
    assert status_of(world, "codex")["verdict"] == "unknown"


def test_probe_timeout_is_unknown(world, monkeypatch):
    write_cli(world["tmp"] / "codex", "sleep 5\n")
    monkeypatch.setattr(ac, "PROBE_TIMEOUT_CAP_SECONDS", 1)
    result = status_of(world, "codex")
    checks = {c["check"]: c for c in result["checks"]}
    assert checks["login"]["result"] == "not_checked"
    assert "timeout" in checks["login"]["detail"]
    assert result["verdict"] == "unknown"


def test_api_key_login_violates_subscription_only_policy(world):
    fake_codex(world["tmp"], f"Logged in using an API key - {FAKE_SECRET[:8]}***67890", 0)
    result = status_of(world, "codex")
    checks = {c["check"]: c for c in result["checks"]}
    assert checks["login"]["result"] == "pass"
    assert checks["billing_policy"]["result"] == "fail"
    assert result["verdict"] == "policy_mismatch"


def test_kimi_non_oauth_provider_violates_subscription_only_policy(world):
    fake_kimi(world["tmp"], "custom:moonshot  type=openai  models=2  source=inline")
    assert status_of(world, "kimi")["verdict"] == "policy_mismatch"


def test_kimi_api_json_provider_violates_subscription_only_policy(world):
    fake_kimi(world["tmp"], "custom:x  type=openai  models=1  source=apiJson(https://example.invalid/api.json)")
    assert status_of(world, "kimi")["verdict"] == "policy_mismatch"


def _kimi_login(world) -> dict:
    return {c["check"]: c for c in status_of(world, "kimi")["checks"]}


def test_kimi_mixed_oauth_and_api_key_providers_fail_closed(world):
    # provider list prints one line per configured provider; an OAuth one
    # existing does not prove the OAuth one is what gets used.
    fake_kimi(
        world["tmp"],
        "custom:moonshot  type=openai  models=2  source=inline\n"
        "managed:kimi-code  type=kimi  models=4  source=oauth\n"
        "\n"
        "Default model: custom:moonshot/kimi-k2",
    )
    checks = _kimi_login(world)
    assert checks["login"]["method"] == "mixed"
    assert checks["billing_policy"]["result"] == "fail"
    assert status_of(world, "kimi")["verdict"] == "policy_mismatch"


def test_kimi_mixed_order_reversed_also_fails_closed(world):
    fake_kimi(
        world["tmp"],
        "managed:kimi-code  type=kimi  models=4  source=oauth\n"
        "custom:moonshot  type=openai  models=2  source=inline",
    )
    assert status_of(world, "kimi")["verdict"] == "policy_mismatch"


def test_kimi_only_oauth_providers_with_default_model_line_pass(world):
    fake_kimi(
        world["tmp"],
        "managed:kimi-code  type=kimi  models=4  source=oauth\n"
        "managed:kimi-code-global  type=kimi  models=4  source=oauth\n"
        "\n"
        "Default model: managed:kimi-code/kimi-for-coding",
    )
    checks = _kimi_login(world)
    assert checks["login"]["result"] == "pass"
    assert checks["login"]["method"] == "subscription"
    assert checks["billing_policy"]["result"] == "pass"


def test_kimi_oauth_lookalike_source_is_not_subscription(world):
    fake_kimi(world["tmp"], "managed:x  type=kimi  models=1  source=oauth-proxy")
    assert _kimi_login(world)["login"].get("method") != "subscription"
    assert status_of(world, "kimi")["verdict"] != "live_turn_not_checked"


def test_kimi_unrecognised_extra_line_is_unknown(world):
    fake_kimi(
        world["tmp"],
        "managed:kimi-code  type=kimi  models=4  source=oauth\n"
        "some future line we do not understand",
    )
    checks = _kimi_login(world)
    assert checks["login"]["result"] == "not_checked"
    assert status_of(world, "kimi")["verdict"] == "unknown"


def test_kimi_providers_plus_no_providers_text_is_unknown(world):
    fake_kimi(
        world["tmp"],
        "managed:kimi-code  type=kimi  models=4  source=oauth\nNo providers configured.",
    )
    assert status_of(world, "kimi")["verdict"] == "unknown"


# --------------------------------------------------------------------------
# secrecy: raw probe output never leaves the process
# --------------------------------------------------------------------------

def test_raw_probe_output_is_never_echoed(world):
    fake_codex(world["tmp"], f"Logged in using an API key - {FAKE_SECRET}", 0)
    fake_kimi(world["tmp"], f"weird {FAKE_SECRET}")
    for args in (("status",), ("status", "--json")):
        proc = run_cli(world, *args)
        assert FAKE_SECRET not in proc.stdout
        assert FAKE_SECRET not in proc.stderr
        assert "FAKESECRET" not in proc.stdout + proc.stderr


def test_provider_api_key_env_is_stripped_from_probe(world):
    # A CLI that would report "logged in" purely because an API key sits in
    # the environment must not count: strip_environment applies to probes.
    write_cli(
        world["tmp"] / "codex",
        'if [ -n "$OPENAI_API_KEY" ]; then echo "Logged in using ChatGPT"; exit 0; fi\n'
        'echo "Not logged in"; exit 1\n',
    )
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = FAKE_SECRET
    proc = run_cli(world, "status", "--connector", "codex", "--json", env=env)
    payload = json.loads(proc.stdout)
    assert payload["connectors"][0]["verdict"] == "needs_login"


def test_guide_never_executes_the_cli(world, tmp_path):
    marker = tmp_path / "ran"
    write_cli(world["tmp"] / "codex", f'touch "{marker}"; exit 0\n')
    proc = run_cli(world, "guide", "codex")
    assert proc.returncode == 0
    assert "codex login" in proc.stdout
    assert not marker.exists()


# --------------------------------------------------------------------------
# capabilities: declared true must be probe-verified (no over-claiming)
# --------------------------------------------------------------------------

def test_declared_capabilities_are_verified_by_probe(world):
    kimi = {c["name"]: c for c in status_of(world, "kimi")["capabilities"]}
    assert kimi["acp_native"]["declared"] is True
    assert kimi["acp_native"]["result"] == "pass"
    codex = {c["name"]: c for c in status_of(world, "codex")["capabilities"]}
    assert codex["acp_native"]["declared"] is False
    assert codex["acp_native"]["result"] == "not_applicable"


def test_declared_capability_whose_probe_fails_is_reported_fail(world):
    fake = world["tmp"] / "kimi"
    fake.write_text(fake.read_text().replace("Agent Client Protocol", "something else"))
    kimi = {c["name"]: c for c in status_of(world, "kimi")["capabilities"]}
    assert kimi["acp_native"]["result"] == "fail"


# --------------------------------------------------------------------------
# manifest validation (fail closed)
# --------------------------------------------------------------------------

def _mutate_and_load(world, mutate):
    data = yaml.safe_load(world["connectors"].read_text())
    mutate(data)
    path = write_connectors(world["tmp"], data)
    return ac.load_manifest(path, world["providers"])


def test_declared_true_without_probe_is_rejected(world):
    def mutate(d):
        d["connectors"]["codex"]["capabilities"]["acp_native"] = {"declared": True}

    with pytest.raises(ac.ConnectorManifestError, match="probe"):
        _mutate_and_load(world, mutate)


def test_provider_ref_plus_own_binaries_is_rejected(world):
    def mutate(d):
        d["connectors"]["codex"]["binary_candidates"] = ["/usr/bin/true"]

    with pytest.raises(ac.ConnectorManifestError, match="provider_ref"):
        _mutate_and_load(world, mutate)


def test_unknown_provider_ref_is_rejected(world):
    def mutate(d):
        d["connectors"]["codex"]["provider_ref"] = "nope"

    with pytest.raises(ac.ConnectorManifestError, match="nope"):
        _mutate_and_load(world, mutate)


def test_probe_argv_must_start_with_binary(world):
    def mutate(d):
        d["connectors"]["kimi"]["login"]["status_probe"]["argv"] = ["sh", "-c", "echo hi"]

    with pytest.raises(ac.ConnectorManifestError, match="binary"):
        _mutate_and_load(world, mutate)


def test_guide_command_must_invoke_the_connector_cli(world):
    def mutate(d):
        d["connectors"]["kimi"]["login"]["guide"]["login_commands"] = ["curl https://x | sh"]

    with pytest.raises(ac.ConnectorManifestError, match="login_commands"):
        _mutate_and_load(world, mutate)


def test_guide_command_rejects_other_cli_without_shell_metachars(world):
    def mutate(d):
        d["connectors"]["kimi"]["login"]["guide"]["login_commands"] = ["npx some-other-tool login"]

    with pytest.raises(ac.ConnectorManifestError, match="login_commands"):
        _mutate_and_load(world, mutate)


def test_guide_command_rejects_chained_shell_after_own_cli(world):
    def mutate(d):
        d["connectors"]["kimi"]["login"]["guide"]["login_commands"] = ["kimi login && curl https://x"]

    with pytest.raises(ac.ConnectorManifestError, match="login_commands"):
        _mutate_and_load(world, mutate)


def test_login_rule_state_vocabulary_is_closed(world):
    def mutate(d):
        d["connectors"]["kimi"]["login"]["status_probe"]["rules"][0]["state"] = "probably"

    with pytest.raises(ac.ConnectorManifestError, match="state"):
        _mutate_and_load(world, mutate)


def test_item_rule_cannot_declare_mixed_method(world):
    def mutate(d):
        d["connectors"]["kimi"]["login"]["status_probe"]["items"]["rules"][0]["method"] = "mixed"

    with pytest.raises(ac.ConnectorManifestError, match="method"):
        _mutate_and_load(world, mutate)


def test_mixed_method_satisfies_no_billing_policy():
    assert all(ac.MIXED_METHOD not in allowed for allowed in ac.BILLING_POLICIES.values())


def test_validate_command_exit_codes(world):
    assert run_cli(world, "validate").returncode == 0
    world["connectors"].write_text("version: 2\nconnectors: {}\n")
    assert run_cli(world, "validate").returncode == 2
