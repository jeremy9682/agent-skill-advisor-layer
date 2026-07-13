#!/usr/bin/env python3
"""Deterministic Phase-1.5 design selection in manual shadow mode.

It accepts an explicit task contract and writes auditable selection records.
It deliberately does not parse natural-language prompts, call an LLM, make a
network request, hook prompt submission, or invoke any skill.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "design-skill-catalog.yaml"
SCHEMA_REF = "schemas/design-selection-record.md"
VALID_EVIDENCE_KINDS = {"read", "invocation", "artifact"}


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


def _usage_claim(task: dict[str, Any]) -> dict[str, Any]:
    requested = bool(task.get("usage_claim", False))
    evidence = task.get("evidence", [])
    evidence = evidence if isinstance(evidence, list) else []
    accepted: list[dict[str, str]] = []
    seen_kinds: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        kind, path_text = item.get("kind"), item.get("path")
        if (
            kind not in VALID_EVIDENCE_KINDS
            or not isinstance(path_text, str)
            or not path_text.strip()
        ):
            continue
        declared_path = Path(path_text).expanduser()
        resolved_path = declared_path if declared_path.is_absolute() else ROOT / declared_path
        if not resolved_path.exists() or kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        retained_path = (
            str(resolved_path.resolve())
            if declared_path.is_absolute()
            else str(declared_path)
        )
        accepted.append({"kind": kind, "path": retained_path})
    permitted = not requested or bool(accepted)
    return {
        "requested": requested,
        "permitted": permitted,
        "accepted_evidence_kinds": [item["kind"] for item in accepted],
        "accepted_evidence": accepted,
        "reason": (
            None
            if permitted
            else "usage claim requires existing read, invocation, or artifact evidence"
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
    entries: dict[str, dict[str, Any]], surface: str, *, magazine: bool
) -> list[str]:
    """Select only catalog-supported advisory gates for this surface."""
    candidates = ["design-review"]
    if magazine:
        candidates.insert(0, "plan-design-review")
    return [
        skill for skill in candidates
        if isinstance(entries.get(skill, {}).get("surface"), list)
        and surface in entries[skill]["surface"]
    ]


def select(task: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic records for an explicit contract; never invokes skills."""
    entries = catalog_entries(catalog)
    deliverables = task.get("deliverables")
    if not isinstance(deliverables, list) or not deliverables:
        raise ValueError("task.deliverables must be a non-empty list")
    task_id = task.get("id", "unnamed-task")
    usage_claim = _usage_claim(task)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(deliverables):
        if not isinstance(raw, dict):
            records.append(
                _record(
                    f"invalid-{index}",
                    "invalid",
                    "deliverable must be a mapping",
                    task_id,
                    usage_claim,
                )
            )
            continue
        deliverable_id = raw.get("id")
        if not isinstance(deliverable_id, str) or not deliverable_id or deliverable_id in seen:
            records.append(
                _record(
                    str(deliverable_id or f"invalid-{index}"),
                    "invalid",
                    "deliverable id must be unique and non-empty",
                    task_id,
                    usage_claim,
                )
            )
            continue
        seen.add(deliverable_id)
        surface = raw.get("surface")
        if not isinstance(surface, str) or not surface:
            records.append(_record(deliverable_id, "invalid", "surface is required", task_id, usage_claim))
            continue
        if raw.get("needs_direction") is True:
            records.append(
                _record(
                    deliverable_id,
                    "needs_direction",
                    "task contract explicitly marks visual direction unresolved",
                    task_id,
                    usage_claim,
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
                    usage_claim,
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
                    usage_claim,
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
                "usage_claim": usage_claim,
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
    parser.add_argument("--input", required=True, type=Path, help="Explicit YAML/JSON task contract")
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
