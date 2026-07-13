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
live 111-skill fleet + hints (recalibrated 2026-07-11 post CJK-fragment
fix): true hits >= 1.48, hardest scored negatives <= 0.96, threshold 1.35
(see routing_eval.py FIRE_THRESHOLD for the full calibration note). Companion
candidates must clear COMPANION_RATIO * top score (0.6), which in
calibration suppressed all noisy runners-up. Recalibrate when eval recall
moves; never tune by feel.

Log (M3 + M-privacy): one JSON line per emission to
~/.codex/skill-governance/routing-log.jsonl. Privacy-minimized by default —
prompt stored as sha256 prefix + length only (NO plaintext); cwd stored as
basename only; when available the record also carries a session pointer
(session_id / transcript_path from the hook stdin) so an operator can trace
a hash back to a transcript without the router persisting the text itself.
Plaintext excerpt (prompt_head) is written ONLY when the environment variable
SKILL_ROUTER_DEBUG_PLAINTEXT=1 is set, and every such record carries a
ttl_expires (write time + 7 days). On startup the hook opportunistically
strips plaintext from any expired debug records (in-place rewrite, best-effort
— any failure is swallowed and never affects the main flow). Old log lines
that already contain prompt_head are NOT migrated and never crash the cleanup.
O_APPEND single-line writes; 5 MB rotate to .1; all log failures silent.

Index cache (M4): parsed skill entries cached to
~/.codex/skill-governance/router-index.json keyed by the sum+count of all
SKILL.md mtimes; any drift rebuilds. Suggestion language is advisory by
design (blind-review T3): the three vercel-hook false-trigger incidents of
2026-07-06 came from MANDATORY wording on lexical matches.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

# Firing + companion display rule AND top-K truncation live in
# routing_eval.chosen_candidates (single source; calibration table there).
LOG_MAX_BYTES = 5 * 1024 * 1024

# Plaintext excerpts are opt-in only, and self-expire after this window so a
# forgotten debug flag cannot leave prompt text on disk indefinitely.
DEBUG_PLAINTEXT_ENV = "SKILL_ROUTER_DEBUG_PLAINTEXT"
DEBUG_PLAINTEXT_TTL_DAYS = 7

ROOT = Path(__file__).resolve().parents[1]
GOV_DIR = Path.home() / ".codex" / "skill-governance"
CACHE_PATH = GOV_DIR / "router-index.json"
LOG_PATH = GOV_DIR / "routing-log.jsonl"

# Hot-route shrink (2026-07-13 三席评估): keep the auto-suggest surface small.
# Content-creation skills are the measured attractors (huashu-design fired on
# 100+ unrelated prompts) with ~zero real invocation via the router — they are
# reached through the CLAUDE.md design decision table + design-trigger hook, or
# explicitly, NOT lexically. Excluding them from lexical auto-suggestion can
# only REMOVE noise (never adds a suggestion) and does not touch the decision
# table. Overridable per host via GOV_DIR/hot-route-exclude.json (a JSON list);
# an empty list restores the pre-shrink behavior.
DEFAULT_HOT_ROUTE_EXCLUDE = {
    "huashu-design", "social-monitor", "huashu-data-pro", "huashu-research",
    "huashu-article-edit",
}


def load_hot_route_exclude() -> set[str]:
    try:
        v = json.loads((GOV_DIR / "hot-route-exclude.json").read_text())
        if isinstance(v, list):
            return {str(x) for x in v}
    except (OSError, ValueError):
        pass
    return set(DEFAULT_HOT_ROUTE_EXCLUDE)


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


def purge_expired_plaintext() -> None:
    """Strip plaintext from expired debug records; rewrite the log in place.

    Opportunistic and best-effort: bounded by the 5 MB rotation cap and wrapped
    so ANY failure is swallowed — the hot path must never be affected. Cheap
    early-out when no record carries a ttl_expires (the default, plaintext-off
    case). Only expired debug records are touched: their prompt_head/ttl_expires
    keys are dropped, leaving the hash+len skeleton intact. Old-format lines
    (prompt_head without ttl_expires) and any non-JSON line are preserved
    verbatim, so the cleanup can never be crashed by legacy content.
    """
    try:
        try:
            raw = LOG_PATH.read_text(encoding="utf-8")
        except OSError:
            return
        if "ttl_expires" not in raw:
            return  # nothing to expire; skip all per-line parsing + any write
        now = datetime.datetime.now()
        changed = False
        out_lines: list[str] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            keep = line
            try:
                rec = json.loads(line)
            except ValueError:
                out_lines.append(keep)  # legacy / malformed: never choke
                continue
            if isinstance(rec, dict):
                exp = rec.get("ttl_expires")
                if exp:
                    try:
                        expired = datetime.datetime.fromisoformat(str(exp)) < now
                    except (ValueError, TypeError):
                        expired = False
                    if expired:
                        rec.pop("prompt_head", None)
                        rec.pop("ttl_expires", None)
                        keep = json.dumps(rec, ensure_ascii=False)
                        changed = True
            out_lines.append(keep)
        # Known race: concurrent sessions append via O_APPEND while we
        # read->rewrite; lines appended in that window are lost. Accepted:
        # only reachable when expired debug plaintext exists, loss is log-only.
        if not changed:
            return
        text = ("\n".join(out_lines) + "\n") if out_lines else ""
        tmp_path = LOG_PATH.with_suffix(".jsonl.tmp")
        try:
            tmp_path.write_text(text, encoding="utf-8")
            os.chmod(tmp_path, 0o600)  # keep write_log's permission; umask would widen to 0644
            os.replace(tmp_path, LOG_PATH)
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    except Exception:
        pass  # cleanup is strictly best-effort; never surface to the hot path


def write_log(
    prompt: str,
    cwd: str,
    candidates: list[dict],
    fired: bool,
    skip_reason: str = "",
    session_id: str = "",
    transcript_path: str = "",
) -> None:
    try:
        GOV_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if LOG_PATH.is_file() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                LOG_PATH.replace(LOG_PATH.with_suffix(".jsonl.1"))
        except OSError:
            pass
        now = datetime.datetime.now()
        record = {
            "ts": now.isoformat(timespec="seconds"),
            "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:16],
            "prompt_len": len(prompt),
            "repo": os.path.basename(cwd) if cwd else "",
            "fired": fired,
            "skip_reason": skip_reason,
            "candidates": candidates,
        }
        # Session pointer: lets an operator trace a hash to a transcript without
        # the router itself persisting the prompt text. Only present when the
        # hook stdin actually carried these fields.
        if session_id:
            record["session_id"] = session_id
        if transcript_path:
            record["transcript_path"] = transcript_path
        # Plaintext excerpt is opt-in and self-expiring (see module docstring).
        if os.environ.get(DEBUG_PLAINTEXT_ENV) == "1":
            record["prompt_head"] = prompt[:80]
            record["ttl_expires"] = (
                now + datetime.timedelta(days=DEBUG_PLAINTEXT_TTL_DAYS)
            ).isoformat(timespec="seconds")
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
        # Session pointer (Claude Code UserPromptSubmit stdin carries these);
        # absent -> logged record falls back to hash+len only.
        session_id = data.get("session_id") or ""
        transcript_path = data.get("transcript_path") or ""
        if not prompt.strip():
            noop()
            return 0

        # Opportunistic, best-effort expiry of debug plaintext. Self-guards
        # against any failure so it can never degrade routing (unlike the outer
        # try, which would drop the suggestion to {}).
        purge_expired_plaintext()

        routing = load_routing_module()
        skip_reason = routing.should_skip_prompt(prompt)
        if skip_reason:
            write_log(prompt, cwd, [], fired=False, skip_reason=skip_reason,
                      session_id=session_id, transcript_path=transcript_path)
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
        # Hot-route shrink: drop excluded content-creation skills from the
        # auto-suggest surface (they stay reachable via decision table/explicit).
        exclude = load_hot_route_exclude()
        if exclude:
            chosen = [(n, s) for n, s in chosen if n not in exclude]
        if not chosen:
            write_log(prompt, cwd, [
                {"skill": n, "score": round(s, 2)} for n, s in top[:1]
            ], fired=False, session_id=session_id, transcript_path=transcript_path)
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
        write_log(prompt, cwd, candidates, fired=True,
                  session_id=session_id, transcript_path=transcript_path)
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
