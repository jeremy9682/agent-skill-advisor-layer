#!/usr/bin/env python3
"""O11 prototype: coding-agent connector registry, login probe and login guide.

Internal ops tool (founder Q44 / Q46). Three commands:

* ``validate`` -- load ``agent-connectors.yaml`` and fail closed on any rule
  violation (exit 2).
* ``status`` -- for each connector: is the vendor CLI installed, is a login
  configured, does the login method match the billing policy, do the
  declared capabilities hold, and does a real turn work. Every check is
  three-state (``pass`` / ``fail`` / ``not_checked``); ``not_checked`` never
  counts as success. Two layers are reported separately:

  - ``login_verdict`` -- binary + login + billing policy only;
  - ``verdict`` -- overall readiness: the login layer, then every capability
    declared true, then ``live_turn``. Only ``ready`` is success.

  Exit 0 only when every requested connector is ``ready``. This prototype
  never runs a real turn, so plain ``status`` always exits non-zero
  (``live_turn_not_checked``) -- by design, it is not a readiness gate yet.
  ``status --login-only`` is the explicit, narrower question "is a login
  configured that matches the billing policy": it skips capability probes
  and exits 0 when every ``login_verdict`` is ``login_configured``.
  Connectors whose login is not configured get their login guide printed.
* ``guide <connector>`` -- print the login / sign-up guide. Never runs the CLI.

Safety properties (each has a test):

* only the vendor CLI is ever executed (probe argv must start with
  ``{binary}``), with stdin closed, a bounded timeout, and the provider's
  API-key environment variables stripped;
* raw probe output is classified in memory and never printed or returned
  (``codex login status`` prints a partial API key);
* nothing here runs a login, reads a credential file or stores a secret;
* a capability declared ``true`` must carry a probe, and the probe result is
  what gets reported (no over-claiming);
* "login configured" is not "usable": ``live_turn`` is always ``not_checked``
  in this prototype.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "agent-connectors.yaml"
DEFAULT_PROVIDERS = ROOT / "agent-providers.yaml"

PROBE_TIMEOUT_CAP_SECONDS = 60
BINARY_TOKEN = "{binary}"

LOGIN_STATES = frozenset({"logged_in", "not_logged_in"})
LOGIN_METHODS = frozenset({"subscription", "api_key", "api_key_or_custom"})
# Result-only method: several configured sources disagree. Never declarable in
# a rule and deliberately absent from every billing policy (fails closed).
MIXED_METHOD = "mixed"
EVIDENCE_KINDS = frozenset({"official-status-command", "config-inference"})
BILLING_POLICIES = {
    # policy -> login methods that satisfy it
    "existing-subscription-login-only": frozenset({"subscription"}),
    "existing-login-only": LOGIN_METHODS,
}
PROVIDER_OWNED_FIELDS = ("binary_candidates", "strip_environment", "billing_policy")

PASS, FAIL, NOT_CHECKED, NOT_APPLICABLE = "pass", "fail", "not_checked", "not_applicable"

RESULT_ZH = {PASS: "过", FAIL: "不过", NOT_CHECKED: "没检查", NOT_APPLICABLE: "不适用"}
VERDICT_ZH = {
    "login_configured": "登录已配置",
    "needs_install": "未安装",
    "needs_login": "未登录",
    "policy_mismatch": "登录方式不符合计费策略",
    "unknown": "无法判断",
    "capability_failed": "声明的能力探测不过",
    "capability_not_checked": "声明的能力没检查",
    "live_turn_not_checked": "真实对话没检查（不能算可用）",
    "ready": "可用",
}


class ConnectorManifestError(ValueError):
    pass


# --------------------------------------------------------------------------
# manifest loading and validation (fail closed)
# --------------------------------------------------------------------------

def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConnectorManifestError(f"cannot read {path}: {exc}") from exc


def raw_connector(path: Path, name: str) -> dict:
    return dict(_load_yaml(path)["connectors"][name])


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ConnectorManifestError(message)


def _check_argv(where: str, argv: Any) -> None:
    _require(
        isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv),
        f"{where}: argv must be a non-empty list of strings",
    )
    _require(argv[0] == BINARY_TOKEN, f"{where}: argv must start with {BINARY_TOKEN}")
    _require(
        all(BINARY_TOKEN not in a for a in argv[1:]),
        f"{where}: {BINARY_TOKEN} may only appear as argv[0]",
    )


def _check_timeout(where: str, value: Any) -> None:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0,
        f"{where}: timeout_seconds must be a positive number",
    )


def _check_pattern(where: str, pattern: Any) -> None:
    _require(isinstance(pattern, str) and pattern, f"{where}: pattern must be a non-empty string")
    try:
        re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise ConnectorManifestError(f"{where}: bad pattern: {exc}") from exc


def _validate_login(name: str, login: Any) -> None:
    _require(isinstance(login, dict), f"{name}.login must be a mapping")
    probe = login.get("status_probe")
    where = f"{name}.login.status_probe"
    _require(isinstance(probe, dict), f"{where} is required")
    _check_argv(where, probe.get("argv"))
    _check_timeout(where, probe.get("timeout_seconds"))
    _require(
        probe.get("evidence") in EVIDENCE_KINDS,
        f"{where}.evidence must be one of {sorted(EVIDENCE_KINDS)}",
    )
    items = probe.get("items")
    if items is not None:
        iw = f"{where}.items"
        _require(isinstance(items, dict), f"{iw} must be a mapping")
        code = items.get("exit_code")
        _require(isinstance(code, int) and not isinstance(code, bool), f"{iw}.exit_code must be an int")
        _check_pattern(f"{iw}.item_pattern", items.get("item_pattern"))
        record_fields = set(re.compile(items["item_pattern"]).groupindex)
        _require(
            bool(record_fields),
            f"{iw}.item_pattern must name the record fields with (?P<name>...) groups",
        )
        if "ignore_pattern" in items:
            _check_pattern(f"{iw}.ignore_pattern", items.get("ignore_pattern"))
        item_rules = items.get("rules")
        _require(
            isinstance(item_rules, list) and item_rules,
            f"{iw}.rules must be a non-empty list",
        )
        for i, rule in enumerate(item_rules):
            rw = f"{iw}.rules[{i}]"
            _require(isinstance(rule, dict), f"{rw} must be a mapping")
            _require(
                set(rule) == {"fields", "method"},
                f"{rw} must have exactly 'fields' and 'method' (whole-record field matching only)",
            )
            fields = rule["fields"]
            _require(
                isinstance(fields, dict) and fields,
                f"{rw}.fields must be a non-empty mapping of record field -> regex",
            )
            for field, pattern in fields.items():
                _require(
                    field in record_fields,
                    f"{rw}.fields.{field} is not a named field of item_pattern {sorted(record_fields)}",
                )
                _check_pattern(f"{rw}.fields.{field}", pattern)
            _require(
                rule.get("method") in LOGIN_METHODS,
                f"{rw}.method must be one of {sorted(LOGIN_METHODS)}",
            )
            if rule["method"] == "subscription":
                # A rule that grants subscription must pin the whole record
                # (provider id included), or any OAuth-looking record passes.
                missing = sorted(record_fields - set(fields))
                _require(
                    not missing,
                    f"{rw}: a subscription rule must pin every record field; missing {missing}",
                )
    rules = probe.get("rules")
    _require(isinstance(rules, list) and rules, f"{where}.rules must be a non-empty list")
    for i, rule in enumerate(rules):
        rw = f"{where}.rules[{i}]"
        _require(isinstance(rule, dict), f"{rw} must be a mapping")
        code = rule.get("exit_code")
        _require(isinstance(code, int) and not isinstance(code, bool), f"{rw}.exit_code must be an int")
        _check_pattern(rw, rule.get("pattern"))
        state = rule.get("state")
        _require(state in LOGIN_STATES, f"{rw}.state must be one of {sorted(LOGIN_STATES)}")
        if state == "logged_in":
            _require(
                rule.get("method") in LOGIN_METHODS,
                f"{rw}.method must be one of {sorted(LOGIN_METHODS)} for a logged_in state",
            )
        else:
            _require("method" not in rule, f"{rw}: a not_logged_in state carries no method")

    guide = login.get("guide")
    gw = f"{name}.login.guide"
    _require(isinstance(guide, dict), f"{gw} is required")
    commands = guide.get("login_commands")
    _require(
        isinstance(commands, list) and commands and all(isinstance(c, str) for c in commands),
        f"{gw}.login_commands must be a non-empty list of strings",
    )
    for command in commands:
        _require(
            command.split()[:1] == [name] and not any(ch in command for ch in "|;&`$><"),
            f"{gw}.login_commands must only invoke the {name} CLI itself: {command!r}",
        )
    steps = guide.get("steps")
    _require(
        isinstance(steps, list) and steps and all(isinstance(s, str) for s in steps),
        f"{gw}.steps must be a non-empty list of strings",
    )
    for key in ("signup_url", "signup_url_global"):
        if key in guide:
            _require(
                isinstance(guide[key], str) and guide[key].startswith("https://"),
                f"{gw}.{key} must be an https URL",
            )


def _validate_capabilities(name: str, caps: Any) -> None:
    _require(isinstance(caps, dict), f"{name}.capabilities must be a mapping")
    for cap, spec in caps.items():
        where = f"{name}.capabilities.{cap}"
        _require(isinstance(spec, dict), f"{where} must be a mapping")
        declared = spec.get("declared")
        _require(isinstance(declared, bool), f"{where}.declared must be true or false")
        probe = spec.get("probe")
        if declared:
            _require(
                isinstance(probe, dict),
                f"{where}: declared true requires a probe (capabilities are never over-claimed)",
            )
            _check_argv(f"{where}.probe", probe.get("argv"))
            _check_timeout(f"{where}.probe", probe.get("timeout_seconds"))
            _check_pattern(f"{where}.probe", probe.get("pattern"))
        else:
            _require(probe is None, f"{where}: declared false must not carry a probe")


def load_manifest(path: Path = DEFAULT_MANIFEST, providers_path: Path = DEFAULT_PROVIDERS) -> dict:
    data = _load_yaml(path)
    _require(isinstance(data, dict), f"{path}: top level must be a mapping")
    _require(data.get("version") == 1, f"{path}: version must be 1")
    connectors = data.get("connectors")
    _require(isinstance(connectors, dict) and connectors, f"{path}: connectors must be a non-empty mapping")

    providers_doc = _load_yaml(providers_path)
    providers = (providers_doc or {}).get("providers") if isinstance(providers_doc, dict) else None
    _require(isinstance(providers, dict), f"{providers_path}: providers mapping missing")

    resolved: dict[str, dict] = {}
    for name, spec in connectors.items():
        _require(isinstance(spec, dict), f"connector {name} must be a mapping")
        _require(isinstance(spec.get("display_name"), str), f"{name}.display_name is required")
        ref = spec.get("provider_ref")
        if ref is not None:
            _require(ref in providers, f"{name}.provider_ref {ref!r} is not in {providers_path}")
            dup = [f for f in PROVIDER_OWNED_FIELDS if f in spec]
            _require(
                not dup,
                f"{name}: provider_ref already supplies {dup}; do not re-declare them",
            )
            provider = providers[ref]
            binaries = provider.get("binary_candidates")
            strip = provider.get("strip_environment", [])
            billing = provider.get("billing_policy")
        else:
            binaries = spec.get("binary_candidates")
            strip = spec.get("strip_environment", [])
            billing = spec.get("billing_policy")
        _require(
            isinstance(binaries, list) and binaries and all(isinstance(b, str) for b in binaries),
            f"{name}: binary_candidates must be a non-empty list",
        )
        _require(
            isinstance(strip, list) and all(isinstance(s, str) for s in strip),
            f"{name}: strip_environment must be a list of names",
        )
        _require(
            billing in BILLING_POLICIES,
            f"{name}: billing_policy must be one of {sorted(BILLING_POLICIES)}",
        )
        _validate_login(name, spec.get("login"))
        _validate_capabilities(name, spec.get("capabilities", {}))

        resolved[name] = {
            **spec,
            "provider_ref": ref,
            "binary_candidates": list(binaries),
            "strip_environment": list(strip),
            "billing_policy": billing,
            "capabilities": spec.get("capabilities", {}),
        }
    return {"version": 1, "connectors": resolved}


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

def resolve_binary(candidates: list[str], env: dict[str, str]) -> str | None:
    for candidate in candidates:
        expanded = os.path.expanduser(candidate)
        if os.sep in expanded:
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded
        else:
            found = shutil.which(expanded, path=env.get("PATH"))
            if found:
                return found
    return None


def _probe_env(strip: list[str]) -> dict[str, str]:
    env = dict(os.environ)
    for key in strip:
        env.pop(key, None)
    return env


def _run_probe(binary: str, argv: list[str], timeout: float, env: dict[str, str]) -> dict:
    """Run a probe; return exit code + output kept in memory only."""

    cmd = [binary if a == BINARY_TOKEN else a for a in argv]
    effective = min(float(timeout), float(PROBE_TIMEOUT_CAP_SECONDS))
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=effective,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ran": False, "why": f"timeout after {effective:g}s"}
    except OSError as exc:
        return {"ran": False, "why": f"could not start: {exc.__class__.__name__}"}
    return {"ran": True, "exit_code": proc.returncode, "output": (proc.stdout or "") + (proc.stderr or "")}


def _classify_items(items: dict, exit_code: int, output: str) -> tuple[bool, dict | None]:
    """Aggregate a one-line-per-source listing (e.g. ``kimi provider list``).

    Returns ``(found, state)``. ``found`` is False when the output has no item
    line at all (the caller then falls back to whole-output rules). Otherwise
    every line must be either an ignorable line or an item line that some item
    rule classifies -- anything else makes the result unknown (``None``).

    An item line is a line that ``item_pattern`` matches *in full* (no text
    before or after the record). A rule classifies it only when every field
    it names matches that record field *in full*. Only a single agreed method
    passes through; disagreeing sources yield ``mixed``, which no billing
    policy accepts.
    """

    if items["exit_code"] != exit_code:
        return False, None
    item_re = re.compile(items["item_pattern"])
    ignore_re = re.compile(items["ignore_pattern"]) if items.get("ignore_pattern") else None
    methods: set[str] = set()
    found = False
    unrecognised = False
    for line in output.splitlines():
        record = item_re.fullmatch(line)
        if record:
            found = True
            fields = record.groupdict()
            for rule in items["rules"]:
                if all(
                    fields.get(name) is not None and re.fullmatch(pattern, fields[name])
                    for name, pattern in rule["fields"].items()
                ):
                    methods.add(rule["method"])
                    break
            else:
                unrecognised = True
        elif ignore_re is not None and ignore_re.fullmatch(line):
            continue
        else:
            unrecognised = True
    if not found:
        return False, None
    if unrecognised:
        return True, None
    method = next(iter(methods)) if len(methods) == 1 else MIXED_METHOD
    return True, {"state": "logged_in", "method": method}


def classify_login(probe_or_rules: dict | list[dict], exit_code: int, output: str) -> dict | None:
    if isinstance(probe_or_rules, dict):
        rules = probe_or_rules["rules"]
        items = probe_or_rules.get("items")
    else:
        rules, items = probe_or_rules, None
    if items is not None:
        found, state = _classify_items(items, exit_code, output)
        if found:
            return state
    for rule in rules:
        if rule["exit_code"] == exit_code and re.search(rule["pattern"], output, re.MULTILINE):
            return {"state": rule["state"], "method": rule.get("method")}
    return None


def _check(name: str, result: str, detail: str, **extra: Any) -> dict:
    return {"check": name, "result": result, "detail": detail, **extra}


def _capabilities(
    spec: dict, binary: str | None, env: dict[str, str], run_probes: bool = True
) -> list[dict]:
    out = []
    for cap, cspec in spec["capabilities"].items():
        row = {"name": cap, "declared": cspec["declared"]}
        if not cspec["declared"]:
            row.update(result=NOT_APPLICABLE, detail=cspec.get("note", "declared false"))
        elif binary is None:
            row.update(result=NOT_CHECKED, detail="binary not installed")
        elif not run_probes:
            row.update(result=NOT_CHECKED, detail="skipped: --login-only")
        else:
            probe = cspec["probe"]
            ran = _run_probe(binary, probe["argv"], probe["timeout_seconds"], env)
            if not ran["ran"]:
                row.update(result=NOT_CHECKED, detail=ran["why"])
            elif ran["exit_code"] == 0 and re.search(probe["pattern"], ran["output"], re.MULTILINE):
                row.update(result=PASS, detail="probe confirmed")
            else:
                row.update(
                    result=FAIL,
                    detail=f"declared but probe did not confirm (exit {ran['exit_code']}; output not echoed)",
                )
        out.append(row)
    return out


def connector_status(manifest: dict, name: str, login_only: bool = False) -> dict:
    spec = manifest["connectors"][name]
    env = _probe_env(spec["strip_environment"])
    checks: list[dict] = []

    binary = resolve_binary(spec["binary_candidates"], env)
    if binary is None:
        checks.append(_check("binary", FAIL, "not found in binary_candidates / PATH"))
    else:
        checks.append(_check("binary", PASS, binary))

    probe = spec["login"]["status_probe"]
    login_state: dict | None = None
    if binary is None:
        checks.append(_check("login", NOT_CHECKED, "binary not installed", evidence=probe["evidence"]))
    else:
        ran = _run_probe(binary, probe["argv"], probe["timeout_seconds"], env)
        if not ran["ran"]:
            checks.append(_check("login", NOT_CHECKED, ran["why"], evidence=probe["evidence"]))
        else:
            login_state = classify_login(probe, ran["exit_code"], ran["output"])
            if login_state is None:
                checks.append(
                    _check(
                        "login",
                        NOT_CHECKED,
                        f"exit {ran['exit_code']}; output matched no rule (output not echoed)",
                        evidence=probe["evidence"],
                    )
                )
            elif login_state["state"] == "logged_in":
                checks.append(
                    _check("login", PASS, "logged in", evidence=probe["evidence"], method=login_state["method"])
                )
            else:
                checks.append(_check("login", FAIL, "not logged in", evidence=probe["evidence"]))

    login_row = checks[-1]
    policy = spec["billing_policy"]
    if login_row["result"] != PASS:
        checks.append(_check("billing_policy", NOT_CHECKED, f"{policy}: no login to compare"))
    elif login_row["method"] in BILLING_POLICIES[policy]:
        checks.append(_check("billing_policy", PASS, f"{policy}: method {login_row['method']}"))
    else:
        checks.append(
            _check("billing_policy", FAIL, f"{policy}: method {login_row['method']} not allowed")
        )

    checks.append(
        _check(
            "live_turn",
            NOT_CHECKED,
            "prototype does not run a real turn; login configured is not proof the account works",
        )
    )

    if binary is None:
        login_verdict = "needs_install"
    elif login_row["result"] == FAIL:
        login_verdict = "needs_login"
    elif login_row["result"] == NOT_CHECKED:
        login_verdict = "unknown"
    elif checks[-2]["result"] != PASS:
        login_verdict = "policy_mismatch"
    else:
        login_verdict = "login_configured"

    capabilities = _capabilities(spec, binary, env, run_probes=not login_only)
    declared = [c for c in capabilities if c["declared"]]
    live_turn = checks[-1]
    if login_verdict != "login_configured":
        verdict = login_verdict
    elif any(c["result"] == FAIL for c in declared):
        verdict = "capability_failed"
    elif any(c["result"] != PASS for c in declared):
        verdict = "capability_not_checked"
    elif live_turn["result"] != PASS:
        verdict = "live_turn_not_checked"
    else:
        verdict = "ready"

    return {
        "connector": name,
        "display_name": spec["display_name"],
        "provider_ref": spec["provider_ref"],
        "login_verdict": login_verdict,
        "verdict": verdict,
        "checks": checks,
        "capabilities": capabilities,
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_guide(name: str, spec: dict) -> str:
    guide = spec["login"]["guide"]
    lines = [f"[{name}] 登录引导（只提示，不代为执行、不代输凭据）"]
    if "official_command" in spec.get("install", {}):
        lines.append(f"  未安装时的官方安装命令：{spec['install']['official_command']}")
    lines.append("  登录命令：")
    lines.extend(f"    {c}" for c in guide["login_commands"])
    lines.append("  步骤：")
    lines.extend(f"    {i}. {s}" for i, s in enumerate(guide["steps"], 1))
    for key, label in (("signup_url", "注册页"), ("signup_url_global", "注册页（海外）")):
        if key in guide:
            lines.append(f"  {label}：{guide[key]}")
    return "\n".join(lines)


def render_status(result: dict) -> str:
    name = result["connector"]
    lines = [
        f"{name}（{result['display_name']}）：{VERDICT_ZH[result['verdict']]} [{result['verdict']}]",
        f"  登录层：{VERDICT_ZH[result['login_verdict']]} [{result['login_verdict']}]",
    ]
    labels = {"binary": "可执行文件", "login": "登录", "billing_policy": "计费策略", "live_turn": "真实对话"}
    for check in result["checks"]:
        extra = ""
        if check["check"] == "login":
            extra = f"（证据：{check['evidence']}"
            extra += f"；方式：{check['method']}）" if check.get("method") else "）"
        lines.append(
            f"  - {labels[check['check']]}：{RESULT_ZH[check['result']]}  {check['detail']}{extra}"
        )
    caps = " ".join(f"{c['name']}={RESULT_ZH[c['result']]}" for c in result["capabilities"])
    lines.append(f"  - 能力：{caps}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_connect.py", description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--providers-manifest", type=Path, default=DEFAULT_PROVIDERS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    st = sub.add_parser("status")
    st.add_argument("--connector", action="append", default=None)
    st.add_argument("--json", action="store_true")
    st.add_argument(
        "--login-only",
        action="store_true",
        help="only check binary + login + billing policy; skip capability probes; "
        "exit 0 when every login_verdict is login_configured",
    )
    gd = sub.add_parser("guide")
    gd.add_argument("connector")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest, args.providers_manifest)
    except ConnectorManifestError as exc:
        print(f"manifest invalid: {exc}", file=sys.stderr)
        return 2

    if args.command == "validate":
        print(f"ok: {len(manifest['connectors'])} connectors: {', '.join(manifest['connectors'])}")
        return 0

    if args.command == "guide":
        if args.connector not in manifest["connectors"]:
            print(f"unknown connector: {args.connector}", file=sys.stderr)
            return 2
        print(render_guide(args.connector, manifest["connectors"][args.connector]))
        return 0

    names = args.connector or list(manifest["connectors"])
    unknown = [n for n in names if n not in manifest["connectors"]]
    if unknown:
        print(f"unknown connector: {', '.join(unknown)}", file=sys.stderr)
        return 2
    results = [connector_status(manifest, n, login_only=args.login_only) for n in names]
    scope = "login-only" if args.login_only else "full"
    if args.json:
        print(json.dumps({"scope": scope, "connectors": results}, ensure_ascii=False, indent=2))
    else:
        blocks = [f"范围：{'仅登录层（--login-only，未跑能力探测）' if args.login_only else '完整（登录 + 能力 + 真实对话）'}"]
        for r in results:
            block = render_status(r)
            if r["login_verdict"] != "login_configured":
                block += "\n" + render_guide(r["connector"], manifest["connectors"][r["connector"]])
            blocks.append(block)
        print("\n\n".join(blocks))
    if args.login_only:
        ok = all(r["login_verdict"] == "login_configured" for r in results)
    else:
        ok = all(r["verdict"] == "ready" for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
