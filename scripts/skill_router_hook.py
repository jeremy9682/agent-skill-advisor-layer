#!/usr/bin/env python3
"""UserPromptSubmit router hook — suggest (never mandate) relevant skills.

Reads {prompt, cwd} JSON on stdin. Scores the prompt against locally
installed skills using the SAME LexicalIndex + hints overlay that
scripts/routing_eval.py measures, so eval results describe production
behavior. Emits additionalContext naming up to 3 candidate skills with
their call-policy tier, or {} when nothing clears the threshold.

Failure policy (blind-review M1): ANY internal error degrades to `{}` and
exit 0. This hook must never block or delay a user prompt materially.

Threshold (M4): FIRE_THRESHOLD = 4.0, calibrated 2026-07-06 against the
live 109-skill fleet + hints: true hits scored 4.03-17.85, hardest
negatives (rename/GIL/weather/goodbye prompts) scored <= 3.66. Companion
candidates must clear COMPANION_RATIO * top score (0.6), which in
calibration suppressed all noisy runners-up. Recalibrate when eval recall
moves; never tune by feel.

Log (M3): one JSON line per emission to
~/.codex/skill-governance/routing-log.jsonl — prompt stored as sha256 +
first 80 chars only; cwd stored as basename only; O_APPEND single-line
writes; 5 MB rotate to .1; all log failures silent.

Index cache (M4): parsed skill entries cached to
~/.codex/skill-governance/router-index.json keyed by the sum+count of all
SKILL.md mtimes; any drift rebuilds. Suggestion language is advisory by
design (blind-review T3): the three vercel-hook false-trigger incidents of
2026-07-06 came from MANDATORY wording on lexical matches.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

# Firing + companion display rule AND top-K truncation live in
# routing_eval.chosen_candidates (single source; calibration table there).
LOG_MAX_BYTES = 5 * 1024 * 1024

ROOT = Path(__file__).resolve().parents[1]
GOV_DIR = Path.home() / ".codex" / "skill-governance"
CACHE_PATH = GOV_DIR / "router-index.json"
LOG_PATH = GOV_DIR / "routing-log.jsonl"


def noop() -> None:
    sys.stdout.write("{}")


def load_routing_module():
    path = ROOT / "scripts" / "routing_eval.py"
    spec = importlib.util.spec_from_file_location("routing_eval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fleet_fingerprint(audit) -> str:
    """Fingerprint from a fresh filesystem scan of the skill roots.

    Never derived from cached entries: a newly installed SKILL.md must
    change the fingerprint even though no cached path changed (final-review
    finding 1, 2026-07-06). Scans paths + count + mtimes; cheap (no parse).
    """
    paths: list[str] = []
    total = 0.0
    for root in getattr(audit, "SKILL_ROOTS", {}).values():
        try:
            for skill_md in Path(root).rglob("SKILL.md"):
                paths.append(str(skill_md))
                try:
                    total += skill_md.stat().st_mtime
                except OSError:
                    continue
        except OSError:
            continue
    digest = hashlib.sha256("\n".join(sorted(paths)).encode()).hexdigest()[:12]
    return f"{len(paths)}:{total:.0f}:{digest}"


def load_skills_cached(routing) -> list[dict]:
    """Return skill entries, using the scan-keyed JSON cache when fresh."""
    audit = routing.load_audit_module()
    current = fleet_fingerprint(audit)
    try:
        cached = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        cached = None
    if isinstance(cached, dict) and cached.get("fingerprint") == current:
        skills = cached.get("skills") or []
        if skills:
            return skills
    skills = routing.collect_skills(audit, None)
    try:
        GOV_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(
            {"fingerprint": current, "skills": skills},
            ensure_ascii=False))
    except OSError:
        pass
    return skills


def write_log(
    prompt: str,
    cwd: str,
    candidates: list[dict],
    fired: bool,
    skip_reason: str = "",
) -> None:
    try:
        GOV_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if LOG_PATH.is_file() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                LOG_PATH.replace(LOG_PATH.with_suffix(".jsonl.1"))
        except OSError:
            pass
        record = {
            "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:16],
            "prompt_head": prompt[:80],
            "repo": os.path.basename(cwd) if cwd else "",
            "fired": fired,
            "skip_reason": skip_reason,
            "candidates": candidates,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except Exception:
        pass  # M3: log failures are always silent


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw:
            noop()
            return 0
        data = json.loads(raw)
        prompt = data.get("prompt") or ""
        cwd = data.get("cwd") or data.get("workspace") or ""
        if not prompt.strip():
            noop()
            return 0

        routing = load_routing_module()
        skip_reason = routing.should_skip_prompt(prompt)
        if skip_reason:
            write_log(prompt, cwd, [], fired=False, skip_reason=skip_reason)
            noop()
            return 0
        skills = load_skills_cached(routing)
        hints = routing.load_hints(ROOT / "routing-evals" / "hints.yaml")
        index = routing.LexicalIndex(skills, hints=hints)
        top = index.rank(prompt, top_k=getattr(routing, "TOP_K", 3), cwd=cwd)

        # Optional machine-local threshold override (manual escape hatch to
        # quiet the router on one host without editing canonical code). Absent
        # by default -> canonical FIRE_THRESHOLD is used. Validate any value
        # with `routing_eval.py --check --fire-threshold X` before setting it;
        # displayed-recall must stay green.
        fire = None
        try:
            tune = json.loads((GOV_DIR / "router-tune.json").read_text())
            v = tune.get("fire_threshold")
            if isinstance(v, (int, float)):
                fire = float(v)
        except (OSError, ValueError):
            fire = None

        # Single-source display rule shared with the eval (finding 2).
        chosen = routing.chosen_candidates(top, fire_threshold=fire)
        if not chosen:
            write_log(prompt, cwd, [
                {"skill": n, "score": round(s, 2)} for n, s in top[:1]
            ], fired=False)
            noop()
            return 0

        policy = {s["name"]: s.get("policy", "") for s in skills}
        candidates = [
            {"skill": n, "score": round(s, 2), "policy": policy.get(n, "")}
            for n, s in chosen
        ]

        lines = []
        for n, s in chosen:
            tier = policy.get(n, "")
            note = "（suggest-confirm：需用户明确批准才能执行）" if tier == "suggest-confirm" else ""
            lines.append(f"- `{n}`{note}")
        context = (
            "[skill-router] 本条任务可能匹配以下已安装 skill（仅建议，不强制；"
            "判断不符直接忽略即可）：\n" + "\n".join(lines) +
            "\n如采用，先加载该 skill 的 SKILL.md 再动手。"
        )
        write_log(prompt, cwd, candidates, fired=True)
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }, ensure_ascii=False))
        return 0
    except Exception:
        noop()  # M1: never block the user's prompt
        return 0


if __name__ == "__main__":
    sys.exit(main())
