#!/usr/bin/env python3
"""Deterministic Phase-1.5 design selection in manual shadow mode.

It accepts an explicit task contract and writes auditable selection records.
It deliberately does not parse natural-language prompts, call an LLM, make a
network request, hook prompt submission, or invoke any skill.
"""

from __future__ import annotations

import argparse
import json
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


def catalog_names(catalog: dict[str, Any]) -> set[str]:
    entries = catalog.get("design_skills")
    if not isinstance(entries, list):
        raise ValueError("catalog.design_skills must be a list")
    names = {entry.get("name") for entry in entries if isinstance(entry, dict)}
    if not all(isinstance(name, str) and name for name in names):
        raise ValueError("catalog contains an unnamed skill")
    return names


def _usage_claim(task: dict[str, Any]) -> dict[str, Any]:
    requested = bool(task.get("usage_claim", False))
    evidence = task.get("evidence", [])
    evidence = evidence if isinstance(evidence, list) else []
    accepted = [item for item in evidence if isinstance(item, dict) and item.get("kind") in VALID_EVIDENCE_KINDS]
    permitted = not requested or bool(accepted)
    return {
        "requested": requested,
        "permitted": permitted,
        "accepted_evidence_kinds": [item["kind"] for item in accepted],
        "reason": None if permitted else "usage claim requires read, invocation, or artifact evidence",
    }


def _baseline(language: str, *, erp: bool, apple: bool) -> list[dict[str, Any]]:
    if language != "cjk":
        return []
    active = ["cjk-typography", "cjk-spacing"]
    suppressed: list[str] = []
    if erp and not apple:
        active.append("erp-structure")
    else:
        suppressed.append("erp-structure")
    return [{
        "skill": "design-systems",
        "active_facets": active,
        "suppressed_facets": suppressed,
        "precedence_note": "CJK baseline controls typography and forbids negative letter-spacing.",
    }]


def _apple_overlay(*, has_cjk_baseline: bool) -> list[dict[str, Any]]:
    precedence_note = (
        "CJK baseline outranks typography-micro for CJK letter-spacing."
        if has_cjk_baseline
        else "No CJK baseline; typography-micro applies unconstrained."
    )
    return [{
        "skill": "apple-design",
        "active_facets": ["motion-physics", "gesture", "transient-material", "typography-micro"],
        "suppressed_facets": [],
        "precedence_note": precedence_note,
    }]


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
    if surface in {"product-ui", "mobile-ui", "dashboard", "detail", "table", "schedule", "marketing-web"}:
        return "frontend-design", None
    return None, "unknown or unsupported surface"


def select(task: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic records for an explicit contract; never invokes skills."""
    names = catalog_names(catalog)
    deliverables = task.get("deliverables")
    if not isinstance(deliverables, list) or not deliverables:
        raise ValueError("task.deliverables must be a non-empty list")
    task_id = task.get("id", "unnamed-task")
    usage_claim = _usage_claim(task)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(deliverables):
        if not isinstance(raw, dict):
            records.append({"deliverable_id": f"invalid-{index}", "status": "invalid", "reason": "deliverable must be a mapping"})
            continue
        deliverable_id = raw.get("id")
        if not isinstance(deliverable_id, str) or not deliverable_id or deliverable_id in seen:
            records.append({"deliverable_id": str(deliverable_id or f"invalid-{index}"), "status": "invalid", "reason": "deliverable id must be unique and non-empty"})
            continue
        seen.add(deliverable_id)
        surface = raw.get("surface")
        if not isinstance(surface, str) or not surface:
            records.append({"deliverable_id": deliverable_id, "status": "invalid", "reason": "surface is required"})
            continue
        if raw.get("needs_direction") is True:
            records.append({
                "deliverable_id": deliverable_id,
                "status": "needs_direction",
                "reason": "task contract explicitly marks visual direction unresolved",
                "usage_claim": usage_claim,
                "provenance": {"catalog": "design-skill-catalog.yaml", "schema": SCHEMA_REF, "mode": "manual-shadow"},
            })
            continue
        author, reason = _author_for(raw)
        if author is None:
            records.append({
                "deliverable_id": deliverable_id,
                "status": "needs_direction",
                "reason": reason,
                "usage_claim": usage_claim,
                "provenance": {"catalog": "design-skill-catalog.yaml", "schema": SCHEMA_REF, "mode": "manual-shadow"},
            })
            continue
        if author not in names:
            raise ValueError(f"catalog lacks selected author: {author}")
        language = raw.get("language", "latin")
        apple = raw.get("visual_direction") == "apple"
        baselines = _baseline(language, erp=bool(raw.get("erp", False)), apple=apple)
        overlays = _apple_overlay(has_cjk_baseline=bool(baselines)) if apple else []
        gates = ["design-review"]
        if surface == "deck" and raw.get("deck_mode", raw.get("visual_direction")) == "magazine":
            gates.insert(0, "plan-design-review")
        records.append({
            "deliverable_id": deliverable_id,
            "status": "selected",
            "visual_author": author,
            "baselines": baselines,
            "overlays": overlays,
            "gates": gates,
            "usage_claim": usage_claim,
            "provenance": {
                "catalog": "design-skill-catalog.yaml",
                "schema": SCHEMA_REF,
                "mode": "manual-shadow",
                "task_id": task_id,
            },
        })
    return {"version": 1, "mode": "manual-shadow", "task_id": task_id, "records": records}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Explicit YAML/JSON task contract")
    parser.add_argument("--output", type=Path, help="Write YAML record instead of stdout")
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    args = parser.parse_args()
    source = load_mapping(args.input)
    task = source.get("task", source)
    if not isinstance(task, dict):
        raise SystemExit("input task must be a mapping")
    record = select(task, load_mapping(args.catalog))
    rendered = yaml.safe_dump(record, allow_unicode=True, sort_keys=False)
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
