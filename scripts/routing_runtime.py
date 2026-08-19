#!/usr/bin/env python3
"""Pure runtime compiler for the repository's routing canon."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
import re

import yaml


class RoutingRuntimeError(RuntimeError):
    pass


CURSOR_MODEL_LINE_RE = re.compile(r"^([a-z0-9][a-z0-9._+\-]*)\s+-\s+(.+)$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(raw)


def load_routing_canon(path: Path) -> dict:
    try:
        canon = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RoutingRuntimeError(f"cannot load routing canon: {exc}") from exc
    if not isinstance(canon, dict) or canon.get("version") != 1:
        raise RoutingRuntimeError("routing canon must be a mapping with version: 1")
    if not isinstance(canon.get("runtime_routes"), dict) or not canon["runtime_routes"]:
        raise RoutingRuntimeError(
            "routing canon needs a non-empty runtime_routes mapping"
        )
    return canon


def resolve_binding(canon: dict, route_name: str) -> dict:
    route = canon["runtime_routes"].get(route_name)
    if not isinstance(route, dict):
        raise RoutingRuntimeError(f"unknown route: {route_name}")
    model = route.get("model")
    effort = route.get("effort")
    policy_ref = str(route.get("policy_ref") or "")
    if policy_ref:
        prefix = "task_shapes."
        if not policy_ref.startswith(prefix):
            raise RoutingRuntimeError(
                f"route {route_name!r} has unsupported policy_ref: {policy_ref!r}"
            )
        shape_name = policy_ref.removeprefix(prefix)
        shape = canon.get("task_shapes", {}).get(shape_name)
        family = str(route.get("policy_family") or "")
        if not isinstance(shape, dict) or not family:
            raise RoutingRuntimeError(
                f"route {route_name!r} cannot resolve policy_ref {policy_ref!r}"
            )
        try:
            model = shape["execution_model"][family]
            effort = shape["execution_effort"]
        except (KeyError, TypeError) as exc:
            raise RoutingRuntimeError(
                f"route {route_name!r} policy family {family!r} is not allowed"
            ) from exc
    required_values = {
        "provider": route.get("provider"),
        "model": model,
        "effort": effort,
        "seat": route.get("seat"),
    }
    required = tuple(required_values)
    missing = [key for key in required if not required_values[key]]
    if missing:
        raise RoutingRuntimeError(
            f"route {route_name!r} missing required key(s): " + ", ".join(missing)
        )
    binding = {
        "provider": str(route["provider"]),
        "model": str(model),
        "effort": str(effort),
        "seat": str(route["seat"]),
        "route_policy": str(route.get("route_policy") or "enabled"),
        "review_independence": str(
            route.get("review_independence") or "not-applicable"
        ),
        "governance_effort": str(route.get("governance_effort") or effort),
    }
    if "eligible_producer_routes" in route:
        eligible = route["eligible_producer_routes"]
        if (
            not isinstance(eligible, list)
            or not eligible
            or any(not isinstance(item, str) or not item for item in eligible)
        ):
            raise RoutingRuntimeError(
                f"route {route_name!r} has invalid eligible_producer_routes"
            )
        binding["eligible_producer_routes"] = list(eligible)
    if "timeout_seconds" in route:
        try:
            binding["timeout_seconds"] = int(route["timeout_seconds"])
        except (TypeError, ValueError) as exc:
            raise RoutingRuntimeError(
                f"route {route_name!r} has invalid timeout_seconds"
            ) from exc
        if binding["timeout_seconds"] <= 0:
            raise RoutingRuntimeError(
                f"route {route_name!r} timeout_seconds must be positive"
            )
    if "serial_group" in route:
        serial_group = route["serial_group"]
        if not isinstance(serial_group, str) or not serial_group.strip():
            raise RoutingRuntimeError(
                f"route {route_name!r} has invalid serial_group"
            )
        binding["serial_group"] = serial_group.strip()
    if "managed_skills" in route:
        managed_skills = route["managed_skills"]
        if managed_skills not in {"auto", "disabled"}:
            raise RoutingRuntimeError(
                f"route {route_name!r} has invalid managed_skills policy"
            )
        binding["managed_skills"] = managed_skills
    return binding


def parse_cursor_model_catalog(output: str) -> list[dict[str, str]]:
    lines = output.splitlines()
    try:
        start = (
            next(
                i for i, line in enumerate(lines) if line.strip() == "Available models"
            )
            + 1
        )
    except StopIteration as exc:
        raise RoutingRuntimeError("Cursor model catalog header was not found") from exc
    models: list[dict[str, str]] = []
    for raw in lines[start:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Tip:"):
            break
        match = CURSOR_MODEL_LINE_RE.fullmatch(line)
        if not match:
            raise RoutingRuntimeError(
                f"unrecognized Cursor model catalog line: {line!r}"
            )
        models.append({"id": match.group(1), "label": match.group(2)})
    if not models:
        raise RoutingRuntimeError("Cursor model catalog contained no models")
    return models


def resolve_model_family(provider: dict, model_id: str) -> str:
    rules = provider.get("model_family_rules", [])
    for rule in rules:
        if fnmatch.fnmatchcase(model_id, str(rule.get("glob") or "")):
            return str(rule.get("family") or "undisclosed")
    if rules:
        return "undisclosed"
    return str(provider.get("family") or "undisclosed")


def private_path_ref(path: Path, cwd: Path, home: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(cwd.resolve()))
    except ValueError:
        pass
    try:
        return "~/" + str(resolved.relative_to(home.resolve()))
    except ValueError:
        return "external:" + sha256_bytes(str(resolved).encode("utf-8"))[:16]


def discover_instruction_sources(
    cwd: Path,
    provider_id: str,
    home: Path | None = None,
) -> list[dict[str, str]]:
    home = (home or Path.home()).resolve()
    cwd = cwd.resolve()
    candidates: list[Path] = []
    if provider_id == "codex":
        candidates.append(home / ".codex" / "AGENTS.md")
    elif provider_id == "claude":
        candidates.append(home / ".claude" / "CLAUDE.md")
    else:
        candidates.extend(
            [
                home / ".codex" / "AGENTS.md",
                home / ".claude" / "CLAUDE.md",
            ]
        )
    scope_dirs = [cwd]
    if cwd == home or home in cwd.parents:
        current = cwd
        while current != home:
            current = current.parent
            scope_dirs.append(current)
    for directory in reversed(scope_dirs):
        candidates.extend([directory / "AGENTS.md", directory / "CLAUDE.md"])
    rows: list[dict[str, str]] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        rows.append(
            {
                "kind": resolved.name,
                "scope_ref": private_path_ref(resolved, cwd, home),
                "content_sha256": sha256_bytes(resolved.read_bytes()),
                "delivery": "provider-native-candidate-not-wrapper-confirmed",
            }
        )
    return rows


def resolve_intent_evidence(cwd: Path, intent_ref: str | None) -> dict:
    if not intent_ref:
        return {"status": "not-linked"}
    path = (cwd / intent_ref).resolve()
    try:
        path.relative_to(cwd.resolve())
    except ValueError:
        return {
            "status": "outside-workspace",
            "ref_sha256": canonical_digest(intent_ref),
        }
    if not path.is_file():
        return {"status": "not-observed", "ref": intent_ref}
    return {
        "status": "observed",
        "ref": str(path.relative_to(cwd.resolve())),
        "sha256": sha256_bytes(path.read_bytes()),
    }


def build_instruction_bom(
    *,
    cwd: Path,
    provider_id: str,
    provider: dict,
    provider_version: str,
    canon_path: Path,
    route_name: str,
    binding: dict,
    prompt_sha256: str,
    skill_evidence: dict,
    intent_ref: str | None,
    mode: str,
) -> dict:
    mcp = provider.get("mcp_capabilities") or {
        "status": "opaque-unavailable",
        "names": [],
    }
    mcp_status = str(mcp.get("status") or "opaque-unavailable")
    mcp_names = sorted(map(str, mcp.get("names", [])))
    safe_mcp = {
        "status": mcp_status,
        "capability_count": len(mcp_names),
        "digest": canonical_digest({"status": mcp_status, "names": mcp_names}),
    }
    command_templates = provider.get("commands", {})
    body = {
        "version": 1,
        "routing": {
            "canon_sha256": sha256_bytes(canon_path.read_bytes()),
            "route": route_name,
            "binding_sha256": canonical_digest(binding),
        },
        "instructions": discover_instruction_sources(cwd, provider_id),
        "intent": resolve_intent_evidence(cwd, intent_ref),
        "prompt_sha256": prompt_sha256,
        "prompt_template_sha256": canonical_digest(command_templates),
        "skills": skill_evidence,
        "mcp": safe_mcp,
        "provider_builtin_prompt": {
            "status": "opaque",
            "version": f"opaque:{provider_version}",
        },
        "provider_adapter_sha256": canonical_digest(provider),
        "execution": {
            "provider": provider_id,
            "model": str(binding.get("model") or "unknown"),
            "effort": str(binding.get("effort") or "provider-default"),
            "seat": str(binding.get("seat") or "unknown"),
            "risk_triggers": list(map(str, binding.get("risk_triggers", []))),
            "mode": mode,
        },
        "privacy": {
            "contains_instruction_text": False,
            "contains_prompt_text": False,
            "contains_response_text": False,
            "contains_credentials": False,
        },
    }
    return dict(body, digest=canonical_digest(body))


STAGE_REVIEW_ROUTES = frozenset(
    {
        "codex_final_review",
        "final_review",
        "claude_final_review",
        "fable_final_review",
        "secondary_final_review",
    }
)
SPAWN_FINAL_REVIEW_ROUTES = STAGE_REVIEW_ROUTES | {"arbitration"}
_UNDISCLOSED_FAMILIES = frozenset({"", "unknown", "undisclosed"})


def _require_mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise RoutingRuntimeError(f"{name} must be a mapping")
    return value


def stage_gate_policy(canon: dict) -> dict:
    """Consume `final_review.stage_gate`. Missing or incomplete blocks fail closed."""

    gate = _require_mapping(
        _require_mapping(canon.get("final_review"), "final_review").get("stage_gate"),
        "final_review.stage_gate",
    )
    model = gate.get("model")
    floor = gate.get("review_effort_floor")
    overlay = gate.get("overlay_still_applies")
    if not model or floor not in {"high", "xhigh"} or overlay is not True:
        raise RoutingRuntimeError("final_review.stage_gate is incomplete")
    return {
        "model": str(model),
        "review_effort_floor": str(floor),
        "overlay_still_applies": True,
        "enforced_by": str(gate.get("enforced_by") or ""),
    }


def spawn_dispatch_policy(canon: dict) -> dict:
    """Consume `spawn_dispatch`. Missing override list fails closed."""

    block = _require_mapping(canon.get("spawn_dispatch"), "spawn_dispatch")
    required = block.get("explicit_override_required_for")
    if not isinstance(required, list) or not required:
        raise RoutingRuntimeError(
            "spawn_dispatch.explicit_override_required_for is missing"
        )
    if block.get("default_selection") != "inherit_parent":
        raise RoutingRuntimeError(
            "spawn_dispatch.default_selection must be inherit_parent"
        )
    resolution = _require_mapping(
        block.get("final_review_resolution"),
        "spawn_dispatch.final_review_resolution",
    )
    return {
        "default_selection": "inherit_parent",
        "explicit_override_required_for": [str(item) for item in required],
        "precedence": [str(item) for item in (block.get("precedence") or [])],
        "final_review_resolution": resolution,
        "enforced_by": str(block.get("enforced_by") or ""),
    }


def cross_family_mandatory_shapes(canon: dict) -> frozenset[str]:
    block = _require_mapping(
        _require_mapping(canon.get("final_review"), "final_review").get(
            "cross_family_mandatory"
        ),
        "final_review.cross_family_mandatory",
    )
    shapes = block.get("task_shapes")
    if not isinstance(shapes, list) or not shapes:
        raise RoutingRuntimeError(
            "final_review.cross_family_mandatory.task_shapes is missing"
        )
    return frozenset(map(str, shapes))


def spawn_requires_explicit_override(canon: dict, route_name: str | None) -> bool:
    if not route_name:
        return False
    required = set(spawn_dispatch_policy(canon)["explicit_override_required_for"])
    if "final_review" in required and route_name in SPAWN_FINAL_REVIEW_ROUTES:
        return True
    if (
        "cross_family_mandatory" in required
        and route_name in cross_family_mandatory_shapes(canon)
    ):
        return True
    return False


def validate_spawn_dispatch(
    canon: dict,
    *,
    route_name: str | None,
    inherit_parent: bool,
    explicit_override: bool,
) -> dict:
    """Fail closed when a gated spawn inherits the parent without override."""

    policy = spawn_dispatch_policy(canon)
    gated = spawn_requires_explicit_override(canon, route_name)
    if inherit_parent and gated and not explicit_override:
        raise RoutingRuntimeError(
            f"spawn for route {route_name!r} requires an explicit model/effort "
            "override; inherit_parent is forbidden for final_review / "
            "cross_family_mandatory"
        )
    if explicit_override:
        status = "explicit-override"
    elif inherit_parent:
        status = "inherit-parent"
    else:
        status = "not-a-spawn"
    return {
        "status": status,
        "gated": gated,
        "default_selection": policy["default_selection"],
    }


def validate_stage_gate(
    canon: dict,
    *,
    review_route: str | None,
    reviewer_family: str,
    producer_family: str,
    producer_route: str | None = None,
    risk_triggers: list[str] | None = None,
) -> dict:
    """Block same-family producer + stage final-reviewer except D3.

    D3 (`secondary_final_review` of `ordinary_bug_fix`) may stay same-family
    only when no risk overlay fired. Overlay or a cross_family_mandatory
    producer route forces the reciprocal family (Codex producer → Fable).
    Does not rewrite `runtime_routes` effort bindings.
    """

    if review_route not in STAGE_REVIEW_ROUTES:
        return {"status": "not-stage-gate", "route": review_route}
    gate = stage_gate_policy(canon)
    spawn = spawn_dispatch_policy(canon)
    triggers = [str(item) for item in (risk_triggers or []) if item]
    overlay = bool(triggers)
    d3_allowed = (
        review_route == "secondary_final_review"
        and producer_route == "ordinary_bug_fix"
        and not overlay
    )
    disclosed = (
        reviewer_family not in _UNDISCLOSED_FAMILIES
        and producer_family not in _UNDISCLOSED_FAMILIES
    )
    same_family = disclosed and reviewer_family == producer_family
    mandatory = (producer_route or "") in cross_family_mandatory_shapes(canon)
    cross_family_required = overlay or mandatory
    if producer_family == "openai" and cross_family_required:
        if reviewer_family == "openai":
            reciprocal = spawn["final_review_resolution"].get(
                "reciprocal_reviewer_for_codex_producer"
            )
            raise RoutingRuntimeError(
                "spawn_dispatch.precedence: Codex-family producer requires "
                f"reciprocal reviewer {reciprocal!r}, not Sol"
            )
    if same_family and not d3_allowed:
        raise RoutingRuntimeError(
            "stage_gate forbids the same model family as both producer and "
            f"stage final reviewer ({producer_family!r}); "
            f"route={review_route!r} producer_route={producer_route!r}"
        )
    return {
        "status": "d3-same-family-allowed" if d3_allowed else "stage-gate-ok",
        "model": gate["model"],
        "review_effort_floor": (
            "xhigh" if overlay else gate["review_effort_floor"]
        ),
        "overlay": overlay,
    }
