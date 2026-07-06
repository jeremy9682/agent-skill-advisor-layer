#!/usr/bin/env python3
"""Deterministic routing eval + description lint for local agent skills.

This is a LEXICAL BASELINE, not a semantic router. It measures whether the
skill fleet's names/descriptions let a simple retriever surface the right
skill for realistic prompts, and whether suggest-confirm (high-cost) skills
leak into top candidates for prompts that never asked for them.

It answers "did the trigger contract regress", not "is the router smart".

No LLM calls, no network. Safe for CI.

Usage:
  python3 scripts/routing_eval.py                 # run eval against local skills
  python3 scripts/routing_eval.py --lint          # description lint only
  python3 scripts/routing_eval.py --json out.json # machine-readable report
  python3 scripts/routing_eval.py --check         # non-zero exit on failures
  python3 scripts/routing_eval.py --skills-dir D  # eval a specific skills dir
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "routing-evals" / "cases.yaml"

TOP_K = 3
# Ratchet, not aspiration: set at the measured 2026-07-06 baseline (44%).
# Raise it as description fixes land; never lower it.
RECALL_THRESHOLD = 0.40

TRIGGER_CLAUSE_RE = re.compile(
    r"use (this )?(skill|advisor|command|tool)? ?(when|for|to)"
    r"|use when|when the user|when asked|trigger|触发|适用|使用时机",
    re.IGNORECASE,
)
CONFIRM_CLAUSE_RE = re.compile(
    r"approval|approve|confirm|suggest|do not execute|never execute"
    r"|批准|确认|建议|不要自动|不能偷跑",
    re.IGNORECASE,
)
CJK_RE = re.compile(r"[㐀-鿿]")


def load_audit_module() -> Any:
    path = ROOT / "scripts" / "skill_audit.py"
    spec = importlib.util.spec_from_file_location("skill_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tokenize(text: str) -> list[str]:
    """CJK character bigrams + lowercased latin/digit words."""
    tokens: list[str] = []
    for word in re.findall(r"[a-zA-Z0-9_-]+", text):
        tokens.append(word.lower())
    cjk_chars = CJK_RE.findall(text)
    tokens.extend(cjk_chars)
    tokens.extend(a + b for a, b in zip(cjk_chars, cjk_chars[1:]))
    return tokens


class LexicalIndex:
    """IDF-weighted token overlap between a prompt and skill name+description."""

    def __init__(self, skills: list[dict[str, Any]]):
        self.skills = skills
        self.doc_tokens: list[set[str]] = []
        df: dict[str, int] = {}
        for skill in skills:
            toks = set(tokenize(f"{skill['name']} {skill.get('description', '')}"))
            self.doc_tokens.append(toks)
            for t in toks:
                df[t] = df.get(t, 0) + 1
        n = max(len(skills), 1)
        self.idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}

    def rank(self, prompt: str, top_k: int = TOP_K) -> list[tuple[str, float]]:
        q = set(tokenize(prompt))
        scored: list[tuple[str, float]] = []
        for skill, toks in zip(self.skills, self.doc_tokens):
            overlap = q & toks
            if not overlap:
                continue
            score = sum(self.idf.get(t, 0.0) for t in overlap)
            norm = 1.0 + math.log(1 + len(toks))
            scored.append((skill["name"], score / norm))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:top_k]


def parse_cases(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(path.read_text()) or {}
    except ImportError:
        pass
    # Minimal fallback parser for the known cases.yaml shape.
    cases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("candidates:"):
            break  # candidate zone entries are not eval cases
        if re.match(r"^\s*-\s+id:", line):
            current = {"id": line.split("id:", 1)[1].strip()}
            cases.append(current)
        elif current is not None and ":" in line and line.startswith("    "):
            key, val = line.strip().split(":", 1)
            val = val.strip()
            if val.startswith("["):
                items = [v.strip() for v in val.strip("[]").split(",") if v.strip()]
                current[key] = items
            elif val:
                current[key] = val.strip("\"'")
    return {"cases": cases, "candidates": []}


def collect_skills(audit: Any, skills_dir: Path | None) -> list[dict[str, Any]]:
    """Discover skills with supply-chain evidence: root, path, hash, policy."""
    skill_md_paths: list[tuple[str, Path]] = []
    if skills_dir is not None:
        skill_md_paths = [
            (str(skills_dir), p) for p in sorted(skills_dir.glob("*/SKILL.md"))
        ]
    else:
        seen: set[str] = set()
        for entry in audit.discover_skills():
            if entry["name"] in seen:
                continue
            seen.add(entry["name"])
            skill_md_paths.append((entry.get("runtime", "?"), Path(entry["skill_md"])))

    entries: list[dict[str, Any]] = []
    for root, skill_md in skill_md_paths:
        data, issues = audit.parse_frontmatter(skill_md)
        name = data.get("name", skill_md.parent.name)
        description = data.get("description", "")
        try:
            sha = hashlib.sha256(skill_md.read_bytes()).hexdigest()[:16]
        except OSError:
            sha = "unreadable"
        entries.append(
            {
                "name": name,
                "description": description,
                "root": root,
                "path": str(skill_md),
                "frontmatter_issues": issues,
                "sha256": sha,
                "policy": audit.call_policy(name, description, data),
            }
        )
    return entries


def run_eval(
    skills: list[dict[str, Any]], cases: list[dict[str, Any]]
) -> dict[str, Any]:
    index = LexicalIndex(skills)
    policy = {s["name"]: s["policy"] for s in skills}
    installed = set(policy)

    results: list[dict[str, Any]] = []
    recall_hits = recall_total = 0
    gate_events: list[dict[str, Any]] = []
    unexpected_high_cost: list[dict[str, Any]] = []
    known_leak_events: list[dict[str, Any]] = []

    for case in cases:
        expect = list(case.get("expect", []) or [])
        high_cost_ok = set(case.get("high_cost_ok", []) or [])
        known_leaks = set(case.get("known_leaks", []) or [])
        top = index.rank(case["prompt"])
        top_names = [name for name, _ in top]

        expect_installed = [e for e in expect if e in installed]
        skipped = [e for e in expect if e not in installed]
        hit = None
        if expect_installed:
            recall_total += 1
            hit = any(e in top_names for e in expect_installed)
            recall_hits += bool(hit)

        case_high_cost = [n for n in top_names if policy.get(n) == "suggest-confirm"]
        for name in case_high_cost:
            event = {"case": case["id"], "skill": name, "rank": top_names.index(name) + 1}
            gate_events.append(event)
            if name in high_cost_ok:
                continue
            if name in known_leaks:
                event = dict(event, known=True)
                known_leak_events.append(event)
            else:
                unexpected_high_cost.append(event)

        results.append(
            {
                "id": case["id"],
                "prompt": case["prompt"],
                "top": [{"skill": n, "score": round(s, 3)} for n, s in top],
                "recall_hit": hit,
                "skipped_missing_skill": skipped,
                "high_cost_in_top": case_high_cost,
            }
        )

    recall = recall_hits / recall_total if recall_total else 1.0

    # Cross-lingual reachability: a description with zero CJK characters is
    # lexically unreachable from a Chinese prompt (and vice versa). Semantic
    # routers bridge languages; lexical pre-filters and hooks do not.
    no_cjk = sum(
        1 for s in skills if not CJK_RE.search(s.get("description", "") or "")
    )
    return {
        "metric": "lexical-baseline-v1",
        "top_k": TOP_K,
        "skills_evaluated": len(skills),
        "recall_at_k": round(recall, 3),
        "recall_hits": recall_hits,
        "recall_total": recall_total,
        "descriptions_without_cjk": no_cjk,
        "gate_dependency_events": gate_events,
        "known_leaks": known_leak_events,
        "unexpected_high_cost_candidates": unexpected_high_cost,
        "cases": results,
    }


def run_lint(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for skill in skills:
        desc = skill.get("description", "") or ""
        issues: list[str] = []
        if not desc.strip():
            issues.append("L1_missing_description")
        else:
            if not TRIGGER_CLAUSE_RE.search(desc):
                issues.append("L2_no_trigger_clause")
            if skill["policy"] == "suggest-confirm" and not CONFIRM_CLAUSE_RE.search(desc):
                issues.append("L4_high_cost_without_confirm_language")
        if skill.get("frontmatter_issues"):
            issues.extend(f"audit:{i}" for i in skill["frontmatter_issues"])
        if issues:
            findings.append(
                {
                    "name": skill["name"],
                    "root": skill["root"],
                    "path": skill["path"],
                    "sha256": skill["sha256"],
                    "policy": skill["policy"],
                    "frontmatter_issues": skill.get("frontmatter_issues", []),
                    "issues": issues,
                }
            )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--lint", action="store_true", help="lint only, skip eval")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--check", action="store_true", help="exit 1 on failures")
    args = parser.parse_args()

    audit = load_audit_module()
    skills = collect_skills(audit, args.skills_dir)

    report: dict[str, Any] = {
        "skills": [
            {
                "name": s["name"],
                "root": s["root"],
                "path": s["path"],
                "sha256": s["sha256"],
                "policy": s["policy"],
                "frontmatter_issues": s.get("frontmatter_issues", []),
            }
            for s in skills
        ]
    }

    failed = False
    if not args.lint:
        cases = parse_cases(args.cases).get("cases", [])
        eval_report = run_eval(skills, cases)
        report["eval"] = eval_report
        print(f"skills evaluated: {eval_report['skills_evaluated']}")
        print(
            f"recall@{TOP_K}: {eval_report['recall_at_k']:.0%}"
            f" ({eval_report['recall_hits']}/{eval_report['recall_total']})"
        )
        print(
            f"descriptions lexically unreachable from Chinese prompts: "
            f"{eval_report['descriptions_without_cjk']}/{eval_report['skills_evaluated']}"
        )
        print(f"gate-dependency events (suggest-confirm in top-{TOP_K}): "
              f"{len(eval_report['gate_dependency_events'])}")
        for event in eval_report["known_leaks"]:
            print(f"  known leak {event['case']}: {event['skill']} at rank {event['rank']}")
        print(f"unexpected high-cost candidates: "
              f"{len(eval_report['unexpected_high_cost_candidates'])}")
        for event in eval_report["unexpected_high_cost_candidates"]:
            print(f"  !! {event['case']}: {event['skill']} at rank {event['rank']}")
        misses = [c for c in eval_report["cases"] if c["recall_hit"] is False]
        for miss in misses:
            got = ", ".join(t["skill"] for t in miss["top"]) or "(nothing)"
            print(f"  miss {miss['id']}: wanted one of case expects, got [{got}]")
        if eval_report["recall_at_k"] < RECALL_THRESHOLD:
            print(f"FAIL: recall@{TOP_K} below threshold {RECALL_THRESHOLD:.0%}")
            failed = True
        if eval_report["unexpected_high_cost_candidates"]:
            failed = True

    lint_findings = run_lint(skills)
    report["lint"] = lint_findings
    print(f"lint findings: {len(lint_findings)}")
    for finding in lint_findings:
        print(f"  {finding['name']} [{finding['policy']}]: {', '.join(finding['issues'])}")
    if any("L1_missing_description" in f["issues"] for f in lint_findings):
        failed = True

    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"json report: {args.json}")

    return 1 if (failed and args.check) else 0


if __name__ == "__main__":
    sys.exit(main())
