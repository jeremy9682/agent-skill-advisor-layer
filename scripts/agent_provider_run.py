#!/usr/bin/env python3
"""Run local AI CLIs through one observable, privacy-minimized interface.

The provider manifest is portable. Native transcripts and credentials remain in
each product's own local storage. The append-only journal records pointers and
digests, never prompt/response text or auth material.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
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
SKILL_NAME_RE = re.compile(r"^- `([^`]+)`$")


class ProviderRunError(RuntimeError):
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
    return parse_session(adapter, None, "ambiguous-concurrent-artifacts"), len(paths)


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


def _scan_jsonl_rows(path: Path, limit: int = 400):
    try:
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def extract_codex_model_from_jsonl(path: Path) -> tuple[str, str]:
    """Read model identity from Codex rollout JSONL. Never invents a value."""
    last_model = ""
    for row in _scan_jsonl_rows(path):
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


def find_run_record(run_id: str, config: dict, expected_repo: str) -> dict:
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
        if row.get("mode") != "execute":
            raise ProviderRunError("producer run was not a write-capable execution")
        return row
    raise ProviderRunError(f"producer run not found in local journal: {run_id}")


def validate_review_independence(
    route_name: str | None,
    provider_id: str,
    args: argparse.Namespace,
    config: dict,
    expected_repo: str,
) -> tuple[str, dict | None]:
    if route_name is None:
        return "not-route-enforced", None
    route = route_binding(config, route_name)
    policy = str(route.get("review_independence") or "not-applicable")
    if policy == "not-applicable":
        return policy, None
    if not args.producer_run_id:
        raise ProviderRunError(f"route {route_name!r} requires --producer-run-id")
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
            if not health_status.startswith("verified-") or session_status not in {
                "attributed-single-artifact",
                "attributed-correlated-artifacts",
            }:
                raise ProviderRunError(
                    f"route {route_name!r} requires verified model evidence "
                    "for brokered producer runs"
                )
        observed_family = provider_family(producer_id, config, producer_observed)
        if (
            producer_family not in {"", "unknown"}
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
        if requested_model == "auto":
            if observed in {"", "unknown"}:
                return {"status": "unverified", "reason": "model-unobserved"}
            if observed in {"auto", "auto-undisclosed"}:
                return {
                    "status": "verified-native-session-model-opaque",
                    "model_observed": observed,
                }
            return {
                "status": "verified-native-session-model",
                "model_observed": observed,
            }
        if observed == requested_model:
            return {
                "status": "verified-native-session-model",
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
) -> str:
    lowered = stderr.lower()
    if run_status == "timed-out":
        return timeout_class or "timeout"
    if run_status == "interrupted":
        return "interrupted"
    if run_status in {"provider-health-unverified", "review-independence-violation"}:
        return run_status
    if exit_code != 0 and (
        "402" in lowered
        or "spending-limit" in lowered
        or "run out of credits" in lowered
    ):
        return "quota-exhausted"
    if exit_code != 0 and "429" in lowered and "free-usage-exhausted" in lowered:
        return "quota-exhausted"
    if exit_code != 0 and (
        "401" in lowered
        or "unauthorized" in lowered
        or "authentication required" in lowered
    ):
        return "authentication"
    if exit_code != 0 and (
        "review data policy" in lowered
        or ("actionrequirederror" in lowered and "retention policy" in lowered)
    ):
        return "action-required-data-policy"
    if exit_code != 0:
        return "provider-error"
    return "none"


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
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        run_status = "completed"
    except subprocess.TimeoutExpired as exc:
        run_status = "timed-out"
        telemetry["timeout_class"] = "timeout_total"
        proc = subprocess.CompletedProcess(
            command,
            124,
            stdout=exc.stdout.decode()
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            stderr=(
                (
                    exc.stderr.decode()
                    if isinstance(exc.stderr, bytes)
                    else (exc.stderr or "")
                )
                + "\nprovider-timeout"
            ),
        )
    except KeyboardInterrupt:
        run_status = "interrupted"
        proc = subprocess.CompletedProcess(
            command, 130, stdout="", stderr="provider-interrupted"
        )
    return proc, run_status, telemetry


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
        if run_status == "timed-out" and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        elif proc.poll() is None:
            try:
                proc.wait(timeout=max(1.0, total_budget - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                run_status = "timed-out"
                telemetry["timeout_class"] = "timeout_total"
                proc.kill()
                proc.wait(timeout=5)
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
        if proc is not None and proc.poll() is None:
            proc.kill()
        telemetry.pop("_last_progress_mono", None)
        completed = subprocess.CompletedProcess(
            command, 130, stdout="", stderr="provider-interrupted"
        )
        return completed, "interrupted", telemetry, events
    except OSError as exc:
        telemetry["timeout_class"] = "timeout_startup"
        telemetry.pop("_last_progress_mono", None)
        completed = subprocess.CompletedProcess(
            command, 127, stdout="", stderr=f"provider-start-failed: {exc}"
        )
        return completed, "timed-out", telemetry, events


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



def run_provider(args: argparse.Namespace, config: dict) -> int:
    provider_id, model, effort, seat, route_name = resolve_route(args, config)
    if not SEAT_RE.fullmatch(seat or ""):
        raise ProviderRunError(
            "seat must use the checkpoint vocabulary, e.g. codex-landing"
        )
    if args.mode == "execute" and not args.allow_write:
        raise ProviderRunError("execute mode requires explicit --allow-write")
    if args.timeout_seconds <= 0:
        raise ProviderRunError("--timeout-seconds must be positive")
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
        _path, skill_manifest, manifest_sha = skill_manifest_info(config)
        selection = {
            "manifest_sha256": manifest_sha,
            "available_count": len(skill_entries_by_name(skill_manifest)),
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
    if provider.get("requires_workspace_trust"):
        if not args.trust_workspace:
            raise ProviderRunError(
                "Cursor headless mode requires explicit --trust-workspace"
            )
        command.insert(-1, "--trust")
    elif args.trust_workspace:
        raise ProviderRunError(
            "--trust-workspace is only valid for providers that require it"
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
    env, stripped_env = scrub_environment(provider)
    before = session_snapshot(provider)
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    started = time.monotonic()
    stream_events: list[dict] = []
    if provider_id == "codex" and "--json" in command:
        proc, run_status, stage_telemetry, stream_events = run_codex_json_process(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=args.timeout_seconds,
        )
    else:
        proc, run_status, stage_telemetry = run_blocking_process(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=args.timeout_seconds,
        )
    duration_ms = round((time.monotonic() - started) * 1000)
    ended_at = utc_now()
    after = session_snapshot(provider)
    adapter = str(provider["session"].get("adapter") or "")
    session, changed_artifact_count = attribute_session(
        adapter, before, after, requested_model=str(model)
    )
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
    if producer_ref is not None and (
        session["session_id"] == "unknown"
        or producer_ref["session_id"] == session["session_id"]
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
    )
    if (
        session.get("model_observed") in {None, "", "unknown"}
        and stream_events
    ):
        stream_model = extract_codex_model_from_events(stream_events)
        if stream_model != "unknown":
            session = dict(session)
            session["model_observed"] = stream_model
            session["model_observation_reason"] = "codex-json-stream"
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
        "timeout_seconds": args.timeout_seconds,
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
        "model_family": provider_family(provider_id, config, str(model)),
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
    for name in names:
        binding = resolve_binding(canon, name)
        provider_id = canonical_provider_id(config, binding["provider"])
        provider = config["providers"].get(provider_id)
        blockers: list[dict] = []
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
        route_rows.append(
            {
                "route": name,
                "status": classify_route_status(blockers),
                "provider_id": provider_id,
                "model": binding["model"],
                "model_family": family,
                "seat": binding["seat"],
                "blockers": blockers,
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
    return {
        "routing_canon": portable_ref(canon_path),
        "routing_canon_sha256": sha256_bytes(canon_path.read_bytes()),
        "task_focus": {
            "task_shape": route_name,
            "required_routes": required_routes,
            "optional_or_disabled_routes": optional_routes,
        },
        "providers": provider_rows,
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
    compiled = {name: resolve_binding(canon, name) for name in canon["runtime_routes"]}
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
    run.add_argument("--checkpoint-event")
    run.add_argument("--risk-trigger", action="append", default=[])
    run.add_argument("--cwd", default=os.getcwd())
    run.add_argument("--mode", choices=["read-only", "execute"], default="read-only")
    run.add_argument("--allow-write", action="store_true")
    run.add_argument("--skill", action="append", default=["auto"])
    run.add_argument("--show-stderr", action="store_true")
    run.add_argument("--no-provider-tools", action="store_true")
    run.add_argument("--no-skills", action="store_true")
    run.add_argument("--timeout-seconds", type=int, default=300)
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
