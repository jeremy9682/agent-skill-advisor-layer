#!/usr/bin/env python3
"""Deterministic Phase-1.5 design selection in manual shadow mode.

It accepts an explicit task contract and writes auditable selection records.
It deliberately does not parse natural-language prompts, call an LLM, make a
network request, hook prompt submission, or invoke any skill.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "design-skill-catalog.yaml"
SCHEMA_REF = "schemas/design-selection-record.md"
VALID_EVIDENCE_KINDS = ("read", "invocation")
VALID_LANGUAGES = ("cjk", "latin")
VALID_VISUAL_DIRECTIONS = ("apple", "magazine", "template", "branded")
VALID_DECK_MODES = ("magazine", "template", "branded")
VALID_MOTION_SOURCES = ("html-interface",)
BOOLEAN_DELIVERABLE_FIELDS = ("erp", "media_export", "needs_direction")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _valid_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    return parsed <= datetime.now(timezone.utc) + timedelta(minutes=5)


def load_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    return data


def catalog_entries(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = catalog.get("design_skills")
    if not isinstance(entries, list):
        raise ValueError("catalog.design_skills must be a list")
    mapped_entries = [entry for entry in entries if isinstance(entry, dict)]
    names = [entry.get("name") for entry in mapped_entries]
    if not names or not all(isinstance(name, str) and name for name in names):
        raise ValueError("catalog contains an unnamed skill")
    if len(set(names)) != len(names):
        raise ValueError("catalog contains duplicate skill names")
    return {str(entry["name"]): entry for entry in mapped_entries}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside_installation(
    path: Path, skill: str, entries: dict[str, dict[str, Any]]
) -> bool:
    installations = entries.get(skill, {}).get("installations", [])
    if not isinstance(installations, list):
        return False
    for installation in installations:
        root_text = installation.get("path") if isinstance(installation, dict) else None
        if not isinstance(root_text, str) or not root_text:
            continue
        try:
            path.relative_to(Path(root_text).expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


def _receipt_matches(path: Path, expected: dict[str, str]) -> bool:
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    objects: list[Any]
    try:
        parsed = json.loads(text)
        objects = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        try:
            objects = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return False
    return any(
        isinstance(item, dict)
        and all(item.get(key) == value for key, value in expected.items())
        for item in objects
    )


def _usage_claim(
    task: dict[str, Any],
    deliverable_id: str,
    selected_skills: list[str],
    entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    requested = bool(task.get("usage_claim", False))
    if not requested:
        return {
            "requested": False,
            "permitted": True,
            "verification": "not-requested",
            "accepted_evidence_kinds": [],
            "accepted_evidence": [],
            "reason": None,
        }
    evidence = task.get("evidence", [])
    evidence = evidence if isinstance(evidence, list) else []
    accepted: list[dict[str, str]] = []
    seen_kinds: set[str] = set()
    seen_events: set[tuple[str, ...]] = set()
    selected = set(selected_skills)
    task_id = str(task.get("id", "unnamed-task"))
    for item in evidence:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        path_text = item.get("path")
        skill = item.get("skill")
        evidence_task = item.get("task_id")
        evidence_deliverable = item.get("deliverable_id")
        occurred_at = item.get("occurred_at")
        declared_sha = item.get("sha256")
        if (
            kind not in VALID_EVIDENCE_KINDS
            or skill not in selected
            or evidence_task != task_id
            or evidence_deliverable != deliverable_id
            or not _valid_utc_timestamp(occurred_at)
            or not isinstance(declared_sha, str)
            or not SHA256_RE.fullmatch(declared_sha)
            or not isinstance(path_text, str)
            or not path_text.strip()
        ):
            continue
        declared_path = Path(path_text).expanduser()
        resolved_path = declared_path if declared_path.is_absolute() else ROOT / declared_path
        resolved_path = resolved_path.resolve()
        if not resolved_path.is_file() or _file_sha256(resolved_path) != declared_sha:
            continue
        event_id = item.get("event_id")
        if kind == "read":
            if not _inside_installation(resolved_path, skill, entries):
                continue
            event_key = (
                kind,
                skill,
                task_id,
                deliverable_id,
                occurred_at,
                str(resolved_path),
                declared_sha,
            )
        else:
            if not isinstance(event_id, str) or not event_id:
                continue
            expected_receipt = {
                "event_id": event_id,
                "kind": "invocation",
                "skill": skill,
                "task_id": task_id,
                "deliverable_id": deliverable_id,
                "occurred_at": occurred_at,
            }
            if not _receipt_matches(resolved_path, expected_receipt):
                continue
            event_key = (kind, event_id, skill, task_id, deliverable_id, declared_sha)
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        seen_kinds.add(kind)
        retained_path = (
            str(resolved_path)
            if declared_path.is_absolute()
            else str(declared_path)
        )
        retained = {
            "kind": kind,
            "skill": skill,
            "task_id": task_id,
            "deliverable_id": deliverable_id,
            "occurred_at": occurred_at,
            "path": retained_path,
            "sha256": declared_sha,
        }
        if kind == "invocation":
            retained["event_id"] = event_id
        accepted.append(retained)
    permitted = bool(accepted)
    return {
        "requested": requested,
        "permitted": permitted,
        "verification": "hash-bound-attestation" if permitted else "insufficient-evidence",
        "accepted_evidence_kinds": [
            kind for kind in VALID_EVIDENCE_KINDS if kind in seen_kinds
        ],
        "accepted_evidence": accepted,
        "reason": (
            None
            if permitted
            else (
                "usage claim requires task-, deliverable-, skill-, time-, "
                "and SHA-bound read or invocation evidence"
            )
        ),
    }


def _facet_scope(entry: dict[str, Any], active: list[str]) -> tuple[list[str], list[str]]:
    """Derive an exhaustive active/suppressed split from catalogued ownership."""
    owned = entry.get("owns")
    if (
        not isinstance(owned, list)
        or not owned
        or not all(isinstance(item, str) for item in owned)
    ):
        raise ValueError(f"catalog entry {entry.get('name')} has invalid owns facets")
    if (
        len(set(owned)) != len(owned)
        or len(set(active)) != len(active)
        or not set(active).issubset(owned)
    ):
        raise ValueError(f"invalid facet selection for {entry.get('name')}")
    return active, [facet for facet in owned if facet not in active]


def _baseline(
    entries: dict[str, dict[str, Any]],
    language: str,
    *,
    erp: bool,
    apple: bool,
) -> list[dict[str, Any]]:
    if language != "cjk":
        return []
    active = ["cjk-typography", "cjk-spacing"]
    if erp and not apple:
        active.append("erp-structure")
    active, suppressed = _facet_scope(entries["design-systems"], active)
    return [
        {
            "skill": "design-systems",
            "active_facets": active,
            "suppressed_facets": suppressed,
            "precedence_note": (
                "CJK baseline controls typography and forbids negative letter-spacing."
            ),
        }
    ]


def _apple_overlay(
    entries: dict[str, dict[str, Any]], *, has_cjk_baseline: bool
) -> list[dict[str, Any]]:
    precedence_note = (
        "CJK baseline outranks typography-micro for CJK letter-spacing."
        if has_cjk_baseline
        else "No CJK baseline; typography-micro applies unconstrained."
    )
    active, suppressed = _facet_scope(
        entries["apple-design"], list(entries["apple-design"]["owns"])
    )
    return [
        {
            "skill": "apple-design",
            "active_facets": active,
            "suppressed_facets": suppressed,
            "precedence_note": precedence_note,
        }
    ]


def _author_for(deliverable: dict[str, Any]) -> tuple[str | None, str | None]:
    surface = deliverable.get("surface")
    direction = deliverable.get("visual_direction")
    deck_mode = deliverable.get("deck_mode", direction)
    if surface == "deck":
        if deck_mode == "magazine":
            return "guizang-ppt-skill", None
        if deck_mode == "template":
            return "html-ppt", None
        if deck_mode == "branded":
            return "huashu-design", None
        return None, "deck requires explicit deck_mode: magazine, template, or branded"
    if surface in {"video", "image"} or deliverable.get("media_export"):
        return "huashu-design", None
    if surface in {
        "product-ui",
        "mobile-ui",
        "dashboard",
        "detail",
        "table",
        "schedule",
        "marketing-web",
    }:
        return "frontend-design", None
    return None, "unknown or unsupported surface"


def _provenance(task_id: str) -> dict[str, str]:
    return {
        "catalog": "design-skill-catalog.yaml",
        "schema": SCHEMA_REF,
        "mode": "manual-shadow",
        "task_id": task_id,
    }


def _record(
    deliverable_id: str,
    status: str,
    reason: str,
    task_id: str,
    usage_claim: dict[str, Any],
) -> dict[str, Any]:
    """Build the common shape for non-selected records."""
    return {
        "deliverable_id": deliverable_id,
        "status": status,
        "reason": reason,
        "visual_author": None,
        "baselines": [],
        "overlays": [],
        "gates": [],
        "gate_note": None,
        "usage_claim": usage_claim,
        "provenance": _provenance(task_id),
    }


def _unsupported(
    entries: dict[str, dict[str, Any]], surface: str, skills: list[str]
) -> str | None:
    for skill in skills:
        entry = entries.get(skill)
        if entry is None:
            return f"catalog lacks selected skill: {skill}"
        supported = entry.get("surface")
        if not isinstance(supported, list) or surface not in supported:
            return f"{skill} does not support surface {surface}"
    return None


def _gates_for(
    entries: dict[str, dict[str, Any]],
    surface: str,
    *,
    magazine: bool,
    html_motion: bool,
) -> list[str]:
    """Select only catalog-supported advisory gates for this surface."""
    if surface == "video":
        candidates = ["review-animations"] if html_motion else []
    else:
        candidates = ["design-review"]
    if magazine:
        candidates.insert(0, "plan-design-review")
    return [
        skill for skill in candidates
        if isinstance(entries.get(skill, {}).get("surface"), list)
        and surface in entries[skill]["surface"]
    ]


def _allowed_surfaces(entries: dict[str, dict[str, Any]]) -> set[str]:
    """Return the finite surface vocabulary declared by the catalog."""
    allowed: set[str] = set()
    for entry in entries.values():
        surfaces = entry.get("surface")
        if isinstance(surfaces, list):
            allowed.update(surface for surface in surfaces if isinstance(surface, str))
    return allowed


def _invalid_deliverable_contract(
    raw: dict[str, Any], entries: dict[str, dict[str, Any]]
) -> str | None:
    """Reject malformed structured fields before they can alter selection."""
    surface = raw.get("surface")
    if not isinstance(surface, str) or not surface:
        return "surface is required"
    if surface not in _allowed_surfaces(entries):
        return f"surface must be a catalogued value, got {surface!r}"

    language = raw.get("language", "latin")
    if language not in VALID_LANGUAGES:
        return "language must be one of: cjk, latin"

    if "visual_direction" in raw and raw["visual_direction"] not in VALID_VISUAL_DIRECTIONS:
        return "visual_direction must be one of: apple, magazine, template, branded"
    if "deck_mode" in raw and raw["deck_mode"] not in VALID_DECK_MODES:
        return "deck_mode must be one of: magazine, template, branded"
    if "motion_source" in raw and raw["motion_source"] not in VALID_MOTION_SOURCES:
        return "motion_source must be html-interface when supplied"
    for field in BOOLEAN_DELIVERABLE_FIELDS:
        if field in raw and not isinstance(raw[field], bool):
            return f"{field} must be a boolean"
    return None


def _validate_task_contract(task: dict[str, Any]) -> str:
    """Validate top-level inputs that apply to every record, fail closed."""
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task.id must be a non-empty string")
    if "usage_claim" in task and not isinstance(task["usage_claim"], bool):
        raise ValueError("task.usage_claim must be a boolean")
    if "evidence" in task and not isinstance(task["evidence"], list):
        raise ValueError("task.evidence must be a list")
    return task_id


def select(task: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic records for an explicit contract; never invokes skills."""
    if not isinstance(task, dict):
        raise ValueError("task must be a mapping")
    entries = catalog_entries(catalog)
    task_id = _validate_task_contract(task)
    deliverables = task.get("deliverables")
    if not isinstance(deliverables, list) or not deliverables:
        raise ValueError("task.deliverables must be a non-empty list")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(deliverables):
        if not isinstance(raw, dict):
            deliverable_id = f"invalid-{index}"
            records.append(
                _record(
                    deliverable_id,
                    "invalid",
                    "deliverable must be a mapping",
                    task_id,
                    _usage_claim(task, deliverable_id, [], entries),
                )
            )
            continue
        deliverable_id = raw.get("id")
        if (
            not isinstance(deliverable_id, str)
            or not deliverable_id.strip()
            or deliverable_id in seen
        ):
            invalid_id = str(deliverable_id or f"invalid-{index}")
            records.append(
                _record(
                    invalid_id,
                    "invalid",
                    "deliverable id must be unique and non-empty",
                    task_id,
                    _usage_claim(task, invalid_id, [], entries),
                )
            )
            continue
        seen.add(deliverable_id)
        invalid_contract = _invalid_deliverable_contract(raw, entries)
        if invalid_contract:
            records.append(
                _record(
                    deliverable_id,
                    "invalid",
                    invalid_contract,
                    task_id,
                    _usage_claim(task, deliverable_id, [], entries),
                )
            )
            continue
        surface = raw["surface"]
        if raw.get("needs_direction") is True:
            records.append(
                _record(
                    deliverable_id,
                    "needs_direction",
                    "task contract explicitly marks visual direction unresolved",
                    task_id,
                    _usage_claim(task, deliverable_id, [], entries),
                )
            )
            continue
        author, reason = _author_for(raw)
        if author is None:
            records.append(
                _record(
                    deliverable_id,
                    "needs_direction",
                    reason or "selection requires direction",
                    task_id,
                    _usage_claim(task, deliverable_id, [], entries),
                )
            )
            continue
        language = raw.get("language", "latin")
        apple = raw.get("visual_direction") == "apple"
        baselines = _baseline(
            entries, language, erp=bool(raw.get("erp", False)), apple=apple
        )
        overlays = (
            _apple_overlay(entries, has_cjk_baseline=bool(baselines))
            if apple
            else []
        )
        gates = _gates_for(
            entries,
            surface,
            html_motion=raw.get("motion_source") == "html-interface",
            magazine=(
                surface == "deck"
                and raw.get("deck_mode", raw.get("visual_direction")) == "magazine"
            ),
        )
        required_skills = [
            author,
            *[item["skill"] for item in baselines],
            *[item["skill"] for item in overlays],
            *gates,
        ]
        unsupported = _unsupported(entries, surface, required_skills)
        if unsupported:
            records.append(
                _record(
                    deliverable_id,
                    "needs_direction",
                    unsupported,
                    task_id,
                    _usage_claim(task, deliverable_id, [], entries),
                )
            )
            continue
        records.append(
            {
                "deliverable_id": deliverable_id,
                "status": "selected",
                "reason": None,
                "visual_author": author,
                "baselines": baselines,
                "overlays": overlays,
                "gates": gates,
                "gate_note": (
                    f"No catalogued gate supports this {surface} scope; "
                    "choose a manual review before publication."
                    if surface in {"image", "video"} and not gates
                    else None
                ),
                "usage_claim": _usage_claim(
                    task, deliverable_id, required_skills, entries
                ),
                "provenance": _provenance(task_id),
            }
        )
    return {
        "version": 1,
        "mode": "manual-shadow",
        "task_id": task_id,
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, type=Path, help="Explicit YAML/JSON task contract"
    )
    parser.add_argument("--output", type=Path, help="Write YAML record instead of stdout")
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    args = parser.parse_args()
    try:
        source = load_mapping(args.input)
        task = source.get("task", source)
        if not isinstance(task, dict):
            raise ValueError("input task must be a mapping")
        record = select(task, load_mapping(args.catalog))
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    rendered = yaml.safe_dump(record, allow_unicode=True, sort_keys=False)
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
