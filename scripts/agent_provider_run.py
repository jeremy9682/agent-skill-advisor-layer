#!/usr/bin/env python3
"""Run local AI CLIs through one observable, privacy-minimized interface.

The provider manifest is portable. Native transcripts and credentials remain in
each product's own local storage. The append-only journal records pointers and
digests, never prompt/response text or auth material.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

import yaml

try:
    from scripts.ledger_core import FIELDS as LEDGER_FIELDS
    from scripts.ledger_core import checkpoint_state
    from scripts.routing_runtime import (
        RoutingRuntimeError,
        build_instruction_bom,
        load_routing_canon,
        parse_cursor_model_catalog,
        resolve_binding,
        resolve_model_family,
    )
except ModuleNotFoundError:  # Direct execution through the ~/.local/bin symlink.
    from ledger_core import FIELDS as LEDGER_FIELDS
    from ledger_core import checkpoint_state
    from routing_runtime import (
        RoutingRuntimeError,
        build_instruction_bom,
        load_routing_canon,
        parse_cursor_model_catalog,
        resolve_binding,
        resolve_model_family,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "agent-providers.yaml"
SEAT_RE = re.compile(
    r"^(?:claude|codex|fable|opus|sonnet|human|founder)"
    r"(?:-[a-z]+(?:-[a-z]+)*)?$"
)
DEFAULT_RUN_TIMEOUT_SECONDS = 300
# Explicit `agent-run run <provider>` only — governed routes use serial_group
# from routing-policy.yaml. Cursor is intentionally omitted so mechanical routes
# without serial_group can run in parallel.
PROVIDER_SERIAL_GROUPS = {
    "claude": "claude-family",
    "codex": "codex-family",
    "grok": "grok-family",
}
SERIAL_LOCK_WAIT_SECONDS = 900
CURSOR_ATTRIBUTION_SETTLE_SECONDS = 3.0
CURSOR_ATTRIBUTION_POLL_SECONDS = 0.1
SKILL_NAME_RE = re.compile(r"^- `([^`]+)`$")
VERIFIED_BROKER_SESSION_STATUSES = frozenset(
    {
        "attributed-single-artifact",
        "attributed-correlated-artifacts",
        "attributed-stream-json",
    }
)


class ProviderRunError(RuntimeError):
    pass


class SerialLockTimeout(ProviderRunError):
    pass


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ProviderRunError("provider manifest must be a mapping with version: 1")
    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ProviderRunError("provider manifest needs a non-empty providers mapping")
    if "routes" in data:
        raise ProviderRunError(
            "provider manifest may not define routes; routing-policy.yaml is the sole canon"
        )
    journal = data.get("journal")
    if not isinstance(journal, dict):
        raise ProviderRunError("provider manifest needs a journal mapping")
    max_age = journal.get("live_evidence_max_age_seconds")
    if not isinstance(max_age, int) or max_age <= 0:
        raise ProviderRunError(
            "journal.live_evidence_max_age_seconds must be a positive integer"
        )
    future_skew = journal.get("live_evidence_future_skew_seconds")
    if not isinstance(future_skew, int) or future_skew < 0:
        raise ProviderRunError(
            "journal.live_evidence_future_skew_seconds must be a non-negative integer"
        )
    for provider_id, provider in providers.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", provider_id):
            raise ProviderRunError(f"invalid provider id: {provider_id!r}")
        for key in ("binary_candidates", "commands", "session", "billing_policy"):
            if key not in provider:
                raise ProviderRunError(f"provider {provider_id!r} missing {key}")
        for mode in ("read-only", "execute"):
            if mode not in provider["commands"]:
                raise ProviderRunError(
                    f"provider {provider_id!r} missing command mode {mode}"
                )
    return data


def routing_canon(config: dict) -> dict:
    path = ROOT / str(config.get("routing_canon") or "routing-policy.yaml")
    try:
        return load_routing_canon(path)
    except RoutingRuntimeError as exc:
        raise ProviderRunError(str(exc)) from exc


def route_binding(config: dict, route_name: str) -> dict:
    try:
        return resolve_binding(routing_canon(config), route_name)
    except RoutingRuntimeError as exc:
        raise ProviderRunError(str(exc)) from exc


def canonical_provider_id(config: dict, provider_id: str) -> str:
    aliases = config.get("provider_aliases", {})
    return str(aliases.get(provider_id) or provider_id)


def discover_provider_models(provider: dict, binary: Path) -> dict:
    if "model_discovery" not in provider:
        return {
            "status": "static-config",
            "models": [
                {"id": str(model)} for model in provider.get("model_options", [])
            ],
        }
    discovery = provider["model_discovery"]
    if not isinstance(discovery, dict):
        return {"status": "discovery-config-invalid", "models": []}
    command_parts = discovery.get("command")
    if (
        not isinstance(command_parts, list)
        or not command_parts
        or any(not isinstance(part, str) for part in command_parts)
    ):
        return {"status": "discovery-config-invalid", "models": []}
    parser = str(discovery.get("parser") or "")
    if parser != "cursor-models-v1":
        return {"status": "discovery-parser-unsupported", "models": []}
    try:
        command = [
            str(part).format_map({"binary": str(binary)}) for part in command_parts
        ]
    except (KeyError, ValueError, TypeError):
        return {"status": "discovery-config-invalid", "models": []}
    env, _stripped = scrub_environment(provider)
    try:
        run = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "catalog-unavailable", "models": []}
    if run.returncode != 0:
        return {"status": "catalog-unavailable", "models": []}
    try:
        models = parse_cursor_model_catalog(run.stdout or "")
    except RoutingRuntimeError:
        return {"status": "catalog-malformed", "models": []}
    return {"status": "catalog-listed", "models": models}


def validate_provider_model(
    provider_id: str,
    provider: dict,
    binary: Path,
    model: str,
) -> dict:
    catalog = discover_provider_models(provider, binary)
    allowed = {str(row.get("id")) for row in catalog["models"] if row.get("id")}
    if catalog["status"] not in {"catalog-listed", "static-config"}:
        raise ProviderRunError(
            f"model catalog is unavailable for provider {provider_id!r}: {catalog['status']}"
        )
    if model not in allowed:
        raise ProviderRunError(
            f"model {model!r} is not listed for provider {provider_id}"
        )
    return catalog


def resolve_binary(provider: dict) -> Path:
    for raw in provider["binary_candidates"]:
        candidate = expand(str(raw))
        if (
            candidate.is_absolute()
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ):
            return candidate.resolve()
        found = shutil.which(str(raw))
        if found:
            return Path(found).resolve()
    raise ProviderRunError("provider binary not found")


def binary_version(binary: Path, provider: dict) -> str:
    env, _stripped = scrub_environment(provider)
    try:
        run = subprocess.run(
            [str(binary), *map(str, provider.get("version_args", ["--version"]))],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (
        (run.stdout or run.stderr).strip().splitlines()[0][:160]
        if (run.stdout or run.stderr)
        else "unknown"
    )


def repo_slug(cwd: Path) -> str:
    try:
        top = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if top.returncode == 0 and top.stdout.strip():
            root = Path(top.stdout.strip())
            override = root / ".agents" / "ledger-slug"
            if override.is_file():
                value = override.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"[A-Za-z0-9._-]+", value):
                    return value
            return root.name
    except (OSError, subprocess.TimeoutExpired):
        pass
    return cwd.name or "projectless"


def portable_ref(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(path.resolve().relative_to(home))
    except (ValueError, OSError):
        return "external:" + sha256_text(str(path.resolve()))[:16]


def file_fingerprint(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


def grok_sessions(roots: list[str]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for raw_root in roots:
        root = expand(raw_root)
        if not root.is_dir():
            continue
        for summary in root.glob("*/*/summary.json"):
            out[str(summary.parent)] = file_fingerprint(summary)
    return out


def cursor_sessions(roots: list[str]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for raw_root in roots:
        root = expand(raw_root)
        if not root.is_dir():
            continue
        if root.name == "projects":
            pattern = "*/agent-transcripts/**/*.jsonl"
        elif root.name == "chats":
            pattern = "*/*/store.db"
        else:
            pattern = "**/*"
        for path in root.glob(pattern):
            if path.is_file():
                out[str(path)] = file_fingerprint(path)
    return out


def codex_sessions(roots: list[str]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for raw_root in roots:
        root = expand(raw_root)
        if root.is_dir():
            for path in root.glob("**/rollout-*.jsonl"):
                if path.is_file():
                    out[str(path)] = file_fingerprint(path)
    return out


def claude_sessions(roots: list[str]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for raw_root in roots:
        root = expand(raw_root)
        if not root.is_dir():
            continue
        for path in root.glob("**/*.jsonl"):
            if path.is_file() and "subagents" not in path.parts:
                out[str(path)] = file_fingerprint(path)
    return out


def session_snapshot(provider: dict) -> dict[str, tuple[int, int]]:
    session = provider["session"]
    adapter = session.get("adapter")
    roots = list(map(str, session.get("roots", [])))
    if adapter == "grok":
        return grok_sessions(roots)
    if adapter == "cursor":
        return cursor_sessions(roots)
    if adapter == "codex":
        return codex_sessions(roots)
    if adapter == "claude":
        return claude_sessions(roots)
    raise ProviderRunError(f"unsupported session adapter: {adapter!r}")


def changed_session(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> tuple[Path | None, str, int]:
    changed = [Path(path) for path, fp in after.items() if before.get(path) != fp]
    if not changed:
        return None, "not-observed", 0
    if len(changed) != 1:
        return None, "ambiguous-concurrent-artifacts", len(changed)
    return changed[0], "attributed-single-artifact", 1


def attribute_session(
    adapter: str,
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
    requested_model: str | None = None,
) -> tuple[dict, int]:
    paths = [Path(path) for path, fp in after.items() if before.get(path) != fp]
    if not paths:
        return parse_session(adapter, None, "not-observed"), 0
    if len(paths) == 1:
        return parse_session(adapter, paths[0], "attributed-single-artifact"), 1
    parsed = [parse_session(adapter, path, "candidate") for path in paths]
    session_ids = {
        row["session_id"] for row in parsed if row["session_id"] != "unknown"
    }
    if len(session_ids) == 1:
        # Prefer the provider's richer native store when several files belong to
        # one session (Cursor normally writes both JSONL and store.db).
        best = max(
            parsed,
            key=lambda row: (
                row["model_observed"] not in {"unknown", "auto-undisclosed"},
                row["session_ref"].endswith("store.db"),
            ),
        )
        return dict(best, session_status="attributed-correlated-artifacts"), len(paths)
    if adapter == "cursor" and requested_model not in {None, "", "auto"}:
        # Concurrent Cursor invocations share the same artifact roots. Correlate
        # only when every changed session has complete native model metadata and
        # exactly one session matches this invocation's exact named model. This
        # remains ambiguous for same-model concurrency or an unflushed store, so
        # it cannot hijack another invocation's session.
        by_session: dict[str, list[dict]] = {}
        for row in parsed:
            session_id = str(row.get("session_id") or "unknown")
            if session_id == "unknown":
                return parse_session(
                    adapter, None, "ambiguous-concurrent-artifacts"
                ), len(paths)
            by_session.setdefault(session_id, []).append(row)
        native_models: dict[str, str] = {}
        for session_id, rows in by_session.items():
            models = {
                str(row["model_observed"])
                for row in rows
                if row.get("model_observation_reason") == "cursor-store-db"
                and row.get("model_observed") not in {None, "", "unknown", "auto-undisclosed"}
            }
            if len(models) != 1:
                return parse_session(
                    adapter, None, "ambiguous-concurrent-artifacts"
                ), len(paths)
            native_models[session_id] = models.pop()
        matching = [
            session_id
            for session_id, model in native_models.items()
            if model == requested_model
        ]
        if len(matching) == 1:
            matching_rows = by_session[matching[0]]
            best = max(
                matching_rows,
                key=lambda row: row.get("model_observation_reason") == "cursor-store-db",
            )
            return dict(
                best, session_status="attributed-correlated-artifacts"
            ), len(paths)
    return parse_session(adapter, None, "ambiguous-concurrent-artifacts"), len(paths)


def cursor_metadata_signature(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> tuple[tuple[str, str], ...] | None:
    paths = [Path(path) for path, fp in after.items() if before.get(path) != fp]
    parsed = [parse_session("cursor", path, "candidate") for path in paths]
    by_session: dict[str, set[str]] = {}
    for row in parsed:
        session_id = str(row.get("session_id") or "unknown")
        if session_id == "unknown":
            return None
        by_session.setdefault(session_id, set())
        if (
            row.get("model_observation_reason") == "cursor-store-db"
            and row.get("model_observed")
            not in {None, "", "unknown", "auto-undisclosed"}
        ):
            by_session[session_id].add(str(row["model_observed"]))
    if not by_session or any(len(models) != 1 for models in by_session.values()):
        return None
    return tuple(
        sorted((session_id, next(iter(models))) for session_id, models in by_session.items())
    )


def settle_cursor_attribution(
    *,
    provider: dict,
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
    requested_model: str,
    overall_started: float,
    timeout_seconds: float,
    snapshot_fn=None,
    monotonic_fn=None,
    sleep_fn=None,
    max_wait_seconds: float = CURSOR_ATTRIBUTION_SETTLE_SECONDS,
    poll_seconds: float = CURSOR_ATTRIBUTION_POLL_SECONDS,
) -> tuple[dict, int, dict[str, tuple[int, int]], dict]:
    snapshot_fn = snapshot_fn or session_snapshot
    monotonic_fn = monotonic_fn or time.monotonic
    sleep_fn = sleep_fn or time.sleep
    session, changed_count = attribute_session(
        "cursor", before, after, requested_model=requested_model
    )
    telemetry = {
        "status": "not-needed",
        "poll_count": 0,
        "waited_ms": 0,
        "budget_ms": 0,
        "stable_samples_required": 2,
    }
    if (
        requested_model in {"", "auto"}
        or "ambiguous" not in str(session.get("session_status") or "")
    ):
        return session, changed_count, after, telemetry
    now = monotonic_fn()
    total_remaining = max(0.0, overall_started + timeout_seconds - now)
    budget = min(max_wait_seconds, total_remaining)
    telemetry["budget_ms"] = round(budget * 1000)
    if budget <= 0:
        telemetry["status"] = "total-timeout-exhausted"
        return session, changed_count, after, telemetry
    deadline = now + budget
    stable_key: tuple | None = None
    stable_samples = 0
    latest_after = after
    latest_count = changed_count
    while monotonic_fn() < deadline:
        remaining = deadline - monotonic_fn()
        sleep_fn(min(poll_seconds, remaining))
        telemetry["poll_count"] += 1
        latest_after = snapshot_fn(provider)
        candidate, latest_count = attribute_session(
            "cursor", before, latest_after, requested_model=requested_model
        )
        signature = cursor_metadata_signature(before, latest_after)
        if (
            signature is not None
            and "ambiguous" not in str(candidate.get("session_status") or "")
        ):
            key = (
                str(candidate.get("session_id") or "unknown"),
                str(candidate.get("model_observed") or "unknown"),
                signature,
            )
            if key == stable_key:
                stable_samples += 1
            else:
                stable_key = key
                stable_samples = 1
            if stable_samples >= telemetry["stable_samples_required"]:
                telemetry["status"] = "settled"
                telemetry["waited_ms"] = round(
                    (monotonic_fn() - now) * 1000
                )
                return candidate, latest_count, latest_after, telemetry
        else:
            stable_key = None
            stable_samples = 0
    telemetry["status"] = (
        "timeout-unstable" if stable_samples else "timeout-ambiguous"
    )
    telemetry["waited_ms"] = round((monotonic_fn() - now) * 1000)
    return (
        parse_session("cursor", None, "ambiguous-concurrent-artifacts"),
        latest_count,
        latest_after,
        telemetry,
    )


def decode_cursor_meta(db_path: Path) -> dict:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute(
                "select value from meta where key = '0' limit 1"
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {}
    if not row:
        return {}
    raw = row[0]
    if isinstance(raw, str):
        data = raw.encode()
    else:
        data = bytes(raw)
    try:
        if re.fullmatch(rb"[0-9a-fA-F]+", data) and len(data) % 2 == 0:
            data = bytes.fromhex(data.decode())
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return {}


def cursor_model_names(value: object) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        provider_options = value.get("providerOptions")
        if isinstance(provider_options, dict):
            cursor = provider_options.get("cursor")
            if isinstance(cursor, dict) and isinstance(cursor.get("modelName"), str):
                names.append(cursor["modelName"])
        for child in value.values():
            names.extend(cursor_model_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(cursor_model_names(child))
    return names


def decode_cursor_model(db_path: Path) -> str:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute("select data from blobs order by rowid").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return "unknown"
    names: list[str] = []
    decoder = json.JSONDecoder()
    for row in rows:
        raw = row[0]
        data = raw.encode() if isinstance(raw, str) else bytes(raw)
        if re.fullmatch(rb"[0-9a-fA-F]+", data) and len(data) % 2 == 0:
            try:
                data = bytes.fromhex(data.decode())
            except ValueError:
                continue
        start = data.find(b"{")
        if start < 0:
            continue
        try:
            parsed, _end = decoder.raw_decode(data[start:].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        names.extend(cursor_model_names(parsed))
    return names[-1] if names else "unknown"


def _scan_jsonl_rows(path: Path, limit: int = 2000, *, prefer_tail: bool = False):
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if prefer_tail and len(raw_lines) > limit:
        selected = raw_lines[-limit:]
    else:
        selected = raw_lines[:limit]
    for line in selected:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def extract_codex_model_from_jsonl(path: Path) -> tuple[str, str]:
    """Read model identity from Codex rollout JSONL. Never invents a value."""

    def scan(prefer_tail: bool) -> str:
        last_model = ""
        for row in _scan_jsonl_rows(path, prefer_tail=prefer_tail):
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            for key in ("model", "current_model_id", "primaryModelId", "primary_model_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    last_model = value.strip()
            info = payload.get("info")
            if isinstance(info, dict):
                for key in ("current_model_id", "model", "primaryModelId"):
                    value = info.get(key)
                    if isinstance(value, str) and value.strip():
                        last_model = value.strip()
        return last_model

    last_model = scan(False) or scan(True)
    if last_model:
        return last_model, "codex-jsonl-turn-context"
    return "unknown", "codex-jsonl-model-missing"


def extract_claude_model_from_jsonl(path: Path) -> tuple[str, str]:
    """Read model identity from Claude transcript JSONL. Never invents a value."""
    last_model = ""
    for row in _scan_jsonl_rows(path):
        message = row.get("message")
        if isinstance(message, dict):
            value = message.get("model")
            if isinstance(value, str) and value.strip():
                last_model = value.strip()
        value = row.get("model")
        if isinstance(value, str) and value.strip():
            last_model = value.strip()
    if last_model:
        return last_model, "claude-jsonl-assistant-message"
    return "unknown", "claude-jsonl-model-missing"


def parse_session(
    adapter: str, path: Path | None, attribution_status: str = "not-observed"
) -> dict:
    if path is None:
        return {
            "session_id": "unknown",
            "session_ref": "unknown",
            "model_observed": "unknown",
            "model_observation_reason": "no-session-path",
            "session_status": attribution_status,
        }
    if adapter == "grok":
        summary_path = path / "summary.json" if path.is_dir() else path
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = {}
        info = summary.get("info") if isinstance(summary.get("info"), dict) else summary
        observed = str(
            info.get("current_model_id")
            or info.get("model")
            or summary.get("current_model_id")
            or summary.get("model")
            or "unknown"
        )
        return {
            "session_id": str(info.get("id") or summary.get("id") or path.name),
            "session_ref": portable_ref(summary_path.parent),
            "model_observed": observed,
            "model_observation_reason": (
                "grok-summary" if observed != "unknown" else "grok-summary-model-missing"
            ),
            "session_status": attribution_status,
        }
    if adapter == "cursor":
        if path.name == "store.db":
            meta = decode_cursor_meta(path)
            blob_model = decode_cursor_model(path)
            observed = str(
                meta.get("lastUsedModel")
                or (blob_model if blob_model != "unknown" else "auto-undisclosed")
            )
            return {
                "session_id": str(meta.get("agentId") or path.parent.name),
                "session_ref": portable_ref(path),
                "model_observed": observed,
                "model_observation_reason": "cursor-store-db",
                "session_status": attribution_status,
            }
        return {
            "session_id": path.stem,
            "session_ref": portable_ref(path),
            "model_observed": "auto-undisclosed",
            "model_observation_reason": "cursor-jsonl-only",
            "session_status": attribution_status,
        }
    if adapter == "codex":
        match = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
            path.stem,
            re.IGNORECASE,
        )
        session_id = match.group(1) if match else path.stem
        observed, reason = extract_codex_model_from_jsonl(path)
        return {
            "session_id": session_id,
            "session_ref": portable_ref(path),
            "model_observed": observed,
            "model_observation_reason": reason,
            "session_status": attribution_status,
        }
    if adapter == "claude":
        observed, reason = extract_claude_model_from_jsonl(path)
        return {
            "session_id": path.stem,
            "session_ref": portable_ref(path),
            "model_observed": observed,
            "model_observation_reason": reason,
            "session_status": attribution_status,
        }
    raise ProviderRunError(f"unsupported session adapter: {adapter!r}")


def skill_manifest_info(config: dict) -> tuple[Path, dict, str]:
    path = expand(config["skills"]["manifest"])
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ProviderRunError(f"cannot read skill manifest {path}: {exc}") from exc
    return path, data, sha256_bytes(raw)


def skill_entries_by_name(manifest: dict) -> dict[str, dict]:
    selected: dict[str, dict] = {}
    for entry in manifest.get("entries", []):
        if not isinstance(entry, dict) or not entry.get("frontmatter_ok"):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        # Prefer the Codex copy when aliases exist; it is the current wrapper's
        # runtime and the audit guarantees cross-runtime pin equivalence.
        old = selected.get(name)
        if old is None or (
            entry.get("runtime") == "codex" and old.get("runtime") != "codex"
        ):
            selected[name] = entry
    return selected


def auto_skill_names(prompt: str, cwd: Path, config: dict) -> tuple[list[str], str]:
    hook = ROOT / config["skills"]["router_hook"]
    payload = json.dumps(
        {"prompt": prompt, "cwd": str(cwd), "session_id": "agent-run-preflight"}
    )
    try:
        run = subprocess.run(
            [sys.executable, str(hook)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        parsed = json.loads(run.stdout or "{}")
        context = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
    except OSError:
        return [], "router-exec-error"
    except subprocess.TimeoutExpired:
        return [], "router-timeout"
    except ValueError:
        return [], "router-malformed-output"
    names: list[str] = []
    for line in context.splitlines():
        match = SKILL_NAME_RE.fullmatch(line.strip())
        if match:
            names.append(match.group(1))
    if run.returncode != 0:
        return names, f"router-exit-{run.returncode}"
    return names, "ok"


def select_skills(prompt: str, cwd: Path, requested: list[str], config: dict) -> dict:
    _path, manifest, manifest_sha = skill_manifest_info(config)
    entries = skill_entries_by_name(manifest)
    auto_requested = not requested or "auto" in requested
    explicit = [name for name in requested if name != "auto"]
    if auto_requested:
        suggested, routing_status = auto_skill_names(prompt, cwd, config)
    else:
        suggested, routing_status = [], "not-requested"
    allowed_auto = set(map(str, config["skills"].get("auto_select_policies", [])))

    chosen: list[dict] = []
    deferred: list[dict] = []
    seen: set[str] = set()
    for name, source in [
        *((n, "explicit") for n in explicit),
        *((n, "auto") for n in suggested),
    ]:
        if name in seen:
            continue
        seen.add(name)
        entry = entries.get(name)
        if entry is None:
            raise ProviderRunError(f"unknown skill: {name}")
        policy = str(entry.get("call_policy") or "unknown")
        row = {
            "name": name,
            "digest": str(entry.get("tree_hash") or "unknown"),
            "source_group": str(entry.get("source_group") or "unknown"),
            "call_policy": policy,
            "selection_source": source,
        }
        if source == "auto" and policy not in allowed_auto:
            deferred.append(row)
        else:
            chosen.append(row)

    return {
        "manifest_sha256": manifest_sha,
        "available_count": len(entries),
        "chosen": chosen,
        "deferred": deferred,
        "entries": entries,
        "routing_status": routing_status,
        "trusted_content_roots": list(
            map(str, config["skills"].get("trusted_content_roots", []))
        ),
    }


def augment_prompt(prompt: str, skill_selection: dict, max_bytes: int) -> str:
    blocks: list[str] = []
    total = 0
    for row in skill_selection["chosen"]:
        entry = skill_selection["entries"][row["name"]]
        skill_path = Path(entry["skill_md"]).expanduser().resolve()
        trusted_roots = [
            expand(raw).resolve()
            for raw in skill_selection.get("trusted_content_roots", [])
        ]
        if not trusted_roots or not any(
            skill_path == root or root in skill_path.parents for root in trusted_roots
        ):
            raise ProviderRunError(
                f"skill content path is outside trusted roots: {row['name']}"
            )
        try:
            content = skill_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProviderRunError(f"cannot expose skill {row['name']}: {exc}") from exc
        encoded = content.encode("utf-8")
        if total + len(encoded) > max_bytes:
            raise ProviderRunError("managed skill content exceeds max_embedded_bytes")
        total += len(encoded)
        row["content_sha256"] = sha256_bytes(encoded)
        blocks.append(
            f'<managed-skill name="{row["name"]}" tree_sha256="{row["digest"]}" '
            f'content_sha256="{row["content_sha256"]}">\n'
            f"{content}\n</managed-skill>"
        )
    if not blocks:
        return prompt
    return (
        prompt
        + "\n\nThe following managed skills were selected by the local governance wrapper. "
        "Follow them where applicable; their presence proves delivery, not semantic compliance.\n\n"
        + "\n\n".join(blocks)
    )


def build_command(
    provider: dict,
    mode: str,
    binary: Path,
    cwd: Path,
    prompt: str,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    template = provider["commands"][mode]
    values = {
        "binary": str(binary),
        "cwd": str(cwd),
        "prompt": prompt,
        "model": model or str(provider.get("model_requested") or "unknown"),
        "effort": effort or str(provider.get("effort_requested") or "medium"),
    }
    return [str(part).format_map(values) for part in template]


def resolve_route(
    args: argparse.Namespace, config: dict
) -> tuple[str, str | None, str | None, str, str | None]:
    provider_id = canonical_provider_id(config, args.provider)
    model = args.model
    effort = args.effort
    seat = args.seat
    route_name: str | None = None
    if provider_id != "auto" and args.task_shape:
        raise ProviderRunError("--task-shape is valid only with provider auto")
    if provider_id == "auto":
        if not args.task_shape:
            raise ProviderRunError("provider auto requires --task-shape")
        route = route_binding(config, args.task_shape)
        route_policy = str(route.get("route_policy") or "enabled")
        if route_policy != "enabled":
            raise ProviderRunError(
                f"route {args.task_shape!r} is disabled: {route_policy}"
            )
        route_name = args.task_shape
        fixed = {
            "provider": str(route["provider"]),
            "model": str(route["model"]),
            "effort": str(route["effort"]),
            "seat": str(route["seat"]),
        }
        supplied = {"model": model, "effort": effort, "seat": seat}
        conflicts = [
            key
            for key, value in supplied.items()
            if value is not None and str(value) != fixed[key]
        ]
        if conflicts:
            raise ProviderRunError(
                "auto route fields are immutable; conflicting override(s): "
                + ", ".join(conflicts)
            )
        provider_id = fixed["provider"]
        model = fixed["model"]
        effort = fixed["effort"]
        seat = fixed["seat"]
    if not seat:
        raise ProviderRunError("--seat is required unless provider auto supplies it")
    return provider_id, model, effort, seat, route_name


def provider_family(provider_id: str, config: dict, model_id: str | None = None) -> str:
    provider_id = canonical_provider_id(config, provider_id)
    provider = config["providers"].get(provider_id)
    if provider is None:
        raise ProviderRunError(f"unknown provider: {provider_id}")
    if model_id:
        return resolve_model_family(provider, model_id)
    if provider.get("model_family_rules"):
        return "undisclosed"
    return str(provider.get("family") or provider_id)


def journal_model_family(
    provider_id: str,
    config: dict,
    model_requested: str,
    session: dict,
    health_evidence: dict,
) -> str:
    family = provider_family(provider_id, config, str(model_requested))
    if family != "undisclosed":
        return family
    if provider_id not in {"cursor", "grok"}:
        return family
    observed = str(session.get("model_observed") or "unknown")
    if observed in {"", "unknown", "auto", "auto-undisclosed"}:
        return family
    if not str(health_evidence.get("status") or "").startswith("verified-"):
        return family
    return provider_family(provider_id, config, observed)


def find_run_record(
    run_id: str,
    config: dict,
    expected_repo: str,
    *,
    allowed_modes: frozenset[str] = frozenset({"execute"}),
) -> dict:
    path = journal_path(config, expected_repo)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("run_id") != run_id:
            continue
        if row.get("repo") != expected_repo:
            raise ProviderRunError(
                "producer run repo does not match the current repository"
            )
        if row.get("run_status") != "completed" or row.get("exit_code") != 0:
            raise ProviderRunError("producer run did not complete successfully")
        if row.get("mode") not in allowed_modes:
            if allowed_modes == frozenset({"execute"}):
                raise ProviderRunError(
                    "producer run was not a write-capable execution"
                )
            raise ProviderRunError("producer run mode is not eligible for this review contract")
        return row
    raise ProviderRunError(f"producer run not found in local journal: {run_id}")


def _private_review_bundle(
    args: argparse.Namespace,
    config: dict,
    expected_repo: str,
) -> tuple[dict, list[dict]] | None:
    path_raw = getattr(args, "producer_review_bundle", None)
    if not path_raw:
        if any(
            getattr(args, field, None) is not None
            for field in (
                "producer_review_bundle_sha256",
                "orchestration_run_id",
                "orchestration_generation",
                "orchestration_fencing_token",
                "orchestration_reviewer_task_id",
                "orchestration_reviewer_attempt_id",
            )
        ):
            raise ProviderRunError("review bundle binding flags require the bundle path")
        return None
    if getattr(args, "producer_run_id", None) or getattr(args, "producer_provider", None):
        raise ProviderRunError(
            "--producer-review-bundle cannot be combined with legacy producer flags"
        )
    path = Path(path_raw).expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProviderRunError("producer review bundle is unavailable") from exc
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ProviderRunError("producer review bundle must be a mode-0600 regular file")
    raw = path.read_bytes()
    expected_sha = str(getattr(args, "producer_review_bundle_sha256", "") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or sha256_bytes(raw) != expected_sha:
        raise ProviderRunError("producer review bundle hash mismatch")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderRunError("producer review bundle is invalid JSON") from exc
    required = {
        "version", "orchestration_run_id", "generation", "fencing_token",
        "reviewer_task_id", "reviewer_attempt_id", "repo", "producers",
        "candidate",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("version") != 1:
        raise ProviderRunError("producer review bundle has an invalid schema")
    binding = {
        "orchestration_run_id": getattr(args, "orchestration_run_id", None),
        "generation": getattr(args, "orchestration_generation", None),
        "fencing_token": getattr(args, "orchestration_fencing_token", None),
        "reviewer_task_id": getattr(args, "orchestration_reviewer_task_id", None),
        "reviewer_attempt_id": getattr(args, "orchestration_reviewer_attempt_id", None),
    }
    drift = [key for key, observed in binding.items() if value.get(key) != observed]
    if drift:
        raise ProviderRunError(
            "producer review bundle fencing/identity drift: " + ", ".join(drift)
        )
    if value.get("repo") != expected_repo:
        raise ProviderRunError("producer review bundle repo does not match current repository")
    producers = value.get("producers")
    if not isinstance(producers, list) or not producers:
        raise ProviderRunError("producer review bundle requires producer references")
    expected_keys = {
        "task_id", "run_id", "provider_id", "model_observed", "model_family",
        "session_id", "session_status", "mode", "artifact_path", "artifact_sha256",
    }
    verified: list[dict] = []
    run_ids: set[str] = set()
    sessions: set[str] = set()
    identities: set[tuple[str, str, str]] = set()
    for producer in producers:
        if not isinstance(producer, dict) or set(producer) != expected_keys:
            raise ProviderRunError("producer review bundle has an invalid producer reference")
        run_id = str(producer.get("run_id") or "")
        if not run_id or run_id in run_ids:
            raise ProviderRunError("producer review bundle has duplicate or missing run identity")
        run_ids.add(run_id)
        row = find_run_record(
            run_id,
            config,
            expected_repo,
            allowed_modes=frozenset({"read-only", "execute"}),
        )
        provider_id = canonical_provider_id(config, str(row.get("provider_id") or ""))
        observed = str(row.get("model_observed") or "unknown")
        family = str(row.get("model_family") or "unknown")
        session_id = str(row.get("session_id") or "unknown")
        if provider_id in {"cursor", "grok"}:
            health = str(row.get("provider_health_evidence", {}).get("status") or "")
            session_status = str(row.get("session_status") or "unknown")
            if (
                not health.startswith("verified-")
                or session_status not in VERIFIED_BROKER_SESSION_STATUSES
            ):
                raise ProviderRunError("brokered producer model evidence is not verified")
        observed_family = provider_family(provider_id, config, observed)
        if family in {"", "unknown", "undisclosed"}:
            family = observed_family
        if (
            observed in {"", "unknown", "auto-undisclosed"}
            or family in {"", "unknown", "undisclosed"}
            or session_id == "unknown"
        ):
            raise ProviderRunError("producer review identity is not fully attributed")
        identity = (observed, session_id, family)
        if session_id in sessions or identity in identities:
            raise ProviderRunError("producer review bundle reuses a model/session/family identity")
        sessions.add(session_id)
        identities.add(identity)
        comparisons = {
            "provider_id": provider_id,
            "model_observed": observed,
            "model_family": family,
            "session_id": session_id,
            "session_status": str(row.get("session_status") or "unknown"),
            "mode": str(row.get("mode") or "unknown"),
        }
        mismatched = [key for key, observed_value in comparisons.items() if producer.get(key) != observed_value]
        if mismatched:
            raise ProviderRunError(
                "producer review bundle disagrees with provider journal: "
                + ", ".join(mismatched)
            )
        artifact_path = Path(str(producer["artifact_path"])).expanduser()
        try:
            artifact_info = artifact_path.lstat()
            artifact_bytes = artifact_path.read_bytes()
        except OSError as exc:
            raise ProviderRunError("producer review artifact is unavailable") from exc
        if (
            artifact_path.is_symlink()
            or not artifact_path.is_file()
            or stat.S_IMODE(artifact_info.st_mode) != 0o600
            or sha256_bytes(artifact_bytes) != producer["artifact_sha256"]
        ):
            raise ProviderRunError("producer review artifact type, mode, or hash drift")
        verified.append({**row, "model_family": family, "provider_id": provider_id})
    candidate = value.get("candidate")
    if not isinstance(candidate, dict) or set(candidate) != {
        "kind", "artifact_path", "artifact_sha256", "integration_head"
    }:
        raise ProviderRunError("producer review bundle candidate is invalid")
    if candidate["kind"] not in {
        "controller-integration",
        "read-only-artifact-set",
    }:
        raise ProviderRunError("producer review bundle candidate kind is invalid")
    candidate_path = Path(str(candidate["artifact_path"])).expanduser()
    try:
        candidate_info = candidate_path.lstat()
        candidate_bytes = candidate_path.read_bytes()
    except OSError as exc:
        raise ProviderRunError("producer review candidate is unavailable") from exc
    if (
        candidate_path.is_symlink()
        or not candidate_path.is_file()
        or stat.S_IMODE(candidate_info.st_mode) != 0o600
        or sha256_bytes(candidate_bytes) != candidate["artifact_sha256"]
    ):
        raise ProviderRunError("producer review candidate type, mode, or hash drift")
    try:
        candidate_record = json.loads(candidate_bytes)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderRunError("producer review candidate is invalid JSON") from exc
    if (
        not isinstance(candidate_record, dict)
        or candidate_record.get("integration_head") != candidate["integration_head"]
    ):
        raise ProviderRunError("producer review candidate integration identity drift")
    if candidate["kind"] == "controller-integration":
        integration_path = candidate_record.get("integration_path")
        if not isinstance(integration_path, str) or not Path(integration_path).is_dir():
            raise ProviderRunError("producer review integration worktree is unavailable")
        completed = subprocess.run(
            ["git", "-C", integration_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode or completed.stdout.strip() != candidate["integration_head"]:
            raise ProviderRunError("producer review integration HEAD drift")
    return value, verified


def validate_review_independence(
    route_name: str | None,
    provider_id: str,
    args: argparse.Namespace,
    config: dict,
    expected_repo: str,
) -> tuple[str, dict | None]:
    if route_name is None:
        if getattr(args, "producer_review_bundle", None):
            raise ProviderRunError(
                "producer review bundle requires a governed review route"
            )
        return "not-route-enforced", None
    route = route_binding(config, route_name)
    policy = str(route.get("review_independence") or "not-applicable")
    if policy == "not-applicable":
        return policy, None
    bundle_result = _private_review_bundle(args, config, expected_repo)
    if bundle_result is not None:
        bundle, producers = bundle_result
        route = route_binding(config, route_name)
        reviewer_family = provider_family(
            provider_id, config, str(route.get("model") or "")
        )
        producer_families: set[str] = set()
        for producer in producers:
            producer_family = str(producer["model_family"])
            producer_families.add(producer_family)
            if str(producer.get("seat") or "") == str(route.get("seat") or ""):
                raise ProviderRunError("reviewer seat must differ from every producer seat")
            if policy == "cross-family" and reviewer_family == producer_family:
                raise ProviderRunError(
                    f"route {route_name!r} requires cross-family review; "
                    f"reviewer family {reviewer_family!r} appears in producer bundle"
                )
            if policy == "independent-supplement":
                eligible = route.get("eligible_producer_routes")
                if not isinstance(eligible, list) or producer.get("route") not in eligible:
                    raise ProviderRunError(
                        f"route {route_name!r} does not allow a producer route in the bundle"
                    )
        if policy not in {"cross-family", "independent-supplement"}:
            raise ProviderRunError(f"unsupported review independence policy: {policy!r}")
        if any(producer.get("risk_overlay", {}).get("triggers") for producer in producers):
            governance_effort = str(route.get("governance_effort") or route.get("effort"))
            if policy != "cross-family" or governance_effort not in {"xhigh", "max"}:
                raise ProviderRunError(
                    "producer risk overlay requires cross-family review at xhigh/max effort"
                )
        return policy, {
            "status": "verified-private-review-bundle",
            "bundle_sha256": str(args.producer_review_bundle_sha256),
            "producer_count": len(producers),
            "run_ids": sorted(str(row["run_id"]) for row in producers),
            "model_families": sorted(producer_families),
            "session_ids": sorted(str(row["session_id"]) for row in producers),
        }
    if not args.producer_run_id:
        raise ProviderRunError(
            f"route {route_name!r} requires --producer-run-id or --producer-review-bundle"
        )
    producer = find_run_record(args.producer_run_id, config, expected_repo)
    producer_id = canonical_provider_id(config, str(producer.get("provider_id") or ""))
    if (
        args.producer_provider
        and canonical_provider_id(config, args.producer_provider) != producer_id
    ):
        raise ProviderRunError(
            "--producer-provider does not match the producer journal record"
        )
    if str(producer.get("seat") or "") == str(route.get("seat") or ""):
        raise ProviderRunError("reviewer seat must differ from the producer seat")
    if str(producer.get("session_id") or "unknown") == "unknown":
        raise ProviderRunError(
            "producer session is unknown; review independence cannot be proven"
        )
    reviewer_family = provider_family(
        provider_id, config, str(route.get("model") or "")
    )
    producer_observed = str(producer.get("model_observed") or "unknown")
    producer_family = str(producer.get("model_family") or "unknown")
    if policy == "cross-family":
        undisclosed_models = {"", "unknown", "auto-undisclosed"}
        if producer_observed in undisclosed_models:
            raise ProviderRunError(
                f"route {route_name!r} requires producer observed model identity"
            )
        if producer_id in {"cursor", "grok"}:
            health_status = str(
                producer.get("provider_health_evidence", {}).get("status") or ""
            )
            session_status = str(producer.get("session_status") or "unknown")
            if (
                not health_status.startswith("verified-")
                or session_status not in VERIFIED_BROKER_SESSION_STATUSES
            ):
                raise ProviderRunError(
                    f"route {route_name!r} requires verified model evidence "
                    "for brokered producer runs"
                )
        observed_family = provider_family(producer_id, config, producer_observed)
        if (
            producer_family not in {"", "unknown", "undisclosed"}
            and producer_family != observed_family
        ):
            raise ProviderRunError(
                "producer model family does not match the observed model identity"
            )
        producer_family = observed_family
        undisclosed_families = {"", "unknown", "undisclosed"}
        if (
            reviewer_family in undisclosed_families
            or producer_family in undisclosed_families
        ):
            raise ProviderRunError(
                f"route {route_name!r} requires disclosed model families; "
                f"reviewer={reviewer_family!r}, producer={producer_family!r}"
            )
        if reviewer_family == producer_family:
            raise ProviderRunError(
                f"route {route_name!r} requires cross-family review; "
                f"both resolve to {reviewer_family!r}"
            )
    if policy not in {"cross-family", "independent-supplement"}:
        raise ProviderRunError(f"unsupported review independence policy: {policy!r}")
    if policy == "independent-supplement":
        eligible_routes = route.get("eligible_producer_routes")
        if (
            not isinstance(eligible_routes, list)
            or not eligible_routes
            or any(not isinstance(item, str) for item in eligible_routes)
        ):
            raise ProviderRunError(
                f"route {route_name!r} has invalid eligible producer route policy"
            )
        producer_route = str(producer.get("route") or "unknown")
        if producer_route not in eligible_routes:
            raise ProviderRunError(
                f"route {route_name!r} does not allow producer route "
                f"{producer_route!r}; expected an eligible producer route"
            )
    producer_risks = list(producer.get("risk_overlay", {}).get("triggers", []))
    governance_effort = str(route.get("governance_effort") or route.get("effort"))
    if producer_risks and (
        policy != "cross-family" or governance_effort not in {"xhigh", "max"}
    ):
        raise ProviderRunError(
            "producer risk overlay requires cross-family review at xhigh/max effort"
        )
    return policy, {
        "run_id": args.producer_run_id,
        "provider_id": producer_id,
        "seat": str(producer.get("seat") or "unknown"),
        "session_id": str(producer.get("session_id") or "unknown"),
        "model_family": producer_family,
    }


def validate_risk_overlay(
    args: argparse.Namespace,
    route_name: str | None,
    effort: str | None,
    review_independence: str,
    config: dict,
) -> dict:
    requested = list(dict.fromkeys(args.risk_trigger or []))
    try:
        canon = routing_canon(config)
        allowed = set(map(str, canon["risk_overlays"]["triggers"]))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ProviderRunError(f"cannot load routing risk canon: {exc}") from exc
    unknown = sorted(set(requested) - allowed)
    if unknown:
        raise ProviderRunError("unknown risk trigger(s): " + ", ".join(unknown))
    if not requested:
        return {"triggers": [], "status": "not-applied"}
    if route_name == "restricted_zone":
        return {
            "triggers": requested,
            "status": "direction-ratcheted",
            "required_review_effort_floor": "xhigh",
            "required_review_independence": "cross-family",
        }
    if review_independence != "not-applicable":
        if review_independence != "cross-family" or effort not in {"xhigh", "max"}:
            raise ProviderRunError(
                "risk-overlay final review requires cross-family independence and xhigh effort"
            )
        return {"triggers": requested, "status": "review-overlay-satisfied"}
    raise ProviderRunError(
        "risk trigger requires --task-shape restricted_zone or a compliant final-review route"
    )


def provider_health_evidence(
    provider_id: str, requested_model: str, session: dict
) -> dict:
    if provider_id == "cursor":
        if (
            session.get("session_id") == "unknown"
            or session.get("session_ref") == "unknown"
        ):
            return {"status": "unverified", "reason": "session-unattributed"}
        observed = str(session.get("model_observed") or "unknown")
        stream_identity = (
            str(session.get("model_observation_reason") or "")
            == "cursor-stream-json"
        )
        if requested_model == "auto":
            if observed in {"", "unknown"}:
                return {"status": "unverified", "reason": "model-unobserved"}
            if observed in {"auto", "auto-undisclosed"}:
                return {
                    "status": "verified-native-session-model-opaque",
                    "model_observed": observed,
                }
            return {
                "status": (
                    "verified-stream-session-model"
                    if stream_identity
                    else "verified-native-session-model"
                ),
                "model_observed": observed,
            }
        if observed == requested_model:
            return {
                "status": (
                    "verified-stream-session-model"
                    if stream_identity
                    else "verified-native-session-model"
                ),
                "model_observed": observed,
            }
        return {
            "status": "unverified",
            "reason": "native-session-model-mismatch",
            "model_observed": observed,
        }
    if provider_id != "grok":
        return {"status": "not-applicable"}
    if (
        session.get("session_id") == "unknown"
        or session.get("session_ref") == "unknown"
    ):
        return {"status": "unverified", "reason": "session-unattributed"}
    root = expand(str(session["session_ref"]))
    try:
        summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        signals = json.loads((root / "signals.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"status": "unverified", "reason": "native-health-files-unreadable"}
    observed = str(summary.get("current_model_id") or "unknown")
    primary = str(signals.get("primaryModelId") or "unknown")
    errors = signals.get("errorCount")
    if observed != requested_model or primary != requested_model or errors != 0:
        return {
            "status": "unverified",
            "reason": "primary-turn-mismatch",
            "model_observed": observed,
            "primary_model": primary,
            "session_error_count": errors if isinstance(errors, int) else "unknown",
        }
    return {
        "status": "verified-primary-session",
        "model_observed": observed,
        "primary_model": primary,
        "session_error_count": 0,
    }


def scrub_environment(provider: dict) -> tuple[dict[str, str], list[str]]:
    env = dict(os.environ)
    stripped: list[str] = []
    for key in provider.get("strip_environment", []):
        if key in env:
            stripped.append(key)
            env.pop(key, None)
    return env, stripped


def classify_failure(
    run_status: str,
    exit_code: int,
    stderr: str,
    timeout_class: str | None = None,
    stdout: str = "",
) -> str:
    combined = f"{stdout}\n{stderr}".lower()
    if run_status == "timed-out":
        return timeout_class or "timeout"
    if run_status == "interrupted":
        return "interrupted"
    if run_status in {
        "provider-health-unverified",
        "review-independence-violation",
        "provider-start-failed",
        "serial-lock-timeout",
    }:
        return run_status
    if exit_code != 0 and (
        "402" in combined
        or "spending-limit" in combined
        or "run out of credits" in combined
        or "quota exceeded" in combined
        or "quota exhausted" in combined
        or "insufficient credits" in combined
    ):
        return "quota-exhausted"
    if exit_code != 0 and "429" in combined and "free-usage-exhausted" in combined:
        return "quota-exhausted"
    if exit_code != 0 and "429" in combined:
        return "rate-limited"
    if exit_code != 0 and (
        "529" in combined
        or "overloaded" in combined
        or "upstream overload" in combined
    ):
        return "upstream-overload"
    if exit_code != 0 and (
        "401" in combined
        or "unauthorized" in combined
        or "authentication required" in combined
        or "invalid api key" in combined
        or "auth expired" in combined
        or "token expired" in combined
        or "login required" in combined
        or "not logged in" in combined
    ):
        return "auth-expired"
    if exit_code != 0 and (
        "timed out" in combined
        or "timeout" in combined
        or "deadline exceeded" in combined
    ):
        return "timeout"
    if exit_code != 0 and (
        "review data policy" in combined
        or ("actionrequirederror" in combined and "retention policy" in combined)
    ):
        return "action-required-data-policy"
    if exit_code != 0:
        return "provider-error"
    return "none"


def kill_process_tree(proc: subprocess.Popen[str]) -> None:
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        if proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, AttributeError):
                return
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return
    # start_new_session=True on all provider spawns: leader pid is the PGID.
    # Attempt killpg even when poll() != None (leader exited but children remain).
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def serial_lock_path(group: str, journal_root: Path | None = None) -> Path:
    root = journal_root or expand("~/.agent-runs")
    return root / "locks" / f"{group}.lock"


class ProviderSerialLock:
    def __init__(
        self,
        group: str,
        *,
        journal_root: Path | None = None,
        wait_seconds: int = SERIAL_LOCK_WAIT_SECONDS,
    ):
        self.group = group
        self.journal_root = journal_root
        self.wait_seconds = wait_seconds
        self._handle = None
        self.acquired = False
        self.telemetry = {
            "group": group,
            "status": "pending",
            "wait_started_at": None,
            "acquired_at": None,
            "wait_ms": None,
            "wait_timeout_seconds": wait_seconds,
        }

    def __enter__(self) -> ProviderSerialLock:
        path = serial_lock_path(self.group, self.journal_root)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._handle = open(path, "a+", encoding="utf-8")
        self.telemetry["wait_started_at"] = utc_now()
        started = time.monotonic()
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                self.telemetry.update(
                    {
                        "status": "acquired",
                        "acquired_at": utc_now(),
                        "wait_ms": round((time.monotonic() - started) * 1000),
                    }
                )
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.telemetry.update(
                        {
                            "status": "timed-out",
                            "wait_ms": round((time.monotonic() - started) * 1000),
                        }
                    )
                    self._handle.close()
                    self._handle = None
                    raise SerialLockTimeout(
                        f"serial lock wait exceeded for group {self.group!r}"
                    ) from None
                time.sleep(0.25)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is not None:
            if self.acquired:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
            self.acquired = False


def serial_group_for_provider(
    provider_id: str, route: dict | None = None
) -> str | None:
    if route is not None:
        if route.get("serial_group"):
            return str(route["serial_group"])
        return None
    return PROVIDER_SERIAL_GROUPS.get(provider_id)


def effective_timeout_seconds(
    args: argparse.Namespace, route_name: str | None, config: dict
) -> int:
    requested = getattr(args, "timeout_seconds", None)
    if requested is not None:
        return int(requested)
    if route_name:
        timeout = route_binding(config, route_name).get("timeout_seconds")
        if timeout is not None:
            return int(timeout)
    return DEFAULT_RUN_TIMEOUT_SECONDS


def extract_claude_session_from_events(events: list[dict]) -> str | None:
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()
    return None


def extract_codex_session_from_events(events: list[dict]) -> str | None:
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str) and thread_id.strip():
            return thread_id.strip()
    return None


def _cursor_stream_event_identity(event: dict) -> tuple[str | None, str | None]:
    """Return only explicit Cursor stream identities from init/result events.

    Cursor's human-readable model label is deliberately not an identity.  The
    caller validates the returned raw model against the current `cursor-agent
    models` catalog before it may be used for health or review provenance.
    """
    event_type = event.get("type")
    if event_type == "system":
        if event.get("subtype") != "init":
            return None, None
    elif event_type != "result":
        return None, None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None, None
    # Prefer fields whose contract is an ID. `model` is retained as a
    # fail-closed fallback: it must still exactly match a catalog ID below.
    model = None
    for key in ("model_id", "modelId", "current_model_id", "model"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            model = value.strip()
            break
    return session_id.strip(), model


def extract_cursor_stream_identity(
    events: list[dict], model_catalog: list[dict],
) -> dict[str, str] | None:
    """Extract one exact Cursor session/model pair or return no attribution.

    The stream must name a single session across its init/result records.  A
    reported model may be either an exact catalog ID or an exact, unique label
    from that same live catalog.  Cursor currently emits display labels in its
    init event and omits model on the result event, so no heuristic string
    normalization is allowed or needed.
    """
    allowed_ids = {
        str(row.get("id"))
        for row in model_catalog
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    }
    label_ids: dict[str, set[str]] = {}
    for row in model_catalog:
        if not isinstance(row, dict):
            continue
        model_id, label = row.get("id"), row.get("label")
        if (
            isinstance(model_id, str)
            and model_id
            and isinstance(label, str)
            and label
        ):
            label_ids.setdefault(label, set()).add(model_id)
    session_ids: set[str] = set()
    models: set[str] = set()
    saw_identity_event = False
    for event in events:
        if not isinstance(event, dict):
            continue
        session_id, raw_model = _cursor_stream_event_identity(event)
        if session_id is None:
            continue
        saw_identity_event = True
        session_ids.add(session_id)
        if raw_model is None:
            continue
        if raw_model in allowed_ids:
            models.add(raw_model)
            continue
        label_matches = label_ids.get(raw_model, set())
        if len(label_matches) != 1:
            return None
        models.add(next(iter(label_matches)))
    if not saw_identity_event or len(session_ids) != 1 or len(models) != 1:
        return None
    return {"session_id": next(iter(session_ids)), "model_observed": next(iter(models))}


def configure_claude_stream_json(provider_id: str, command: list[str]) -> bool:
    if provider_id != "claude" or "--output-format" not in command:
        return False
    command[command.index("--output-format") + 1] = "stream-json"
    if "--verbose" not in command:
        command.insert(command.index("--output-format"), "--verbose")
    return True


def configure_cursor_stream_json(provider_id: str, command: list[str]) -> bool:
    if provider_id != "cursor":
        return False
    if "--output-format" in command:
        command[command.index("--output-format") + 1] = "stream-json"
        return True
    # The prompt is the final argument in the manifest command. Keeping it
    # final prevents a provider option from ever being parsed as prompt text.
    command[-1:-1] = ["--output-format", "stream-json"]
    return True


def stream_session_record(
    adapter: str,
    session_id: str,
    artifacts: dict[str, tuple[int, int]],
) -> dict:
    lowered = session_id.lower()
    for raw_path in artifacts:
        path = Path(raw_path)
        if lowered not in path.name.lower():
            continue
        parsed = parse_session(adapter, path, "attributed-stream-json")
        if str(parsed.get("session_id") or "") == session_id:
            return parsed
    return parse_session(adapter, None, "attributed-stream-json")


def cursor_stream_session_record(
    session_id: str,
    model_observed: str,
    artifacts: dict[str, tuple[int, int]],
) -> dict:
    """Build a stream-backed Cursor session record without global diff choice."""
    record = stream_session_record("cursor", session_id, artifacts)
    record["session_id"] = session_id
    if record.get("session_ref") == "unknown":
        # This is an opaque local pointer, not a filesystem path. It makes the
        # source of the identity explicit without pretending an artifact was
        # correlated when concurrent Cursor runs did write several artifacts.
        record["session_ref"] = f"stream-json:{session_id}"
    record["model_observed"] = model_observed
    record["model_observation_reason"] = "cursor-stream-json"
    record["session_status"] = "attributed-stream-json"
    record["session_attribution"] = "stream-json"
    return record


def validate_checkpoint(slug: str, event_id: str | None, expected_seat: str) -> dict:
    if not event_id:
        raise ProviderRunError(
            "governed route/write execution requires --checkpoint-event"
        )
    path = Path.home() / ".agent-ledger" / f"{slug}.jsonl"
    if not path.is_file():
        raise ProviderRunError(f"checkpoint ledger not found for repo {slug!r}")
    events: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProviderRunError(f"cannot read checkpoint ledger: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ProviderRunError(
                f"malformed checkpoint ledger row {line_number}: invalid JSON"
            ) from exc
        if not isinstance(row, dict) or set(row) != set(LEDGER_FIELDS):
            raise ProviderRunError(
                f"malformed checkpoint ledger row {line_number}: expected exact 10-field schema"
            )
        events.append(row)
    try:
        state = checkpoint_state(events, event_id)
    except (ValueError, LookupError) as exc:
        raise ProviderRunError(f"malformed checkpoint ledger: {exc}") from exc
    if not state["active"]:
        raise ProviderRunError(
            f"checkpoint event is not currently claimed/open: {event_id}"
        )
    if state["owner"] != expected_seat:
        raise ProviderRunError(
            f"checkpoint owner {state['owner']!r} does not match run seat {expected_seat!r}; record a handoff/claim first"
        )
    return {
        "event_id": event_id,
        "owner": state["owner"],
        "intent_ref": state["intent_ref"],
        "ledger_ref": portable_ref(path),
    }


def journal_path(config: dict, slug: str) -> Path:
    return expand(config["journal"]["root"]) / f"{slug}.jsonl"


def append_journal(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(
            fd,
            (
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode(),
        )
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def sanitized_skill_evidence(selection: dict) -> dict:
    def clean(row: dict) -> dict:
        keys = (
            "name",
            "digest",
            "content_sha256",
            "source_group",
            "call_policy",
            "selection_source",
        )
        return {key: row[key] for key in keys if key in row}

    return {
        "routing_status": selection.get("routing_status", "active"),
        "available": {
            "count": selection["available_count"],
            "manifest_sha256": selection["manifest_sha256"],
        },
        "selected": [clean(row) for row in selection["chosen"]],
        "exposed": [
            dict(clean(row), evidence="wrapper-embedded-content")
            for row in selection["chosen"]
        ],
        "read_or_invoked": [
            {"name": row["name"], "status": "unknown"} for row in selection["chosen"]
        ],
        "deferred_by_policy": [clean(row) for row in selection["deferred"]],
    }



def empty_stage_telemetry() -> dict:
    return {
        "process_started_at": None,
        "first_provider_event_at": None,
        "last_progress_at": None,
        "turn_completed_at": None,
        "provider_event_count": 0,
        "timeout_class": None,
        "stream_mode": "capture",
    }


def extract_codex_agent_message(events: list[dict]) -> str:
    texts: list[str] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agent_message":
            value = item.get("text")
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts[-1] if texts else ""


def extract_claude_agent_message(events: list[dict]) -> str:
    results: list[str] = []
    assistant_texts: list[str] = []
    for event in events:
        if event.get("type") == "result":
            value = event.get("result")
            if isinstance(value, str) and value.strip():
                results.append(value)
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            value = block.get("text")
            if isinstance(value, str) and value.strip():
                assistant_texts.append(value)
    if results:
        return results[-1]
    return "\n".join(assistant_texts)


def extract_claude_model_from_events(events: list[dict]) -> str:
    last = ""
    for event in events:
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        if isinstance(model, str) and model.strip():
            last = model.strip()
    return last or "unknown"


def extract_codex_model_from_events(events: list[dict]) -> str:
    last = ""
    for event in events:
        for key in ("model", "current_model_id"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                last = value.strip()
        item = event.get("item")
        if isinstance(item, dict):
            value = item.get("model")
            if isinstance(value, str) and value.strip():
                last = value.strip()
    return last or "unknown"


def run_blocking_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[subprocess.CompletedProcess, str, dict]:
    telemetry = empty_stage_telemetry()
    telemetry["process_started_at"] = utc_now()
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        run_status = "completed"
        completed = subprocess.CompletedProcess(
            command, proc.returncode if proc.returncode is not None else 0, stdout, stderr
        )
    except subprocess.TimeoutExpired as exc:
        run_status = "timed-out"
        telemetry["timeout_class"] = "timeout_total"
        if proc is not None:
            kill_process_tree(proc)
        stdout = (
            exc.stdout.decode()
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            (exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
            + "\nprovider-timeout"
        )
        completed = subprocess.CompletedProcess(command, 124, stdout, stderr)
    except KeyboardInterrupt:
        run_status = "interrupted"
        if proc is not None:
            kill_process_tree(proc)
        completed = subprocess.CompletedProcess(
            command, 130, stdout="", stderr="provider-interrupted"
        )
    return completed, run_status, telemetry


def run_claude_stream_json_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[subprocess.CompletedProcess, str, dict, list[dict]]:
    """Stream Claude --output-format stream-json; classify total timeout."""
    telemetry = empty_stage_telemetry()
    telemetry["stream_mode"] = "claude-stream-json"
    telemetry["total_budget_seconds"] = float(timeout_seconds)
    events: list[dict] = []
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    run_status = "completed"
    started = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        telemetry["process_started_at"] = utc_now()
        assert proc.stdout is not None
        assert proc.stderr is not None
        import selectors

        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        stdout_open = True
        stderr_open = True
        while stdout_open or stderr_open or proc.poll() is None:
            if time.monotonic() - started >= timeout_seconds:
                run_status = "timed-out"
                telemetry["timeout_class"] = "timeout_total"
                break
            ready = selector.select(timeout=0.25)
            if not ready:
                if proc.poll() is not None and not stdout_open and not stderr_open:
                    break
                continue
            for key, _mask in ready:
                stream = key.fileobj
                label = key.data
                line = stream.readline()
                if line == "":
                    selector.unregister(stream)
                    if label == "stdout":
                        stdout_open = False
                    else:
                        stderr_open = False
                    continue
                if label == "stderr":
                    stderr_chunks.append(line)
                    continue
                stdout_chunks.append(line)
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
        if run_status == "timed-out":
            kill_process_tree(proc)
        elif proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc)
        else:
            proc.wait(timeout=5)
        exit_code = proc.returncode if proc.returncode is not None else 124
        display = extract_claude_agent_message(events)
        raw = "".join(stdout_chunks)
        completed = subprocess.CompletedProcess(
            command,
            exit_code if run_status != "timed-out" else 124,
            stdout=display if display else raw,
            stderr="".join(stderr_chunks),
        )
        telemetry.pop("_last_progress_mono", None)
        return completed, run_status, telemetry, events
    except KeyboardInterrupt:
        if proc is not None:
            kill_process_tree(proc)
        telemetry.pop("_last_progress_mono", None)
        completed = subprocess.CompletedProcess(
            command, 130, stdout="", stderr="provider-interrupted"
        )
        return completed, "interrupted", telemetry, events
    except OSError as exc:
        telemetry.pop("_last_progress_mono", None)
        completed = subprocess.CompletedProcess(
            command, 127, stdout="", stderr=f"provider-start-failed: {exc}"
        )
        return completed, "provider-start-failed", telemetry, events


def run_cursor_stream_json_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[subprocess.CompletedProcess, str, dict, list[dict]]:
    """Run Cursor's native stream-json format with the same bounded reader."""
    proc, run_status, telemetry, events = run_claude_stream_json_process(
        command, cwd=cwd, env=env, timeout_seconds=timeout_seconds
    )
    telemetry["stream_mode"] = "cursor-stream-json"
    return proc, run_status, telemetry, events


def run_codex_json_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    first_event_seconds: int | None = None,
    idle_seconds: int | None = None,
) -> tuple[subprocess.CompletedProcess, str, dict, list[dict]]:
    """Stream Codex --json events; classify startup/first-event/idle/total timeouts."""
    first_budget = float(
        first_event_seconds
        if first_event_seconds is not None
        else min(60, max(5, timeout_seconds))
    )
    idle_budget = float(
        idle_seconds if idle_seconds is not None else min(120, max(10, timeout_seconds))
    )
    total_budget = float(timeout_seconds)
    telemetry = empty_stage_telemetry()
    telemetry["stream_mode"] = "codex-json"
    telemetry["first_event_budget_seconds"] = first_budget
    telemetry["idle_budget_seconds"] = idle_budget
    telemetry["total_budget_seconds"] = total_budget
    events: list[dict] = []
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    run_status = "completed"
    started = time.monotonic()
    proc: subprocess.Popen[str] | None = None

    def mark_progress(now: float) -> None:
        stamp = utc_now()
        if telemetry["first_provider_event_at"] is None:
            telemetry["first_provider_event_at"] = stamp
        telemetry["last_progress_at"] = stamp
        telemetry["provider_event_count"] = int(telemetry["provider_event_count"]) + 1

    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        telemetry["process_started_at"] = utc_now()
        assert proc.stdout is not None
        assert proc.stderr is not None
        import selectors

        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        stdout_open = True
        stderr_open = True
        while stdout_open or stderr_open or proc.poll() is None:
            now = time.monotonic()
            elapsed = now - started
            if telemetry["first_provider_event_at"] is None and elapsed >= first_budget:
                run_status = "timed-out"
                telemetry["timeout_class"] = "timeout_first_event"
                break
            if (
                telemetry["first_provider_event_at"] is not None
                and telemetry["turn_completed_at"] is None
                and telemetry["last_progress_at"] is not None
            ):
                # Approximate idle from monotonic using event cadence via wall isn't stored;
                # track last progress monotonic separately.
                pass
            last_progress_mono = telemetry.get("_last_progress_mono")
            if (
                isinstance(last_progress_mono, (int, float))
                and telemetry["turn_completed_at"] is None
                and (now - float(last_progress_mono)) >= idle_budget
            ):
                run_status = "timed-out"
                telemetry["timeout_class"] = "timeout_idle"
                break
            if elapsed >= total_budget:
                run_status = "timed-out"
                telemetry["timeout_class"] = "timeout_total"
                break
            wait = 0.25
            if telemetry["first_provider_event_at"] is None:
                wait = min(wait, max(0.05, first_budget - elapsed))
            elif isinstance(last_progress_mono, (int, float)):
                wait = min(wait, max(0.05, idle_budget - (now - float(last_progress_mono))))
            wait = min(wait, max(0.05, total_budget - elapsed))
            ready = selector.select(timeout=wait)
            if not ready:
                if proc.poll() is not None and not stdout_open and not stderr_open:
                    break
                continue
            for key, _mask in ready:
                stream = key.fileobj
                label = key.data
                line = stream.readline()
                if line == "":
                    selector.unregister(stream)
                    if label == "stdout":
                        stdout_open = False
                    else:
                        stderr_open = False
                    continue
                if label == "stderr":
                    stderr_chunks.append(line)
                    continue
                stdout_chunks.append(line)
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                events.append(event)
                mark_progress(time.monotonic())
                telemetry["_last_progress_mono"] = time.monotonic()
                event_type = str(event.get("type") or "")
                if event_type == "turn.completed":
                    telemetry["turn_completed_at"] = utc_now()
                    # Final answer observed; stop streaming and reap the process.
                    stdout_open = False
                    stderr_open = False
                    try:
                        selector.unregister(proc.stdout)
                    except Exception:
                        pass
                    try:
                        selector.unregister(proc.stderr)
                    except Exception:
                        pass
                    break
        if run_status == "timed-out":
            kill_process_tree(proc)
        elif proc.poll() is None:
            try:
                proc.wait(timeout=max(1.0, total_budget - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                run_status = "timed-out"
                telemetry["timeout_class"] = "timeout_total"
                kill_process_tree(proc)
        # Drain remaining stderr
        if proc.stderr is not None:
            try:
                remainder = proc.stderr.read() or ""
                if remainder:
                    stderr_chunks.append(remainder)
            except ValueError:
                pass
        returncode = 124 if run_status == "timed-out" else int(proc.returncode or 0)
        display = extract_codex_agent_message(events)
        raw = "".join(stdout_chunks)
        completed = subprocess.CompletedProcess(
            command,
            returncode,
            stdout=display if display else raw,
            stderr="".join(stderr_chunks)
            + ("\nprovider-timeout" if run_status == "timed-out" else ""),
        )
        telemetry.pop("_last_progress_mono", None)
        return completed, run_status, telemetry, events
    except KeyboardInterrupt:
        if proc is not None:
            kill_process_tree(proc)
        telemetry.pop("_last_progress_mono", None)
        completed = subprocess.CompletedProcess(
            command, 130, stdout="", stderr="provider-interrupted"
        )
        return completed, "interrupted", telemetry, events
    except OSError as exc:
        # Spawn failure is not a timeout stage; keep timeout_* for real deadlines.
        telemetry["timeout_class"] = None
        telemetry.pop("_last_progress_mono", None)
        completed = subprocess.CompletedProcess(
            command, 127, stdout="", stderr=f"provider-start-failed: {exc}"
        )
        return completed, "provider-start-failed", telemetry, events


def classify_route_status(blockers: list[dict]) -> str:
    if not blockers:
        return "ready"
    codes = {str(row.get("code") or "") for row in blockers}
    if "route-policy-disabled" in codes or "provider-disabled" in codes:
        return "disabled"
    soft = {"live-evidence-unverified"}
    if codes and codes <= soft:
        return "degraded"
    return "blocked"


def validate_route_concurrency(canon: dict, route_name: str, binding: dict) -> str:
    raw_route = canon["runtime_routes"].get(route_name)
    concurrency = raw_route.get("concurrency") if isinstance(raw_route, dict) else None
    if concurrency not in {"family_serial", "explicitly_parallel"}:
        raise ProviderRunError(f"route {route_name!r} has invalid concurrency declaration")
    serial_group = serial_group_for_provider(str(binding["provider"]), binding)
    if concurrency == "family_serial" and not serial_group:
        raise ProviderRunError(f"route {route_name!r} requires a serial_group")
    if concurrency == "explicitly_parallel" and serial_group:
        raise ProviderRunError(
            f"route {route_name!r} declares parallel execution but has serial_group"
        )
    return concurrency


def apply_workspace_trust(
    provider: dict,
    command: list[str],
    *,
    governed_route: bool,
    explicit_trust: bool,
) -> str:
    if provider.get("requires_workspace_trust"):
        if not governed_route and not explicit_trust:
            raise ProviderRunError(
                "Cursor headless mode requires explicit --trust-workspace"
            )
        command.insert(-1, "--trust")
        return "governed-route-binding" if governed_route else "explicit-cli"
    if explicit_trust:
        raise ProviderRunError(
            "--trust-workspace is only valid for providers that require it"
        )
    return "not-required"


def run_provider(args: argparse.Namespace, config: dict) -> int:
    provider_id, model, effort, seat, route_name = resolve_route(args, config)
    if not SEAT_RE.fullmatch(seat or ""):
        raise ProviderRunError(
            "seat must use the checkpoint vocabulary, e.g. codex-landing"
        )
    if args.mode == "execute" and not args.allow_write:
        raise ProviderRunError("execute mode requires explicit --allow-write")
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        raise ProviderRunError("--timeout-seconds must be positive")
    timeout_seconds = effective_timeout_seconds(args, route_name, config)
    if args.prompt.lstrip().startswith("-"):
        raise ProviderRunError(
            "prompt must not begin with '-' (prefix it with context text)"
        )
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ProviderRunError(f"cwd is not a directory: {cwd}")
    provider_id = canonical_provider_id(config, provider_id)
    provider = config["providers"].get(provider_id)
    if provider is None:
        raise ProviderRunError(f"unknown provider: {provider_id}")
    run_policy = str(provider.get("run_policy") or "enabled")
    if run_policy != "enabled":
        raise ProviderRunError(
            f"provider {provider_id!r} is disabled by run_policy: {run_policy}"
        )
    slug = repo_slug(cwd)
    review_independence, producer_ref = validate_review_independence(
        route_name, provider_id, args, config, slug
    )
    route = route_binding(config, route_name) if route_name else {}
    if route_name is not None:
        validate_route_concurrency(routing_canon(config), route_name, route)
    governance_effort = str(
        route.get("governance_effort") or effort or "provider-default"
    )
    risk_overlay = validate_risk_overlay(
        args, route_name, governance_effort, review_independence, config
    )
    checkpoint = None
    if route_name is not None or args.mode == "execute" or args.checkpoint_event:
        checkpoint = validate_checkpoint(slug, args.checkpoint_event, seat)
    model = model or str(provider.get("model_requested") or "unknown")
    effort = effort or provider.get("effort_requested")
    binary = resolve_binary(provider)
    model_catalog = validate_provider_model(provider_id, provider, binary, str(model))
    if effort is not None and effort not in set(
        map(str, provider.get("effort_options", [effort]))
    ):
        raise ProviderRunError(
            f"effort {effort!r} is not allowed for provider {provider_id}"
        )
    if args.no_skills:
        # --no-skills must work without a local skill-governance install (CI).
        selection = {
            "manifest_sha256": sha256_text(""),
            "available_count": 0,
            "chosen": [],
            "deferred": [],
            "entries": {},
            "routing_status": "explicitly-disabled-for-run",
        }
    else:
        selection = select_skills(args.prompt, cwd, args.skill, config)
        if route_name is not None and selection.get("routing_status") != "ok":
            raise ProviderRunError(
                f"skill router is degraded for governed route: {selection.get('routing_status')}"
            )
    managed_prompt = augment_prompt(
        args.prompt, selection, int(config["skills"].get("max_embedded_bytes", 100_000))
    )
    command = build_command(
        provider, args.mode, binary, cwd, managed_prompt, model, effort
    )
    workspace_trust_source = apply_workspace_trust(
        provider,
        command,
        governed_route=route_name is not None,
        explicit_trust=bool(args.trust_workspace),
    )
    if args.minimal_runtime:
        if provider_id != "codex":
            raise ProviderRunError(
                "--minimal-runtime is currently supported only for Codex"
            )
        command.insert(-1, "--ignore-user-config")
    if args.no_provider_tools:
        if provider_id != "claude" or "--tools" not in command:
            raise ProviderRunError(
                "--no-provider-tools is currently supported only for Claude read-only runs"
            )
        command[command.index("--tools") + 1] = ""
    use_claude_stream = configure_claude_stream_json(provider_id, command)
    use_cursor_stream = configure_cursor_stream_json(provider_id, command)
    env, stripped_env = scrub_environment(provider)
    before: dict[str, tuple[int, int]] = {}
    after: dict[str, tuple[int, int]] = {}
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    started = time.monotonic()
    stream_events: list[dict] = []
    serial_group = serial_group_for_provider(provider_id, route if route_name else None)
    lock_cm = (
        ProviderSerialLock(
            serial_group,
            journal_root=expand(config["journal"]["root"]),
            wait_seconds=min(timeout_seconds, SERIAL_LOCK_WAIT_SECONDS),
        )
        if serial_group
        else contextlib.nullcontext()
    )
    try:
        with lock_cm:
            # Snapshot only after the family lock is held. Otherwise artifacts
            # from the run ahead of us in the queue pollute file-diff fallback.
            before = session_snapshot(provider)
            if provider_id == "codex" and "--json" in command:
                proc, run_status, stage_telemetry, stream_events = run_codex_json_process(
                    command,
                    cwd=cwd,
                    env=env,
                    timeout_seconds=timeout_seconds,
                )
            elif use_claude_stream:
                proc, run_status, stage_telemetry, stream_events = run_claude_stream_json_process(
                    command,
                    cwd=cwd,
                    env=env,
                    timeout_seconds=timeout_seconds,
                )
            elif use_cursor_stream:
                proc, run_status, stage_telemetry, stream_events = run_cursor_stream_json_process(
                    command,
                    cwd=cwd,
                    env=env,
                    timeout_seconds=timeout_seconds,
                )
            else:
                proc, run_status, stage_telemetry = run_blocking_process(
                    command,
                    cwd=cwd,
                    env=env,
                    timeout_seconds=timeout_seconds,
                )
            # Capture the post-run artifact state before releasing the family
            # lock, so the next queued run cannot contaminate attribution.
            after = session_snapshot(provider)
    except SerialLockTimeout as exc:
        run_status = "serial-lock-timeout"
        stage_telemetry = empty_stage_telemetry()
        proc = subprocess.CompletedProcess(
            command, 75, stdout="", stderr=str(exc)
        )
    if serial_group:
        stage_telemetry["serial_lock"] = dict(lock_cm.telemetry)
    adapter = str(provider["session"].get("adapter") or "")
    cursor_stream_identity = (
        extract_cursor_stream_identity(
            stream_events, list(model_catalog.get("models") or [])
        )
        if provider_id == "cursor"
        else None
    )
    if run_status == "serial-lock-timeout":
        session = parse_session(adapter, None, "not-observed")
        changed_artifact_count = 0
    elif cursor_stream_identity is not None:
        # A single native stream identity wins before any global Cursor state
        # diff is considered. This is what makes same-provider parallel runs
        # attributable instead of merely correlatable.
        session = cursor_stream_session_record(
            cursor_stream_identity["session_id"],
            cursor_stream_identity["model_observed"],
            after,
        )
        changed_artifact_count = 0
    else:
        session, changed_artifact_count = attribute_session(
            adapter, before, after, requested_model=str(model)
        )
        if (
            adapter == "cursor"
            and proc.returncode == 0
            and "ambiguous" in str(session.get("session_status") or "")
        ):
            session, changed_artifact_count, after, settle_telemetry = (
                settle_cursor_attribution(
                    provider=provider,
                    before=before,
                    after=after,
                    requested_model=str(model),
                    overall_started=started,
                    timeout_seconds=timeout_seconds,
                )
            )
            stage_telemetry["session_attribution_settle"] = settle_telemetry
    duration_ms = round((time.monotonic() - started) * 1000)
    ended_at = utc_now()
    session_attribution = "file-diff"
    stream_session_id = None
    if provider_id == "claude":
        stream_session_id = extract_claude_session_from_events(stream_events)
    elif provider_id == "codex":
        stream_session_id = extract_codex_session_from_events(stream_events)
    elif provider_id == "cursor" and cursor_stream_identity is not None:
        stream_session_id = cursor_stream_identity["session_id"]
    if stream_session_id:
        if cursor_stream_identity is not None:
            session = cursor_stream_session_record(
                stream_session_id,
                cursor_stream_identity["model_observed"],
                after,
            )
        else:
            session = stream_session_record(adapter, stream_session_id, after)
            session["session_id"] = stream_session_id
            session["session_status"] = "attributed-stream-json"
            session["session_attribution"] = "stream-json"
        session_attribution = "stream-json"
    if session_attribution != "stream-json":
        status = str(session.get("session_status") or "")
        if "ambiguous" in status:
            session_attribution = "ambiguous"
        else:
            session_attribution = "file-diff"
        session = dict(session)
        session["session_attribution"] = session_attribution
    health_evidence = provider_health_evidence(provider_id, str(model), session)
    if (
        route_name is not None
        and provider_id == "grok"
        and proc.returncode == 0
        and health_evidence.get("status") != "verified-primary-session"
    ):
        run_status = "provider-health-unverified"
        proc = subprocess.CompletedProcess(
            command, 3, stdout=proc.stdout, stderr=proc.stderr
        )
    if (
        provider.get("require_named_model_health")
        and str(model) != "auto"
        and proc.returncode == 0
        and not str(health_evidence.get("status") or "").startswith("verified-")
    ):
        run_status = "provider-health-unverified"
        proc = subprocess.CompletedProcess(
            command, 3, stdout=proc.stdout, stderr=proc.stderr
        )
    producer_sessions = (
        set(map(str, producer_ref.get("session_ids", [])))
        if producer_ref is not None
        else set()
    )
    if producer_ref is not None and "session_id" in producer_ref:
        producer_sessions.add(str(producer_ref["session_id"]))
    if producer_ref is not None and (
        session["session_id"] == "unknown"
        or session["session_id"] in producer_sessions
    ):
        run_status = "review-independence-violation"
        proc = subprocess.CompletedProcess(
            command, 3, stdout=proc.stdout, stderr=proc.stderr
        )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    failure_class = classify_failure(
        run_status,
        proc.returncode,
        stderr,
        timeout_class=stage_telemetry.get("timeout_class"),
        stdout=stdout,
    )
    if (
        provider_id in {"claude", "codex"}
        and session.get("model_observed") in {None, "", "unknown"}
        and stream_events
    ):
        stream_model = (
            extract_claude_model_from_events(stream_events)
            if provider_id == "claude"
            else extract_codex_model_from_events(stream_events)
        )
        if stream_model != "unknown":
            session = dict(session)
            session["model_observed"] = stream_model
            session["model_observation_reason"] = f"{provider_id}-json-stream"
    provider_version_value = binary_version(binary, provider)
    skill_evidence = sanitized_skill_evidence(selection)
    ibom_binding = {
        "model": str(model),
        "effort": str(effort or "provider-default"),
        "seat": seat,
        "risk_triggers": risk_overlay.get("triggers", []),
        "review_independence": review_independence,
        "governance_effort": governance_effort,
    }
    canon_path = ROOT / str(config.get("routing_canon") or "routing-policy.yaml")
    instruction_bom = build_instruction_bom(
        cwd=cwd,
        provider_id=provider_id,
        provider=provider,
        provider_version=provider_version_value,
        canon_path=canon_path,
        route_name=route_name or "explicit-provider",
        binding=ibom_binding,
        prompt_sha256=sha256_text(args.prompt),
        skill_evidence=skill_evidence,
        intent_ref=checkpoint.get("intent_ref") if checkpoint else None,
        mode=args.mode,
    )
    record = {
        "schema_version": int(config["journal"]["schema_version"]),
        "event_type": "provider_run",
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "run_status": run_status,
        "failure_class": failure_class,
        "timeout_seconds": timeout_seconds,
        "serial_group": serial_group,
        "session_attribution": session_attribution,
        "stage_telemetry": {
            key: value
            for key, value in stage_telemetry.items()
            if not str(key).startswith("_")
        },
        "model_observation_reason": session.get("model_observation_reason"),
        "provider_id": provider_id,
        "provider_version": provider_version_value,
        "billing_policy": provider["billing_policy"],
        "billing_guard": "best-effort-credential-isolation-not-an-account-charge-guarantee",
        "seat": seat,
        "route": route_name or "explicit-provider",
        "review_independence": review_independence,
        "producer_ref": producer_ref or {"status": "not-required"},
        "risk_overlay": risk_overlay,
        "mode": args.mode,
        "model_requested": model,
        "model_family": journal_model_family(
            provider_id, config, str(model), session, health_evidence
        ),
        "model_catalog_status": model_catalog["status"],
        "effort_requested": effort or "provider-default",
        "governance_effort": governance_effort,
        "model_observed": session["model_observed"],
        "provider_health_evidence": health_evidence,
        "repo": slug,
        "workspace_fingerprint": sha256_text(str(cwd))[:16],
        "user_prompt_sha256": sha256_text(args.prompt),
        "user_prompt_length": len(args.prompt),
        "delivered_prompt_sha256": sha256_text(managed_prompt),
        "delivered_prompt_length": len(managed_prompt),
        "command_template_sha256": sha256_text(
            json.dumps(provider["commands"][args.mode])
        ),
        "exit_code": proc.returncode,
        "stdout_sha256": sha256_text(stdout),
        "stdout_length": len(stdout),
        "stderr_sha256": sha256_text(stderr),
        "stderr_length": len(stderr),
        "session_id": session["session_id"],
        "session_ref": session["session_ref"],
        "session_status": session["session_status"],
        "changed_session_artifact_count": changed_artifact_count,
        "checkpoint": checkpoint or {"status": "not-required-for-explicit-read-only"},
        "stripped_billing_environment": sorted(stripped_env),
        "provider_tools": "none" if args.no_provider_tools else "manifest-default",
        "minimal_runtime": bool(args.minimal_runtime),
        "workspace_trust_explicit": bool(args.trust_workspace),
        "workspace_trust_source": workspace_trust_source,
        "skill_evidence": skill_evidence,
        "instruction_bom": instruction_bom,
        "instruction_bom_digest": instruction_bom["digest"],
    }
    path = journal_path(config, slug)
    append_journal(path, record)
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr and args.show_stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
    print(
        json.dumps(
            {
                "agent_run": {
                    "run_id": run_id,
                    "provider": provider_id,
                    "seat": seat,
                    "exit_code": proc.returncode,
                    "failure_class": failure_class,
                    "duration_ms": duration_ms,
                    "session_id": session["session_id"],
                    "session_ref": session["session_ref"],
                    "session_status": session["session_status"],
                    "journal": portable_ref(path),
                    "model": model,
                    "model_observed": session["model_observed"],
                    "model_family": record["model_family"],
                    "instruction_bom_digest": instruction_bom["digest"],
                    "skills_selected": [row["name"] for row in selection["chosen"]],
                    "skills_deferred": [row["name"] for row in selection["deferred"]],
                }
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return proc.returncode


def discover(config: dict) -> int:
    rows = []
    for provider_id, provider in config["providers"].items():
        try:
            binary = resolve_binary(provider)
            available = True
            version = binary_version(binary, provider)
        except ProviderRunError:
            binary = None
            available = False
            version = "unavailable"
        catalog = (
            discover_provider_models(provider, binary)
            if binary is not None
            else {"status": "binary-unavailable", "models": []}
        )
        rows.append(
            {
                "provider_id": provider_id,
                "available": available,
                "runnable": available
                and str(provider.get("run_policy") or "enabled") == "enabled",
                "binary": portable_ref(binary) if binary else None,
                "version": version,
                "billing_policy": provider["billing_policy"],
                "run_policy": str(provider.get("run_policy") or "enabled"),
                "session_artifacts": len(session_snapshot(provider)),
                "model_catalog": catalog,
            }
        )
    print(json.dumps({"providers": rows}, ensure_ascii=False, indent=2))
    return 0


def latest_provider_evidence(
    config: dict,
    repo: str,
    provider_id: str,
    model: str | None = None,
) -> dict:
    path = journal_path(config, repo)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            canonical_provider_id(config, str(row.get("provider_id") or ""))
            != provider_id
        ):
            continue
        if model and str(row.get("model_requested") or "") != model:
            continue
        observed_at = str(row.get("started_at") or "unknown")
        max_age_seconds = int(config["journal"]["live_evidence_max_age_seconds"])
        future_skew_seconds = int(
            config["journal"]["live_evidence_future_skew_seconds"]
        )
        try:
            observed = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=dt.timezone.utc)
            raw_age_seconds = (
                dt.datetime.now(dt.timezone.utc) - observed
            ).total_seconds()
            if raw_age_seconds < -future_skew_seconds:
                return {
                    "status": "stale-live-evidence",
                    "observed_at": observed_at,
                    "reason": "future-timestamp",
                    "future_skew_seconds": future_skew_seconds,
                }
            age_seconds = max(0, int(raw_age_seconds))
        except ValueError:
            return {
                "status": "stale-live-evidence",
                "observed_at": observed_at,
                "reason": "invalid-timestamp",
                "max_age_seconds": max_age_seconds,
            }
        if raw_age_seconds > max_age_seconds:
            return {
                "status": "stale-live-evidence",
                "observed_at": observed_at,
                "age_seconds": age_seconds,
                "max_age_seconds": max_age_seconds,
            }
        failure = str(row.get("failure_class") or "none")
        if failure == "quota-exhausted":
            return {
                "status": "quota-exhausted",
                "observed_at": observed_at,
                "cooldown": str(row.get("quota_cooldown") or "unknown"),
            }
        if row.get("run_status") == "completed" and row.get("exit_code") == 0:
            health_status = str(
                row.get("provider_health_evidence", {}).get("status") or "unverified"
            )
            session_status = str(row.get("session_status") or "unknown")
            model_observed = str(row.get("model_observed") or "unknown")
            model_requested = str(row.get("model_requested") or "unknown")
            model_identity_unverified = model_observed in {"", "unknown"} or (
                provider_id not in {"cursor", "grok"}
                and model_observed != model_requested
            )
            broker_health_unverified = provider_id in {"cursor", "grok"} and (
                not health_status.startswith("verified-")
                or session_status
                not in {
                    "attributed-single-artifact",
                    "attributed-correlated-artifacts",
                    "attributed-stream-json",
                }
            )
            if model_identity_unverified or broker_health_unverified:
                return {
                    "status": "run-succeeded-health-unverified",
                    "observed_at": observed_at,
                    "model_observed": model_observed,
                }
            return {
                "status": "live-run-verified",
                "observed_at": observed_at,
                "model_observed": model_observed,
            }
        return {
            "status": failure if failure != "none" else "provider-error",
            "observed_at": observed_at,
            "cooldown": "unknown",
        }
    return {"status": "no-live-evidence", "cooldown": "not-applicable"}


def provider_model_evidence(config: dict, repo: str, provider_id: str) -> dict:
    path = journal_path(config, repo)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    evidence: dict[str, dict] = {}
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            canonical_provider_id(config, str(row.get("provider_id") or ""))
            != provider_id
        ):
            continue
        model = str(row.get("model_requested") or "unknown")
        if model in evidence:
            continue
        evidence[model] = latest_provider_evidence(config, repo, provider_id, model)
        if len(evidence) >= 12:
            break
    return evidence


def build_route_doctor(config: dict, route_name: str | None, repo: str) -> dict:
    canon = routing_canon(config)
    if route_name and route_name not in canon["runtime_routes"]:
        raise ProviderRunError(f"unknown route: {route_name!r}")
    provider_rows: list[dict] = []
    catalogs: dict[str, dict] = {}
    binaries: dict[str, Path | None] = {}
    for provider_id, provider in config["providers"].items():
        try:
            binary = resolve_binary(provider)
            installed = True
            version = binary_version(binary, provider)
            catalog = discover_provider_models(provider, binary)
        except ProviderRunError:
            binary = None
            installed = False
            version = "unavailable"
            catalog = {"status": "binary-unavailable", "models": []}
        catalogs[provider_id] = catalog
        binaries[provider_id] = binary
        model_ids = [str(row["id"]) for row in catalog["models"]]
        provider_rows.append(
            {
                "provider_id": provider_id,
                "serial_group": serial_group_for_provider(provider_id),
                "serial_lock_enabled": bool(serial_group_for_provider(provider_id)),
                "installed": installed,
                "configured_runnable": (
                    installed
                    and str(provider.get("run_policy") or "enabled") == "enabled"
                ),
                "version": version,
                "catalog_status": catalog["status"],
                "catalog_model_count": len(model_ids),
                "catalog_sha256": sha256_text(
                    json.dumps(model_ids, separators=(",", ":"))
                ),
                "live_evidence": latest_provider_evidence(config, repo, provider_id),
                "model_evidence": provider_model_evidence(config, repo, provider_id),
            }
        )

    names = list(canon["runtime_routes"])
    route_rows: list[dict] = []
    valid_concurrency = {"family_serial", "explicitly_parallel"}
    for name in names:
        binding = resolve_binding(canon, name)
        raw_route = canon["runtime_routes"][name]
        provider_id = canonical_provider_id(config, binding["provider"])
        provider = config["providers"].get(provider_id)
        blockers: list[dict] = []
        warnings: list[dict] = []
        if binding["route_policy"] != "enabled":
            blockers.append(
                {
                    "code": "route-policy-disabled",
                    "detail": binding["route_policy"],
                }
            )
        if provider is None:
            blockers.append({"code": "provider-unknown", "detail": provider_id})
            family = "undisclosed"
        else:
            family = resolve_model_family(provider, binding["model"])
            if str(provider.get("run_policy") or "enabled") != "enabled":
                blockers.append(
                    {
                        "code": "provider-disabled",
                        "detail": str(provider.get("run_policy")),
                    }
                )
            if binaries.get(provider_id) is None:
                blockers.append(
                    {"code": "provider-not-installed", "detail": provider_id}
                )
            catalog = catalogs.get(provider_id, {"status": "missing", "models": []})
            model_ids = {str(row.get("id")) for row in catalog["models"]}
            if catalog["status"] not in {"catalog-listed", "static-config"}:
                blockers.append(
                    {
                        "code": "model-catalog-unavailable",
                        "detail": catalog["status"],
                    }
                )
            elif binding["model"] not in model_ids:
                blockers.append(
                    {
                        "code": "model-not-listed",
                        "detail": binding["model"],
                    }
                )
            if (
                binding["review_independence"] == "cross-family"
                and family == "undisclosed"
            ):
                blockers.append(
                    {
                        "code": "reviewer-family-undisclosed",
                        "detail": binding["model"],
                    }
                )
            live = latest_provider_evidence(config, repo, provider_id, binding["model"])
            if live["status"] == "quota-exhausted":
                blockers.append(
                    {
                        "code": "live-quota-exhausted",
                        "detail": f"cooldown:{live.get('cooldown', 'unknown')}",
                    }
                )
            elif live["status"] in {
                "auth-expired",
                "authentication",
                "action-required-data-policy",
            }:
                blockers.append(
                    {
                        "code": f"live-{live['status']}",
                        "detail": str(live.get("observed_at") or "unknown"),
                    }
                )
            elif live["status"] != "live-run-verified":
                blockers.append(
                    {
                        "code": "live-evidence-unverified",
                        "detail": live["status"],
                    }
                )
        serial_group = serial_group_for_provider(provider_id, binding)
        concurrency = raw_route.get("concurrency")
        if concurrency not in valid_concurrency:
            blockers.append(
                {"code": "route-concurrency-invalid", "detail": str(concurrency)}
            )
        elif concurrency == "family_serial" and not serial_group:
            blockers.append(
                {"code": "serial-lock-required", "detail": provider_id}
            )
        elif concurrency == "explicitly_parallel" and serial_group:
            blockers.append(
                {
                    "code": "serial-lock-contradicts-parallel",
                    "detail": serial_group,
                }
            )
        elif concurrency == "explicitly_parallel":
            warnings.append(
                {
                    "code": "serial-lock-disabled",
                    "detail": provider_id,
                }
            )
        route_rows.append(
            {
                "route": name,
                "status": classify_route_status(blockers),
                "provider_id": provider_id,
                "model": binding["model"],
                "model_family": family,
                "seat": binding["seat"],
                "timeout_seconds": binding.get("timeout_seconds"),
                "concurrency": concurrency,
                "serial_group": serial_group,
                "serial_lock_enabled": bool(serial_group),
                "blockers": blockers,
                "warnings": warnings,
            }
        )

    ready_route_names = {row["route"] for row in route_rows if row["status"] == "ready"}
    review_families = sorted(
        {
            resolve_model_family(
                config["providers"][canonical_provider_id(config, binding["provider"])],
                binding["model"],
            )
            for name in canon["runtime_routes"]
            for binding in [resolve_binding(canon, name)]
            if binding["review_independence"] == "cross-family"
            and binding["route_policy"] == "enabled"
            and name in ready_route_names
            and canonical_provider_id(config, binding["provider"])
            in config["providers"]
        }
        - {"undisclosed"}
    )
    discovered_families = {
        resolve_model_family(config["providers"][provider_id], str(model["id"]))
        for provider_id, catalog in catalogs.items()
        for model in catalog["models"]
        if provider_id in config["providers"] and model.get("id")
    } - {"undisclosed"}
    producer_families = sorted(discovered_families | set(review_families))
    reviewer_graph = {
        producer: [family for family in review_families if family != producer]
        for producer in producer_families
    }
    # Missing cross-family edges with actionable route-level reasons.
    cross_family_rows = [
        row
        for row in route_rows
        if resolve_binding(canon, row["route"]).get("review_independence")
        == "cross-family"
    ]
    reviewer_graph_gaps: dict[str, list[dict]] = {}
    for producer in producer_families:
        missing: list[dict] = []
        for row in cross_family_rows:
            if row["model_family"] == producer:
                continue
            if row["status"] == "ready":
                continue
            if row["model_family"] in reviewer_graph.get(producer, []):
                continue
            missing.append(
                {
                    "family": row["model_family"],
                    "route": row["route"],
                    "status": row["status"],
                    "reason": ",".join(
                        str(b.get("code") or "unknown") for b in row["blockers"]
                    )
                    or "not-ready",
                }
            )
        # de-dupe by family keeping first reason
        seen: set[str] = set()
        deduped: list[dict] = []
        for item in missing:
            family = str(item["family"])
            if family in seen or family == "undisclosed":
                continue
            seen.add(family)
            deduped.append(item)
        reviewer_graph_gaps[producer] = deduped
    required_routes = [route_name] if route_name else []
    # Focused doctor (--task-shape): only the requested route is required; do not
    # dump every degraded/disabled sibling as "optional". Full inventory is when
    # task_shape is omitted.
    if route_name:
        optional_routes = [
            row["route"]
            for row in route_rows
            if row["route"] != route_name
            and str(resolve_binding(canon, row["route"]).get("route_policy") or "enabled")
            != "enabled"
        ]
    else:
        optional_routes = [
            row["route"]
            for row in route_rows
            if row["status"] in {"disabled", "degraded"}
        ]
    canon_path = ROOT / str(config.get("routing_canon") or "routing-policy.yaml")
    serial_lock_warnings = [
        {
            "code": warning["code"],
            "route": row["route"],
            "provider_id": row["provider_id"],
        }
        for row in route_rows
        for warning in row["warnings"]
        if warning.get("code") == "serial-lock-disabled"
    ]
    return {
        "routing_canon": portable_ref(canon_path),
        "routing_canon_sha256": sha256_bytes(canon_path.read_bytes()),
        "task_focus": {
            "task_shape": route_name,
            "required_routes": required_routes,
            "optional_or_disabled_routes": optional_routes,
        },
        "providers": provider_rows,
        "warnings": serial_lock_warnings,
        "routes": (
            [row for row in route_rows if row["route"] == route_name]
            if route_name
            else route_rows
        ),
        "reviewer_graph": reviewer_graph,
        "reviewer_graph_gaps": reviewer_graph_gaps,
    }


def doctor(args: argparse.Namespace, config: dict) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    repo = args.repo or repo_slug(cwd)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        raise ProviderRunError("--repo must be a project slug, not a path")
    report = build_route_doctor(config, route_name=args.task_shape, repo=repo)
    print(json.dumps({"route_doctor": report}, ensure_ascii=False, indent=2))
    return 0


def ibom(args: argparse.Namespace, config: dict) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    repo = args.repo or repo_slug(cwd)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        raise ProviderRunError("--repo must be a project slug, not a path")
    path = journal_path(config, repo)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProviderRunError(f"run journal not found for repo {repo!r}") from exc
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if args.run_id and row.get("run_id") != args.run_id:
            continue
        bom = row.get("instruction_bom")
        if isinstance(bom, dict):
            print(json.dumps({"instruction_bom": bom}, ensure_ascii=False, indent=2))
            return 0
    target = args.run_id or "latest schema-v4 run"
    raise ProviderRunError(f"I-BOM not found: {target}")


def routes(config: dict) -> int:
    canon = routing_canon(config)
    compiled = {}
    for name in canon["runtime_routes"]:
        binding = resolve_binding(canon, name)
        provider_id = canonical_provider_id(config, binding["provider"])
        serial_group = serial_group_for_provider(provider_id, binding)
        compiled[name] = dict(
            binding,
            concurrency=canon["runtime_routes"][name].get("concurrency"),
            serial_group=serial_group,
            serial_lock_enabled=bool(serial_group),
        )
    print(json.dumps({"routes": compiled}, ensure_ascii=False, indent=2))
    return 0


def status(args: argparse.Namespace, config: dict) -> int:
    root = expand(config["journal"]["root"]).resolve()
    if args.repo and not re.fullmatch(r"[A-Za-z0-9._-]+", args.repo):
        raise ProviderRunError("--repo must be a project slug, not a path")
    path = (root / f"{args.repo}.jsonl").resolve() if args.repo else None
    if path is not None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ProviderRunError("journal path escapes configured root") from exc
    paths = (
        [path]
        if path
        else sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    )
    records: list[dict] = []
    for candidate in paths:
        if candidate is None or not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines[-args.limit :]:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    records.sort(key=lambda row: row.get("started_at", ""), reverse=True)
    print(json.dumps({"runs": records[: args.limit]}, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-run")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    sub.add_parser("routes")
    doc = sub.add_parser("doctor")
    doc.add_argument("--task-shape")
    doc.add_argument("--repo")
    doc.add_argument("--cwd", default=os.getcwd())
    bom = sub.add_parser("ibom")
    bom.add_argument("--run-id")
    bom.add_argument("--repo")
    bom.add_argument("--cwd", default=os.getcwd())
    run = sub.add_parser("run")
    run.add_argument("provider")
    run.add_argument("prompt")
    run.add_argument("--seat")
    run.add_argument("--task-shape")
    run.add_argument("--model")
    run.add_argument("--effort")
    run.add_argument("--producer-provider")
    run.add_argument("--producer-run-id")
    run.add_argument("--producer-review-bundle")
    run.add_argument("--producer-review-bundle-sha256")
    run.add_argument("--orchestration-run-id")
    run.add_argument("--orchestration-generation", type=int)
    run.add_argument("--orchestration-fencing-token")
    run.add_argument("--orchestration-reviewer-task-id")
    run.add_argument("--orchestration-reviewer-attempt-id")
    run.add_argument("--checkpoint-event")
    run.add_argument("--risk-trigger", action="append", default=[])
    run.add_argument("--cwd", default=os.getcwd())
    run.add_argument("--mode", choices=["read-only", "execute"], default="read-only")
    run.add_argument("--allow-write", action="store_true")
    run.add_argument("--skill", action="append", default=["auto"])
    run.add_argument("--show-stderr", action="store_true")
    run.add_argument("--no-provider-tools", action="store_true")
    run.add_argument("--no-skills", action="store_true")
    run.add_argument("--timeout-seconds", type=int)
    run.add_argument("--minimal-runtime", action="store_true")
    run.add_argument("--trust-workspace", action="store_true")
    st = sub.add_parser("status")
    st.add_argument("--repo")
    st.add_argument("--limit", type=int, default=10)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_manifest(Path(args.manifest))
        if args.command == "discover":
            return discover(config)
        if args.command == "routes":
            return routes(config)
        if args.command == "doctor":
            return doctor(args, config)
        if args.command == "ibom":
            return ibom(args, config)
        if args.command == "run":
            return run_provider(args, config)
        if args.command == "status":
            return status(args, config)
        raise ProviderRunError("unsupported command")
    except ProviderRunError as exc:
        print(f"agent-run: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
