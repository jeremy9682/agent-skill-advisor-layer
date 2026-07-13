#!/usr/bin/env python3
"""Offline consistency audit for the phase-1 design skill catalog.

This script deliberately does not route prompts or load skill bodies. It checks
that the small machine catalog agrees with the live skill manifest, the legacy
Claude design decision table, and the design selection eval contract.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "design-skill-catalog.yaml"
DEFAULT_POLICY = ROOT / "routing-policy.yaml"
DEFAULT_MANIFEST = Path.home() / ".codex" / "skill-governance" / "skills-manifest.json"
DEFAULT_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
DEFAULT_EVALS = ROOT / "routing-evals" / "design-cases.yaml"

CALL_POLICIES = {
    "auto-eligible",
    "manual-confirm",
    "suggest-confirm",
    "router",
    "explicit-only",
}
ROLES = {"direction", "author", "adapter", "overlay", "gate"}
LIFECYCLES = {"candidate", "evaluated", "approved", "deprecated"}
SOURCE_KINDS = {"upstream", "local-derivative"}
SURFACES = {
    "product-ui",
    "mobile-ui",
    "dashboard",
    "detail",
    "table",
    "schedule",
    "marketing-web",
    "deck",
    "image",
    "video",
}
PROTECTION_RANK = {
    "auto-eligible": 0,
    "manual-confirm": 1,
    "suggest-confirm": 1,
    "explicit-only": 2,
}
PREMATURE_PHASE1_FIELDS = {
    "compatible_with",
    "conflicts_with",
    "does_not_own",
    "input_types",
    "output_types",
    "phase",
    "requires",
}
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"{path}: expected JSON object with entries[]")
    return data


def expand_path(value: str) -> str:
    return str(Path(value).expanduser())


def evidence_path(value: str) -> Path:
    path_text = value.split("#", 1)[0]
    path = Path(path_text).expanduser()
    return path if path.is_absolute() else ROOT / path


def extract_claude_design_table_skills(text: str) -> set[str]:
    """Extract backticked skill labels from CLAUDE.md's design decision table."""
    marker = "### 选 skill 的决策表"
    if marker not in text:
        return set()
    section = text.split(marker, 1)[1]
    section = section.split("### ", 1)[0]
    names: set[str] = set()
    for line in section.splitlines():
        if line.lstrip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                names.update(re.findall(r"`([^`]+)`", cells[-1]))
    return names


def entry_refs(entry: dict[str, Any]) -> set[str]:
    refs = {str(entry.get("id", "")), str(entry.get("name", ""))}
    aliases = entry.get("aliases", [])
    if isinstance(aliases, list):
        refs.update(str(alias) for alias in aliases)
    return {ref for ref in refs if ref}


def is_protection_tightening(manifest_policy: str, catalog_policy: str) -> bool:
    """Return true only for an explicit move to a stricter invocation gate."""
    if manifest_policy == catalog_policy:
        return True
    if "router" in {manifest_policy, catalog_policy}:
        return False
    return PROTECTION_RANK.get(catalog_policy, -1) > PROTECTION_RANK.get(
        manifest_policy, -1
    )


def _validate_catalog_shape(catalog: dict[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    if catalog.get("schema_version") != 1:
        errors.append("catalog.schema_version must equal 1")
    if catalog.get("canon_ref") != "routing-policy.yaml#design_domain":
        errors.append("catalog.canon_ref must point to routing-policy.yaml#design_domain")
    if catalog.get("status") != "phase1_offline":
        errors.append("catalog.status must be phase1_offline")
    if catalog.get("runtime_consumer") not in (None, "none"):
        errors.append("phase 1 must not declare a runtime consumer")

    invariants = catalog.get("invariants")
    if not isinstance(invariants, dict):
        errors.append("catalog.invariants must be a mapping")
    else:
        if invariants.get("max_visual_authors_per_deliverable") != 1:
            errors.append("max_visual_authors_per_deliverable must equal 1")
        if invariants.get("baseline_precedes_style_overlay") is not True:
            errors.append("baseline_precedes_style_overlay must be true")
        if invariants.get("overlay_has_visual_naming_authority") is not False:
            errors.append("overlay_has_visual_naming_authority must be false")
        if invariants.get("handoff_requires_new_selection_record") is not True:
            errors.append("design author handoff must require a new selection record")
        if invariants.get("usage_claim_requires_read_or_invocation_evidence") is not True:
            errors.append("usage claims must require read or invocation evidence")

    allowed_values = catalog.get("allowed_values")
    expected_values = {
        "role": ROLES,
        "call_policy": CALL_POLICIES,
        "lifecycle": LIFECYCLES,
        "source_kind": SOURCE_KINDS,
        "surface": SURFACES,
    }
    if not isinstance(allowed_values, dict):
        errors.append("catalog.allowed_values must be a mapping")
    else:
        for key, expected in expected_values.items():
            actual = allowed_values.get(key)
            if not isinstance(actual, list) or set(actual) != expected:
                errors.append(f"allowed_values.{key} must equal {sorted(expected)}")

    observed = catalog.get("sources_observed", [])
    if not isinstance(observed, list):
        errors.append("catalog.sources_observed must be a list")
    else:
        for index, source in enumerate(observed):
            if not isinstance(source, dict):
                errors.append(f"sources_observed[{index}] must be a mapping")
                continue
            if source.get("enforcement") != "observe-only":
                errors.append(f"sources_observed[{index}] must remain observe-only in phase 1")
            if source.get("kind") == "plugin-cache" and not source.get("version"):
                errors.append(f"sources_observed[{index}] plugin-cache source needs version")

    entries = catalog.get("design_skills")
    if not isinstance(entries, list):
        errors.append("catalog.design_skills must be a list")
        return []
    if not 1 <= len(entries) <= 12:
        errors.append("phase-1 catalog must contain 1..12 design skills")
    return [entry for entry in entries if isinstance(entry, dict)]


def _validate_entry(
    entry: dict[str, Any],
    manifest_entries: list[dict[str, Any]],
    errors: list[str],
) -> None:
    skill_id = entry.get("id")
    name = entry.get("name")
    label = skill_id or name or "<unnamed>"
    if not isinstance(skill_id, str) or not ID_RE.fullmatch(skill_id):
        errors.append(f"{label}: invalid id")
    if not isinstance(name, str) or not name:
        errors.append(f"{label}: name is required")
        return

    role = entry.get("role")
    if role not in ROLES:
        errors.append(f"{label}: role must be one of {sorted(ROLES)}")
    surfaces = entry.get("surface")
    if not isinstance(surfaces, list) or not surfaces or not all(isinstance(v, str) for v in surfaces):
        errors.append(f"{label}: surface must be a non-empty string list")
    elif not set(surfaces).issubset(SURFACES):
        errors.append(f"{label}: unknown surface values: {sorted(set(surfaces) - SURFACES)}")
    owns = entry.get("owns")
    if not isinstance(owns, list) or not owns or not all(isinstance(v, str) for v in owns):
        errors.append(f"{label}: owns must be a non-empty string list")
    if entry.get("call_policy") not in CALL_POLICIES:
        errors.append(f"{label}: call_policy must reuse the manifest vocabulary")
    lifecycle = entry.get("lifecycle")
    if lifecycle not in LIFECYCLES:
        errors.append(f"{label}: invalid lifecycle")
    evidence = entry.get("evidence")
    if lifecycle in {"evaluated", "approved"}:
        if not isinstance(evidence, str) or not evidence:
            errors.append(f"{label}: {lifecycle} lifecycle requires evidence")
        elif not evidence_path(evidence).exists():
            errors.append(f"{label}: evidence path does not exist: {evidence}")

    forbidden = PREMATURE_PHASE1_FIELDS.intersection(entry)
    if forbidden:
        errors.append(f"{label}: phase-1 premature fields present: {sorted(forbidden)}")

    source = entry.get("source")
    if not isinstance(source, dict):
        errors.append(f"{label}: source must be a mapping")
        source = {}
    source_kind = source.get("kind")
    if source_kind not in SOURCE_KINDS:
        errors.append(f"{label}: source.kind must be one of {sorted(SOURCE_KINDS)}")
    commit = source.get("installed_commit")
    if commit is not None and (not isinstance(commit, str) or not HEX40_RE.fullmatch(commit)):
        errors.append(f"{label}: installed_commit must be null or a 40-char lowercase SHA")
    source_hash = source.get("tree_hash")
    if source_hash is not None and (
        not isinstance(source_hash, str) or not HEX64_RE.fullmatch(source_hash)
    ):
        errors.append(f"{label}: source.tree_hash must be a 64-char lowercase SHA-256")
    if source_kind == "local-derivative" and commit is not None:
        errors.append(f"{label}: local-derivative must not invent installed_commit")
    if source_kind == "upstream" and (
        not source.get("repository") or not source.get("installed_commit")
    ):
        errors.append(f"{label}: upstream source needs repository and installed_commit")

    installations = entry.get("installations")
    if not isinstance(installations, list) or not installations:
        errors.append(f"{label}: installations must be a non-empty list")
        return

    matched: list[dict[str, Any]] = []
    for index, installation in enumerate(installations):
        if not isinstance(installation, dict):
            errors.append(f"{label}: installations[{index}] must be a mapping")
            continue
        runtime = installation.get("runtime")
        path = installation.get("path")
        if not isinstance(runtime, str) or not isinstance(path, str):
            errors.append(f"{label}: installations[{index}] needs runtime and path")
            continue
        expanded = expand_path(path)
        candidates = [
            row
            for row in manifest_entries
            if row.get("runtime") == runtime
            and row.get("name") == name
            and row.get("path") == expanded
        ]
        if len(candidates) != 1:
            errors.append(
                f"{label}: installation {runtime}:{expanded} matched {len(candidates)} manifest entries"
            )
            continue
        row = candidates[0]
        matched.append(row)
        tree_hash = installation.get("tree_hash")
        if not isinstance(tree_hash, str) or not HEX64_RE.fullmatch(tree_hash):
            errors.append(f"{label}: installation {runtime}:{expanded} needs a 64-char tree_hash")
        elif tree_hash != row.get("tree_hash"):
            errors.append(f"{label}: installation tree_hash drift at {runtime}:{expanded}")
        manifest_policy = row.get("call_policy")
        catalog_policy = entry.get("call_policy")
        declared_from = entry.get("manifest_call_policy")
        if declared_from is not None and declared_from != manifest_policy:
            errors.append(
                f"{label}: stale manifest_call_policy at {runtime}:{expanded}"
            )
        if manifest_policy != catalog_policy:
            reason = entry.get("call_policy_reason")
            if (
                declared_from != manifest_policy
                or not isinstance(reason, str)
                or not reason
                or not is_protection_tightening(str(manifest_policy), str(catalog_policy))
            ):
                errors.append(
                    f"{label}: call_policy override at {runtime}:{expanded} must declare "
                    "the live manifest policy, a reason, and only tighten protection"
                )

    if commit is not None:
        heads = {row.get("git_head") for row in matched if row.get("git_head")}
        if heads and heads != {commit}:
            errors.append(f"{label}: installed_commit disagrees with manifest git_head {sorted(heads)}")
    repository = source.get("repository")
    if repository:
        remotes = {row.get("git_remote") for row in matched if row.get("git_remote")}
        if remotes and remotes != {repository}:
            errors.append(f"{label}: repository disagrees with manifest git_remote {sorted(remotes)}")
    if source_hash is not None:
        installation_hashes = {
            installation.get("tree_hash")
            for installation in installations
            if isinstance(installation, dict)
        }
        if installation_hashes != {source_hash}:
            errors.append(
                f"{label}: non-null source.tree_hash must match every installation tree_hash"
            )


def validate_catalog(
    catalog: dict[str, Any],
    manifest: dict[str, Any],
    claude_md_text: str | None = None,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    errors: list[str] = []
    entries = _validate_catalog_shape(catalog, errors)
    ids: set[str] = set()
    refs: dict[str, dict[str, Any]] = {}
    manifest_entries = [row for row in manifest.get("entries", []) if isinstance(row, dict)]

    for entry in entries:
        skill_id = entry.get("id")
        if isinstance(skill_id, str):
            if skill_id in ids:
                errors.append(f"duplicate catalog id: {skill_id}")
            ids.add(skill_id)
        for ref in entry_refs(entry):
            if ref in refs and refs[ref] is not entry:
                errors.append(f"catalog reference collision: {ref}")
            refs[ref] = entry
        _validate_entry(entry, manifest_entries, errors)

    if claude_md_text is not None:
        table_skills = extract_claude_design_table_skills(claude_md_text)
        if not table_skills:
            errors.append("CLAUDE.md design decision table was not found or was empty")
        missing = sorted(table_skills - refs.keys())
        if missing:
            errors.append(f"CLAUDE.md design table skills missing from catalog: {missing}")

    apple = refs.get("apple-design")
    if apple:
        if apple.get("role") != "overlay":
            errors.append("apple-design must be an overlay, not a visual author")
        if "typography-micro" not in apple.get("owns", []):
            errors.append("apple-design must disclose its typography-micro ownership")
    baselines = catalog.get("baseline_skill_ids", [])
    if not isinstance(baselines, list) or not baselines:
        errors.append("baseline_skill_ids must be a non-empty list")
        baselines = []
    for baseline_ref in baselines:
        target = refs.get(str(baseline_ref))
        if not target:
            errors.append(f"unknown baseline_skill_id: {baseline_ref}")
        elif target.get("role") != "overlay":
            errors.append(f"baseline {baseline_ref} must use overlay role in phase 1")
    if "design-systems" not in baselines and not any(
        refs.get(ref, {}).get("name") == "design-systems" for ref in baselines
    ):
        errors.append("design-systems must be declared in baseline_skill_ids")

    return errors, refs


def validate_policy(policy: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    design_domain = policy.get("design_domain")
    if not isinstance(design_domain, dict):
        return ["routing-policy.yaml must define design_domain"]
    expected = {
        "status": catalog.get("status"),
        "catalog": "design-skill-catalog.yaml",
        "selection_evals": "routing-evals/design-cases.yaml",
        "runtime_consumer": "none",
    }
    for key, value in expected.items():
        if design_domain.get(key) != value:
            errors.append(
                f"routing policy design_domain.{key}={design_domain.get(key)!r}, expected {value!r}"
            )
    if design_domain.get("invariants") != catalog.get("invariants"):
        errors.append("routing policy and catalog design invariants must match exactly")
    return errors


def validate_evals(
    evals: dict[str, Any],
    catalog: dict[str, Any],
    refs: dict[str, dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    if evals.get("version") != 1:
        errors.append("design evals version must equal 1")
    if evals.get("contract_kind") != "oracle-only":
        errors.append("phase-1 design evals must identify as oracle-only contracts")
    if evals.get("catalog") != "../design-skill-catalog.yaml":
        errors.append("design evals catalog reference must equal ../design-skill-catalog.yaml")
    cases = evals.get("cases")
    if not isinstance(cases, list) or not 3 <= len(cases) <= 5:
        return errors + ["phase-1 design evals must contain 3..5 cases"]

    seen: set[str] = set()
    apple_case_found = False
    locked_apple_case_found = False
    baseline_ids = set(catalog.get("baseline_skill_ids", []))
    evidence_required = catalog.get("invariants", {}).get(
        "usage_claim_requires_read_or_invocation_evidence"
    ) is True
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"design eval case {index} must be a mapping")
            continue
        case_id = case.get("id")
        prompt = case.get("prompt")
        label = case_id or f"case[{index}]"
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{label}: id is required")
        elif case_id in seen:
            errors.append(f"duplicate design eval id: {case_id}")
        else:
            seen.add(case_id)
        if not isinstance(prompt, str) or not prompt:
            errors.append(f"{label}: prompt is required")
            prompt = ""

        expect = case.get("expect")
        if not isinstance(expect, dict):
            errors.append(f"{label}: expect must be a mapping")
            continue
        author_ref = expect.get("visual_author")
        if isinstance(author_ref, list):
            errors.append(f"{label}: visual_author must be a scalar, never a list")
            author_ref = None
        author = refs.get(str(author_ref)) if author_ref else None
        if not author or author.get("role") != "author":
            errors.append(f"{label}: visual_author must resolve to an author skill")

        selected_constraint_entries: list[dict[str, Any]] = [author] if author else []
        for key, role in (("baselines", "overlay"), ("overlays", "overlay"), ("gates", "gate")):
            values = expect.get(key, [])
            if not isinstance(values, list):
                errors.append(f"{label}: expect.{key} must be a list")
                continue
            for ref in values:
                target = refs.get(str(ref))
                if not target:
                    errors.append(f"{label}: unknown {key} reference: {ref}")
                elif target.get("role") != role:
                    errors.append(f"{label}: {ref} has role {target.get('role')}, expected {role}")
                else:
                    if key == "baselines" and target.get("id") not in baseline_ids:
                        errors.append(f"{label}: {ref} is not declared in baseline_skill_ids")
                    if key == "overlays" and target.get("id") in baseline_ids:
                        errors.append(f"{label}: baseline {ref} cannot be used as an optional overlay")
                    if key in {"baselines", "overlays"}:
                        selected_constraint_entries.append(target)

        facet_owners: dict[str, list[str]] = {}
        for target in selected_constraint_entries:
            for facet in target.get("owns", []):
                facet_owners.setdefault(str(facet), []).append(str(target.get("name")))
        for facet, owners in facet_owners.items():
            if len(owners) > 1:
                errors.append(f"{label}: overlapping constraint facet {facet}: {owners}")

        forbid = case.get("forbid", {})
        forbidden_authors = forbid.get("visual_author", []) if isinstance(forbid, dict) else []
        if not isinstance(forbidden_authors, list):
            errors.append(f"{label}: forbid.visual_author must be a list")
            forbidden_authors = []
        for ref in forbidden_authors:
            if str(ref) not in refs:
                errors.append(f"{label}: unknown forbidden visual_author: {ref}")
        if author_ref in forbidden_authors:
            errors.append(f"{label}: expected author is also forbidden")

        require = case.get("require", {})
        if evidence_required and (
            not isinstance(require, dict) or require.get("invocation_evidence") is not True
        ):
            errors.append(f"{label}: selection contract must require invocation evidence")

        if re.search(r"(?i)(?<![A-Za-z0-9_])apple(?![A-Za-z0-9_])", prompt) and (
            "中文" in prompt or "CJK" in prompt.upper()
        ):
            apple_case_found = True
            if author_ref in {"apple-design", "emilkowalski/apple-design"}:
                errors.append(f"{label}: apple-design cannot be the visual author")
            if "apple-design" not in expect.get("overlays", []):
                errors.append(f"{label}: Apple+CJK case must suggest apple-design as overlay")
            if "design-systems" not in expect.get("baselines", []):
                errors.append(f"{label}: Apple+CJK case must include design-systems baseline")
            if not isinstance(require, dict) or require.get("invocation_evidence") is not True:
                errors.append(f"{label}: Apple+CJK claim must require invocation evidence")
        if case_id == "apple-style-chinese-product-ui":
            locked_apple_case_found = True
            expected_contract = {
                "visual_author": "frontend-design",
                "baselines": ["design-systems"],
                "overlays": ["apple-design"],
                "gates": ["design-review"],
            }
            if expect != expected_contract:
                errors.append(
                    f"{label}: locked Apple+CJK oracle must equal {expected_contract}"
                )
            if "apple-design" not in forbidden_authors:
                errors.append(f"{label}: apple-design must be forbidden as visual_author")
    if not apple_case_found:
        errors.append("design evals need an Apple-style Chinese/CJK regression case")
    if not locked_apple_case_found:
        errors.append("design evals need the locked apple-style-chinese-product-ui oracle")
    return errors


def audit(
    catalog_path: Path,
    policy_path: Path,
    manifest_path: Path,
    claude_md_path: Path,
    evals_path: Path,
) -> dict[str, Any]:
    catalog = load_yaml(catalog_path)
    policy = load_yaml(policy_path)
    manifest = load_manifest(manifest_path)
    claude_text = claude_md_path.read_text()
    evals = load_yaml(evals_path)
    errors, refs = validate_catalog(catalog, manifest, claude_text)
    errors.extend(validate_policy(policy, catalog))
    errors.extend(validate_evals(evals, catalog, refs))
    return {
        "status": "passed" if not errors else "failed",
        "catalog_entries": len(catalog.get("design_skills", [])),
        "eval_cases": len(evals.get("cases", [])),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--claude-md", type=Path, default=DEFAULT_CLAUDE_MD)
    parser.add_argument("--evals", type=Path, default=DEFAULT_EVALS)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()
    try:
        report = audit(args.catalog, args.policy, args.manifest, args.claude_md, args.evals)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        report = {"status": "failed", "errors": [str(exc)]}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif report["status"] == "passed":
        print(
            "design catalog audit: passed "
            f"({report['catalog_entries']} entries, {report['eval_cases']} eval cases)"
        )
    else:
        print("design catalog audit: failed", file=sys.stderr)
        for error in report.get("errors", []):
            print(f"- {error}", file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
