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
DEFAULT_HINTS = ROOT / "routing-evals" / "hints.yaml"

TOP_K = 3
# Ratchet, not aspiration. History: 0.40 (bare baseline 2026-07-06 AM),
# 0.90 (hints overlay landed 2026-07-06 PM, measured 100%; 0.90 leaves
# fleet-churn slack of ~2 misses). Never lower without a written reason.
RECALL_THRESHOLD = 0.90

# Displayed recall = expected skill actually SHOWN by the production display
# rule (fire threshold + companion + top-k), not just present in the top-k
# ranking. Measured 0.94 at threshold 4.0 on 2026-07-06 (one weak case scores
# below the fire bar). Gate guards against future display regressions.
DISPLAYED_RECALL_THRESHOLD = 0.90

# Router firing threshold — single source shared with skill_router_hook.py.
# Recalibrated 2026-07-11 after the CJK-fragment fix (tokenize no longer
# emits single CJK chars or 1-2 digit numbers; stop-bigram list extended
# with generic process words). Score distribution shifted down wholesale:
# true hits now 1.48+, hardest surviving negatives <= 0.96 (guarded
# agent-brief cases are intercepted by should_skip_prompt BEFORE scoring
# and do not constrain this threshold; non-high-cost sightings on
# out-of-contract prompts, e.g. investigate at 1.56 on 'plan approved
# yesterday', are tolerated by design). Hard negatives: silent cases
# <= 0.96, suggest-confirm sightings <= 1.13. 1.35 sits mid-margin
# between those and the weakest true hit (1.55).
# Previous calibration (2026-07-06, fragment-era scores): true hits
# 4.03-16.83, negatives <= 2.54, threshold 4.0.
# The eval counts an unexpected high-cost candidate as a violation
# only when production would actually SHOW it (see chosen_candidates);
# other sightings still appear in gate_dependency_events for visibility.
FIRE_THRESHOLD = 1.35

# Companion bar: runners-up are shown only when they clear this fraction of
# the top score. Calibration 2026-07-06: suppressed every noisy runner-up
# while keeping legitimate co-candidates.
COMPANION_RATIO = 0.6

AGENT_TO_AGENT_PATTERN_WINDOW = 240
AGENT_TO_AGENT_PATTERNS = (
    "<task-notification>",
    "你是 Claude",
    "你是Claude",
    "你是 Codex",
    "你是Codex",
    "你是 ChatGPT",
    "你是ChatGPT",
    "你是 Gemini",
    "你是Gemini",
    "你是 Fable",
    "你是Fable",
    "作为独立外部审核者",
    "You are Claude",
    "You are Codex",
    "You are ChatGPT",
    "You are Gemini",
    "You are an independent",
    "You are an external reviewer",
    "You are the final reviewer",
)

# Seat-label role briefs (e.g. "你是本项目的 Claude 动态工作流调度/判断席",
# "你是 Fable5 反方终审席") defeat the "你是 Claude" substring because words sit
# between "你是" and the model name. Anchor on the role ASSIGNMENT — "你是"
# followed within a short span by a seat label — so genuine prompts that merely
# mention a seat ("帮我设计一个判断席评分面板", "解释一下判断席/落地席/终审席")
# are NOT skipped. The (?!不是) lookahead excludes the colloquial opener "你是不是…".
AGENT_SEAT_BRIEF_RE = re.compile(
    r"你是(?!不是)[^。！？\n]{0,40}(判断席|终审席|复核席|落地席|调度席|仲裁席)"
)


def chosen_candidates(
    ranked: list[tuple[str, float]],
    fire_threshold: float | None = None,
    companion_ratio: float | None = None,
    top_k: int | None = None,
) -> list[tuple[str, float]]:
    """Production display rule, shared by hook and eval (single source).

    Fires only when the top candidate clears fire_threshold; then shows at
    most top_k entries that also clear companion_ratio * top score. The
    truncation lives HERE so a caller passing a full ranking cannot make
    hook and eval drift.
    """
    fire = FIRE_THRESHOLD if fire_threshold is None else fire_threshold
    ratio = COMPANION_RATIO if companion_ratio is None else companion_ratio
    k = TOP_K if top_k is None else top_k
    ranked = ranked[:k]
    if not ranked or ranked[0][1] < fire:
        return []
    bar = ranked[0][1] * ratio
    return [(n, s) for n, s in ranked if s >= bar]


def should_skip_prompt(prompt: str) -> str:
    """Return a guard reason when a prompt should bypass routing entirely.

    Router hints are for user intent. Agent-to-agent review briefs and task
    notifications are already meta-prompts; scoring their dense workflow
    language caused measured false positives such as `huashu-design`.
    """
    # Agent review/task briefs put the role instruction first; the window keeps
    # this guard from swallowing ordinary user prompts that mention model names.
    stripped = prompt.lstrip()
    window = stripped[:AGENT_TO_AGENT_PATTERN_WINDOW]
    for pattern in AGENT_TO_AGENT_PATTERNS:
        if pattern in window:
            return "agent_to_agent_prompt"
    if AGENT_SEAT_BRIEF_RE.search(window):
        return "agent_to_agent_prompt"
    return ""

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

# Chinese function-word bigrams: near-zero routing signal, high leak risk
# (measured 2026-07-06: "一下" dragged `retro` into three unrelated cases).
CJK_STOP_BIGRAMS = {
    "一下", "帮我", "这个", "一个", "什么", "怎么", "可以",
    "需要", "我们", "你的", "我的", "现在", "然后", "还是",
    # 2026-07-11 扩表（数据驱动，见 tokenize docstring 的实测案例）：
    # 通用流程/元工作词——出现在几乎所有执行类指令里，对"选哪个技能"
    # 零区分度，却让长中文 description 的技能（huashu-design 等）在
    # 纯执行指令上虚高得分。
    "根据", "执行", "任务", "不同", "建议", "按照", "最终",
    "审核", "模型", "分配", "难度", "继续", "具体", "直接",
}


def load_audit_module() -> Any:
    path = ROOT / "scripts" / "skill_audit.py"
    spec = importlib.util.spec_from_file_location("skill_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tokenize(text: str) -> list[str]:
    """CJK character bigrams + lowercased latin/digit words.

    Single CJK characters are deliberately NOT emitted (2026-07-11 fix):
    they carry no discriminative meaning on their own — like indexing
    single letters in English — yet in a mostly-English skill fleet they
    get inflated IDF (e.g. 给 scored 4.31 because only the one skill with
    a long Chinese description contained it). Measured effect: the prompt
    「按照你们的建议执行…最终审核」 scored huashu-design at 13.43 (3.4x the
    fire threshold) purely from 27 fragment hits. Real Chinese signal
    lives in the bigrams (封面/设计/海报), which are kept.
    """
    tokens: list[str] = []
    for word in re.findall(r"[a-zA-Z0-9_-]+", text):
        # 1-2 位纯数字同样是零区分度碎片（"codex 5.6" 的 5/6 曾以 IDF 4.31
        # 命中某技能描述里的 "5 维度评审"）。3 位以上保留（如报错码 500）。
        if word.isdigit() and len(word) < 3:
            continue
        tokens.append(word.lower())
    cjk_chars = CJK_RE.findall(text)
    tokens.extend(
        bigram
        for a, b in zip(cjk_chars, cjk_chars[1:])
        if (bigram := a + b) not in CJK_STOP_BIGRAMS
    )
    return tokens


def load_hints(path: Path) -> dict[str, dict[str, Any]]:
    """Load the routing-hints overlay. Empty dict when unavailable (degrade)."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, Exception):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in data.get("hints", []) or []:
        name = entry.get("skill")
        if not name:
            continue
        fresh = {
            "extra_triggers": entry.get("extra_triggers") or [],
            "negative_triggers": entry.get("negative_triggers") or [],
            "domains": entry.get("domains") or [],
        }
        if name in out:
            # Codex 终审 H3（2026-07-11）：此前同名 stanza 静默覆盖，一次追加式
            # 编辑清空了 huashu-design 等 4 个 skill 的既有 negative_triggers，
            # 且 eval 恰被 known_leaks 容忍而全绿。合并为并集，去重保序。
            merged = out[name]
            for key in ("extra_triggers", "negative_triggers", "domains"):
                seen = set(merged[key])
                merged[key] = merged[key] + [
                    v for v in fresh[key] if v not in seen
                ]
        else:
            out[name] = fresh
    return out


class LexicalIndex:
    """IDF-weighted token overlap between a prompt and skill name+description.

    `hints` merges the routing overlay: extra_triggers extend a skill's token
    set; negative_triggers exclude a skill when the raw prompt contains any of
    them; domains restrict a skill to prompts/cwd mentioning that domain.
    The router hook and the eval share this class — eval measures production.
    """

    def __init__(self, skills: list[dict[str, Any]], hints: dict[str, dict[str, Any]] | None = None):
        self.skills = skills
        self.hints = hints or {}
        self.doc_tokens: list[set[str]] = []
        df: dict[str, int] = {}
        for skill in skills:
            text = f"{skill['name']} {skill.get('description', '')}"
            extra = self.hints.get(skill["name"], {}).get("extra_triggers", [])
            if extra:
                text += " " + " ".join(extra)
            toks = set(tokenize(text))
            self.doc_tokens.append(toks)
            for t in toks:
                df[t] = df.get(t, 0) + 1
        n = max(len(skills), 1)
        self.idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}

    def _excluded(self, name: str, prompt: str, cwd: str) -> bool:
        hint = self.hints.get(name)
        if not hint:
            return False
        lowered = prompt.lower()
        for neg in hint["negative_triggers"]:
            if neg.lower() in lowered:
                return True
        domains = hint["domains"]
        if domains:
            hay = f"{lowered} {cwd.lower()}"
            if not any(d.lower() in hay for d in domains):
                return True
        return False

    def rank(self, prompt: str, top_k: int = TOP_K, cwd: str = "") -> list[tuple[str, float]]:
        q = set(tokenize(prompt))
        scored: list[tuple[str, float]] = []
        for skill, toks in zip(self.skills, self.doc_tokens):
            overlap = q & toks
            if not overlap:
                continue
            if self._excluded(skill["name"], prompt, cwd):
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
    skills: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    hints: dict[str, dict[str, Any]] | None = None,
    fire_threshold: float | None = None,
) -> dict[str, Any]:
    index = LexicalIndex(skills, hints=hints)
    policy = {s["name"]: s["policy"] for s in skills}
    installed = set(policy)

    results: list[dict[str, Any]] = []
    recall_hits = recall_total = 0
    displayed_hits = 0  # expected skill actually shown by the production display rule
    gate_events: list[dict[str, Any]] = []
    unexpected_high_cost: list[dict[str, Any]] = []
    known_leak_events: list[dict[str, Any]] = []
    false_positive_events: list[dict[str, Any]] = []
    negative_total = negative_silent_hits = guard_hits = 0

    for case in cases:
        expect = list(case.get("expect", []) or [])
        high_cost_ok = set(case.get("high_cost_ok", []) or [])
        known_leaks = set(case.get("known_leaks", []) or [])
        skip_reason = should_skip_prompt(case["prompt"])
        top = [] if skip_reason else index.rank(case["prompt"])
        top_names = [name for name, _ in top]
        scores = dict(top)
        shown = {n for n, _ in chosen_candidates(top, fire_threshold=fire_threshold)}
        if not expect and not high_cost_ok and not known_leaks:
            # Codex 终审 H1（2026-07-11）：被 should_skip_prompt 拦截的负例
            # 没经过评分器，混进 precision 分母会虚高"阈值挡住了负例"的证据。
            # 拆两个口径：guard_hit（守卫拦截数）与 negative precision（真经过
            # 评分器且保持沉默的比例）。
            if skip_reason:
                guard_hits += 1
            else:
                negative_total += 1
                if not shown:
                    negative_silent_hits += 1
                else:
                    false_positive_events.append(
                        {
                            "case": case["id"],
                            "shown": [
                                {"skill": n, "score": round(scores.get(n, 0.0), 2)}
                                for n in sorted(shown)
                            ],
                        }
                    )

        expect_installed = [e for e in expect if e in installed]
        skipped = [e for e in expect if e not in installed]
        hit = None
        displayed_hit = None
        if expect_installed:
            recall_total += 1
            hit = any(e in top_names for e in expect_installed)
            recall_hits += bool(hit)
            # displayed recall: would production actually SHOW the expected skill,
            # after the firing threshold + companion + top-k rule? (Codex finding)
            displayed_hit = any(e in shown for e in expect_installed)
            displayed_hits += bool(displayed_hit)
        case_high_cost = [n for n in top_names if policy.get(n) == "suggest-confirm"]
        for name in case_high_cost:
            event = {
                "case": case["id"],
                "skill": name,
                "rank": top_names.index(name) + 1,
                "score": round(scores.get(name, 0.0), 2),
            }
            gate_events.append(event)
            if name in high_cost_ok:
                continue
            if name in known_leaks:
                event = dict(event, known=True)
                known_leak_events.append(event)
            elif name in shown:
                # violation only if production would actually display it
                unexpected_high_cost.append(event)

        results.append(
            {
                "id": case["id"],
                "prompt": case["prompt"],
                "top": [{"skill": n, "score": round(s, 3)} for n, s in top],
                "recall_hit": hit,
                "displayed_hit": displayed_hit,
                "skipped_missing_skill": skipped,
                "high_cost_in_top": case_high_cost,
                "skip_reason": skip_reason,
            }
        )

    recall = recall_hits / recall_total if recall_total else 1.0
    displayed_recall = displayed_hits / recall_total if recall_total else 1.0
    negative_precision = negative_silent_hits / negative_total if negative_total else 1.0

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
        "displayed_recall": round(displayed_recall, 3),
        "displayed_hits": displayed_hits,
        "negative_precision": round(negative_precision, 3),
        "negative_silent_hits": negative_silent_hits,
        "negative_total": negative_total,
        "guard_hits": guard_hits,
        "descriptions_without_cjk": no_cjk,
        "gate_dependency_events": gate_events,
        "known_leaks": known_leak_events,
        "unexpected_high_cost_candidates": unexpected_high_cost,
        "false_positive_candidates": false_positive_events,
        "cases": results,
    }


def model_route_policy(case: dict[str, Any]) -> dict[str, Any]:
    """Return the documented seat/effort/gate policy for a task shape.

    This is deliberately deterministic and offline. It is an eval oracle for
    scale policy, not a runtime router and not a provider/model dispatcher.
    """
    shape = str(case.get("task_shape") or "").strip().lower()
    risk = str(case.get("risk_zone") or "default").strip().lower()
    repo_profile = str(case.get("repo_profile") or "default").strip().lower()
    mechanical = bool(case.get("mechanical"))

    restricted = risk in {"restricted", "irreversible"}
    restricted_repo = repo_profile == "restricted-zone-heavy"
    small_mechanical = shape == "small_fix" and mechanical and risk in {"low", "default"}

    if shape == "small_fix" and not restricted and (not restricted_repo or small_mechanical):
        return {
            "direction_seat": "codex",
            "landing_seat": "codex",
            "final_review_seat": "none",
            "effort": "medium-fast",
            "gates": ["focused_verification"],
            "hot_path": False,
        }

    if shape == "release_ship" or risk == "irreversible":
        return {
            "direction_seat": "gate_owner",
            "landing_seat": "release_owner",
            "final_review_seat": "codex",
            "effort": "xhigh",
            "gates": ["intent", "green_checks", "final_diff_review", "ship_gate"],
            "hot_path": False,
        }

    if restricted or restricted_repo:
        return {
            "direction_seat": "claude",
            "landing_seat": "implementation_owner",
            "final_review_seat": "codex",
            "effort": "xhigh",
            "gates": ["intent", "plan_gate", "blind_plan_review", "final_diff_review"],
            "hot_path": False,
        }

    if shape == "code_review":
        return {
            "direction_seat": "reviewer",
            "landing_seat": "none",
            "final_review_seat": "none",
            "effort": "high",
            "gates": ["intent"],
            "hot_path": False,
        }

    if shape == "bug":
        return {
            "direction_seat": "codex",
            "landing_seat": "codex",
            "final_review_seat": "codex",
            "effort": "high",
            "gates": ["reproduce", "root_cause", "regression"],
            "hot_path": False,
        }

    if shape == "broad_refactor":
        return {
            "direction_seat": "claude",
            "landing_seat": "implementation_owner",
            "final_review_seat": "codex",
            "effort": "high",
            "gates": ["intent", "plan_gate", "final_diff_review"],
            "hot_path": False,
        }

    if shape == "feature":
        return {
            "direction_seat": "claude",
            "landing_seat": "implementation_owner",
            "final_review_seat": "codex",
            "effort": "high",
            "gates": ["intent", "final_diff_review"],
            "hot_path": False,
        }

    return {
        "direction_seat": "codex",
        "landing_seat": "codex",
        "final_review_seat": "codex",
        "effort": "high",
        "gates": ["intent", "focused_verification"],
        "hot_path": False,
    }


def run_model_routing_eval(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for case in cases:
        actual = model_route_policy(case)
        expected = case.get("expect_policy") or {}
        mismatches: dict[str, dict[str, Any]] = {}
        for key, expected_value in expected.items():
            actual_value = actual.get(key)
            if actual_value != expected_value:
                mismatches[key] = {
                    "expected": expected_value,
                    "actual": actual_value,
                }
        result = {
            "id": case.get("id", ""),
            "task_shape": case.get("task_shape", ""),
            "risk_zone": case.get("risk_zone", ""),
            "repo_profile": case.get("repo_profile", ""),
            "actual": actual,
            "expected": expected,
            "mismatches": mismatches,
        }
        results.append(result)
        if mismatches:
            failures.append(result)

    total = len(cases)
    hits = total - len(failures)
    return {
        "metric": "model-routing-policy-v1",
        "total": total,
        "hits": hits,
        "pass_rate": round(hits / total, 3) if total else 1.0,
        "failures": failures,
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


def run_doctor() -> int:
    """skill-doctor v0: summarize router-log emissions into candidate drafts.

    Output is a YAML fragment for the `candidates:` zone of cases.yaml —
    always human-reviewed, never merged automatically.
    """
    log_path = Path.home() / ".codex" / "skill-governance" / "routing-log.jsonl"
    if not log_path.is_file():
        print("# no routing log yet:", log_path)
        return 0
    fired: dict[str, int] = {}
    high_cost: dict[str, int] = {}
    total = 0
    for line in log_path.read_text(errors="ignore").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        total += 1
        for cand in rec.get("candidates", []):
            fired[cand["skill"]] = fired.get(cand["skill"], 0) + 1
            if cand.get("policy") == "suggest-confirm":
                high_cost[cand["skill"]] = high_cost.get(cand["skill"], 0) + 1
    print(f"# routing-log: {total} emissions from {log_path}")
    print("# review each stanza, then promote manually into cases.yaml candidates:")
    for name, count in sorted(high_cost.items(), key=lambda x: -x[1]):
        print(f"""
  - id: cand-doctor-{name}
    observed: "(fill date)"
    source: "router-log, {count}/{total} emissions"
    prompt: "(paste a representative prompt from the log)"
    failure: >-
      suggest-confirm skill {name} surfaced {count} times; review whether
      these were legitimate candidacies or hint/description over-reach.""")
    top = sorted(fired.items(), key=lambda x: -x[1])[:10]
    print("\n# top surfaced skills (frequency):", ", ".join(f"{n}×{c}" for n, c in top))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--hints", type=Path, default=DEFAULT_HINTS)
    parser.add_argument("--no-hints", action="store_true", help="score without the overlay")
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--lint", action="store_true", help="lint only, skip eval")
    parser.add_argument("--doctor", action="store_true",
                        help="summarize the router log into candidate case drafts")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--check", action="store_true", help="exit 1 on failures")
    parser.add_argument("--fire-threshold", type=float, default=None,
                        help="validate recall at this firing threshold (for self-tune)")
    args = parser.parse_args()

    if args.doctor:
        return run_doctor()

    audit = load_audit_module()
    skills = collect_skills(audit, args.skills_dir)
    hints = {} if args.no_hints else load_hints(args.hints)

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
        case_data = parse_cases(args.cases)
        cases = case_data.get("cases", [])
        eval_report = run_eval(skills, cases, hints=hints, fire_threshold=args.fire_threshold)
        eval_report["hints_loaded"] = len(hints)
        report["eval"] = eval_report
        print(f"skills evaluated: {eval_report['skills_evaluated']}")
        print(
            f"recall@{TOP_K} (rank): {eval_report['recall_at_k']:.0%}"
            f" ({eval_report['recall_hits']}/{eval_report['recall_total']})"
        )
        print(
            f"displayed recall (would fire): {eval_report['displayed_recall']:.0%}"
            f" ({eval_report['displayed_hits']}/{eval_report['recall_total']})"
        )
        print(
            f"negative precision (should stay silent, scored only): "
            f"{eval_report['negative_precision']:.0%} "
            f"({eval_report['negative_silent_hits']}/{eval_report['negative_total']}; "
            f"guard-intercepted: {eval_report['guard_hits']})"
            f" ({eval_report['negative_silent_hits']}/{eval_report['negative_total']})"
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
        print(f"false positive candidates: {len(eval_report['false_positive_candidates'])}")
        for event in eval_report["false_positive_candidates"]:
            got = ", ".join(c["skill"] for c in event["shown"])
            print(f"  !! {event['case']}: showed [{got}]")
        misses = [c for c in eval_report["cases"] if c["recall_hit"] is False]
        for miss in misses:
            got = ", ".join(t["skill"] for t in miss["top"]) or "(nothing)"
            print(f"  miss {miss['id']}: wanted one of case expects, got [{got}]")
        if eval_report["recall_at_k"] < RECALL_THRESHOLD:
            print(f"FAIL: recall@{TOP_K} below threshold {RECALL_THRESHOLD:.0%}")
            failed = True
        if eval_report["displayed_recall"] < DISPLAYED_RECALL_THRESHOLD:
            print(f"FAIL: displayed recall below threshold {DISPLAYED_RECALL_THRESHOLD:.0%}"
                  " — a threshold/hint change hid an expected skill from firing")
            failed = True
        if eval_report["unexpected_high_cost_candidates"]:
            failed = True
        if eval_report["false_positive_candidates"]:
            failed = True

        model_cases = case_data.get("model_routing_cases", []) or []
        model_report = run_model_routing_eval(model_cases)
        report["model_routing_eval"] = model_report
        print(
            f"model routing policy: {model_report['pass_rate']:.0%}"
            f" ({model_report['hits']}/{model_report['total']})"
        )
        for failure in model_report["failures"]:
            got = ", ".join(
                f"{k}: expected {v['expected']} got {v['actual']}"
                for k, v in failure["mismatches"].items()
            )
            print(f"  !! model {failure['id']}: {got}")
        if model_report["failures"]:
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
