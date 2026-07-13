from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    path = ROOT / "scripts" / "design_catalog_audit.py"
    spec = importlib.util.spec_from_file_location("design_catalog_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_entry(name: str, role: str, tree_hash: str, path: Path) -> dict:
    owns = {
        "frontend-design": ["page-layout"],
        "design-systems": ["cjk-typography"],
        "apple-design": ["motion-physics", "typography-micro"],
        "design-review": ["visual-verdict"],
    }[name]
    return {
        "id": f"fixture/{name}",
        "name": name,
        "source": {
            "kind": "local-derivative",
            "repository": None,
            "installed_commit": None,
            "path": str(path),
            "tree_hash": tree_hash,
            "update_policy": "git-managed",
        },
        "installations": [
            {"runtime": "codex", "path": str(path), "tree_hash": tree_hash}
        ],
        "role": role,
        "surface": ["product-ui"],
        "owns": owns,
        "call_policy": "auto-eligible",
        "lifecycle": "candidate",
        "evidence": None,
    }


def build_fixture(tmp_path: Path) -> tuple[dict, dict, str, dict]:
    specs = [
        ("frontend-design", "author", "1" * 64),
        ("design-systems", "overlay", "2" * 64),
        ("apple-design", "overlay", "3" * 64),
        ("design-review", "gate", "4" * 64),
    ]
    entries = [
        fixture_entry(name, role, tree_hash, tmp_path / name)
        for name, role, tree_hash in specs
    ]
    catalog = {
        "schema_version": 1,
        "canon_ref": "routing-policy.yaml#design_domain",
        "status": "phase1_offline",
        "runtime_consumer": "none",
        "invariants": {
            "max_visual_authors_per_deliverable": 1,
            "baseline_precedes_style_overlay": True,
            "overlay_has_visual_naming_authority": False,
            "handoff_requires_new_selection_record": True,
            "usage_claim_requires_read_or_invocation_evidence": True,
        },
        "allowed_values": {
            "role": sorted({"direction", "author", "adapter", "overlay", "gate"}),
            "call_policy": sorted(
                {"auto-eligible", "manual-confirm", "suggest-confirm", "router", "explicit-only"}
            ),
            "lifecycle": sorted({"candidate", "evaluated", "approved", "deprecated"}),
            "source_kind": sorted({"upstream", "local-derivative"}),
            "update_policy": sorted(
                {"git-managed", "merge-only", "review-then-ff-only", "source-managed"}
            ),
            "surface": sorted(
                {
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
            ),
        },
        "baseline_skill_ids": ["fixture/design-systems"],
        "sources_observed": [
            {
                "id": "fixture/plugin",
                "kind": "plugin-cache",
                "version": "1.0.0",
                "enforcement": "observe-only",
            }
        ],
        "design_skills": entries,
    }
    manifest = {
        "entries": [
            {
                "runtime": "codex",
                "name": entry["name"],
                "path": entry["installations"][0]["path"],
                "tree_hash": entry["installations"][0]["tree_hash"],
                "call_policy": entry["call_policy"],
            }
            for entry in entries
        ]
    }
    claude_md = """\
### 选 skill 的决策表
| 场景 | 用 |
|---|---|
| UI | `frontend-design` |
| CJK | `design-systems` |
| 动效 | `apple-design` |
| QA | `design-review` |
### 执行规则
"""
    apple = {
        "id": "apple-style-chinese-product-ui",
        "prompt": "做一个 Apple风格中文界面",
        "expect": {
            "visual_author": "frontend-design",
            "baselines": ["design-systems"],
            "overlays": ["apple-design"],
            "gates": ["design-review"],
        },
        "forbid": {"visual_author": ["apple-design"]},
        "require": {"invocation_evidence": True},
    }
    evals = {
        "version": 1,
        "contract_kind": "oracle-only",
        "catalog": "../design-skill-catalog.yaml",
        "cases": [
            apple,
            {**copy.deepcopy(apple), "id": "apple-cjk-2"},
            {**copy.deepcopy(apple), "id": "apple-cjk-3"},
        ],
    }
    return catalog, manifest, claude_md, evals


def test_valid_phase1_fixture_passes(tmp_path):
    audit = load_audit_module()
    catalog, manifest, claude_md, evals = build_fixture(tmp_path)

    errors, refs = audit.validate_catalog(catalog, manifest, claude_md)
    errors.extend(audit.validate_evals(evals, catalog, refs))

    assert errors == []


def test_tree_hash_and_multi_author_drift_fail(tmp_path):
    audit = load_audit_module()
    catalog, manifest, claude_md, evals = build_fixture(tmp_path)
    catalog["design_skills"][0]["installations"][0]["tree_hash"] = "f" * 64
    evals["cases"][0]["expect"]["visual_author"] = ["frontend-design", "apple-design"]

    errors, refs = audit.validate_catalog(catalog, manifest, claude_md)
    errors.extend(audit.validate_evals(evals, catalog, refs))

    assert any("tree_hash drift" in error for error in errors)
    assert any("visual_author must be a scalar" in error for error in errors)


def test_claude_design_table_drift_fails(tmp_path):
    audit = load_audit_module()
    catalog, manifest, claude_md, _ = build_fixture(tmp_path)
    claude_md = claude_md.replace(
        "| QA | `design-review` |", "| QA | `design-review` |\n| New | `missing-design-skill` |"
    )

    errors, _ = audit.validate_catalog(catalog, manifest, claude_md)

    assert any("missing-design-skill" in error for error in errors)


def test_source_kind_and_policy_relaxation_fail_closed(tmp_path):
    audit = load_audit_module()
    catalog, manifest, claude_md, _ = build_fixture(tmp_path)
    apple = next(entry for entry in catalog["design_skills"] if entry["name"] == "apple-design")
    apple["source"]["kind"] = "vendored"
    apple["source"]["installed_commit"] = "a" * 40
    catalog["allowed_values"]["source_kind"].append("vendored")
    manifest_apple = next(row for row in manifest["entries"] if row["name"] == "apple-design")
    manifest_apple["call_policy"] = "explicit-only"

    errors, _ = audit.validate_catalog(catalog, manifest, claude_md)

    assert any("source.kind" in error for error in errors)
    assert any("allowed_values.source_kind" in error for error in errors)
    assert any("only tighten protection" in error for error in errors)


def test_unknown_update_policy_fails_closed(tmp_path):
    audit = load_audit_module()
    catalog, manifest, claude_md, _ = build_fixture(tmp_path)
    catalog["design_skills"][0]["source"]["update_policy"] = "trust-latest"

    errors, _ = audit.validate_catalog(catalog, manifest, claude_md)

    assert any("source.update_policy" in error for error in errors)


def test_call_policy_override_requires_live_fact_and_reason(tmp_path):
    audit = load_audit_module()
    catalog, manifest, claude_md, _ = build_fixture(tmp_path)
    apple = next(entry for entry in catalog["design_skills"] if entry["name"] == "apple-design")
    apple["call_policy"] = "manual-confirm"

    errors, _ = audit.validate_catalog(catalog, manifest, claude_md)

    assert any("call_policy override" in error for error in errors)

    apple["manifest_call_policy"] = "explicit-only"
    errors, _ = audit.validate_catalog(catalog, manifest, claude_md)
    assert any("stale manifest_call_policy" in error for error in errors)


def test_baseline_overlay_swap_and_missing_evidence_fail(tmp_path):
    audit = load_audit_module()
    catalog, manifest, claude_md, evals = build_fixture(tmp_path)
    errors, refs = audit.validate_catalog(catalog, manifest, claude_md)
    assert errors == []
    first = evals["cases"][0]
    first["expect"]["baselines"] = ["apple-design"]
    first["expect"]["overlays"] = ["design-systems"]
    evals["cases"][1]["require"] = {}
    evals["cases"][2]["forbid"]["visual_author"].append("missing-author")

    errors = audit.validate_evals(evals, catalog, refs)

    assert any("not declared in baseline_skill_ids" in error for error in errors)
    assert any("cannot be used as an optional overlay" in error for error in errors)
    assert any("selection contract must require invocation evidence" in error for error in errors)
    assert any("unknown forbidden visual_author" in error for error in errors)
    assert any("locked Apple+CJK oracle" in error for error in errors)


def test_overlapping_constraint_facets_fail(tmp_path):
    audit = load_audit_module()
    catalog, manifest, claude_md, evals = build_fixture(tmp_path)
    author = next(entry for entry in catalog["design_skills"] if entry["name"] == "frontend-design")
    author["owns"].append("motion-physics")
    errors, refs = audit.validate_catalog(catalog, manifest, claude_md)
    assert errors == []

    errors = audit.validate_evals(evals, catalog, refs)

    assert any("overlapping constraint facet motion-physics" in error for error in errors)


def test_policy_and_catalog_must_match_exactly(tmp_path):
    audit = load_audit_module()
    catalog, _, _, _ = build_fixture(tmp_path)
    policy = {
        "design_domain": {
            "status": "drifted",
            "catalog": "design-skill-catalog.yaml",
            "selection_evals": "routing-evals/design-cases.yaml",
            "runtime_consumer": "none",
            "invariants": catalog["invariants"],
        }
    }

    errors = audit.validate_policy(policy, catalog)

    assert any("design_domain.status" in error for error in errors)


def test_source_hash_must_match_every_installation():
    audit = load_audit_module()
    catalog = yaml.safe_load((ROOT / "design-skill-catalog.yaml").read_text())
    apple = next(entry for entry in catalog["design_skills"] if entry["name"] == "apple-design")
    apple["installations"][1]["tree_hash"] = "f" * 64
    manifest = synthetic_manifest(catalog)

    errors, _ = audit.validate_catalog(catalog, manifest, None)

    assert any("must match every installation tree_hash" in error for error in errors)


def test_eval_catalog_reference_and_handoff_invariant_are_locked(tmp_path):
    audit = load_audit_module()
    catalog, manifest, claude_md, evals = build_fixture(tmp_path)
    catalog["invariants"]["handoff_requires_new_selection_record"] = False
    evals["catalog"] = "../wrong.yaml"

    errors, refs = audit.validate_catalog(catalog, manifest, claude_md)
    errors.extend(audit.validate_evals(evals, catalog, refs))

    assert any("handoff" in error for error in errors)
    assert any("catalog reference" in error for error in errors)


def synthetic_manifest(catalog: dict) -> dict:
    rows = []
    for entry in catalog["design_skills"]:
        source = entry["source"]
        for installation in entry["installations"]:
            row = {
                "runtime": installation["runtime"],
                "name": entry["name"],
                "path": str(Path(installation["path"]).expanduser()),
                "tree_hash": installation["tree_hash"],
                "call_policy": entry.get("manifest_call_policy", entry["call_policy"]),
            }
            if source.get("installed_commit"):
                row["git_head"] = source["installed_commit"]
            if source.get("repository"):
                row["git_remote"] = source["repository"]
            rows.append(row)
    return {"entries": rows}


def test_repo_phase1_contract_is_internally_consistent():
    audit = load_audit_module()
    catalog = yaml.safe_load((ROOT / "design-skill-catalog.yaml").read_text())
    evals = yaml.safe_load((ROOT / "routing-evals" / "design-cases.yaml").read_text())
    policy = yaml.safe_load((ROOT / "routing-policy.yaml").read_text())

    errors, refs = audit.validate_catalog(catalog, synthetic_manifest(catalog), None)
    errors.extend(audit.validate_evals(evals, catalog, refs))

    assert errors == []
    assert len(catalog["design_skills"]) == 9
    assert policy["design_domain"]["catalog"] == "design-skill-catalog.yaml"
    assert policy["design_domain"]["selection_evals"] == "routing-evals/design-cases.yaml"
    assert policy["design_domain"]["runtime_consumer"] == "none"
    assert policy["design_domain"]["status"] == catalog["status"]
    assert policy["design_domain"]["invariants"] == catalog["invariants"]
    assert policy["design_domain"]["shadow_mode"] == {
        "status": "phase1_5_manual",
        "selector": "scripts/design_shadow_select.py",
        "selection_record_schema": "schemas/design-selection-record.md",
        "evals": "routing-evals/design-shadow-cases.yaml",
        "runtime_consumer": "none",
    }
    for key in ("selector", "selection_record_schema", "evals"):
        assert (ROOT / policy["design_domain"]["shadow_mode"][key]).is_file()


def test_checked_in_live_audit_evidence_matches_repo_inputs():
    evidence = json.loads(
        (ROOT / "docs" / "intents" / "design-domain-live-audit-20260713.json").read_text()
    )

    assert evidence["kind"] == "point-in-time-live-audit-evidence"
    assert evidence["result"] == {
        "status": "passed",
        "catalog_entries": 9,
        "eval_cases": 5,
        "errors": [],
    }
    for key in ("catalog", "evals"):
        item = evidence["inputs"][key]
        digest = hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest()
        assert item["sha256"] == digest
    for key in ("manifest", "claude_md"):
        digest = evidence["inputs"][key]["sha256"]
        assert len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
