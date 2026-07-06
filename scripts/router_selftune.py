#!/usr/bin/env python3
"""Router self-tune — scheduled watchdog: detect, surface evidence, remind.

Runs weekly (see docs/routing-evals.md). It does NOT mutate routing and does
NOT auto-draft suppression patterns: choosing a negative_trigger is judgment
(a careless pattern hides real matches), so it stays human-gated — the lesson
this whole layer is built on. What it automates is everything UP TO the human
edit:

  1. detect  — recall/displayed-recall regressions + attractor skills (firing
               across many unrelated prompts: the over-firing you notice)
  2. surface — for each attractor, the real sampled noise prompts so you can
               see the pattern to suppress
  3. remind  — dated report + macOS notification

Applying a fix is your call: read the samples, add a negative_trigger to
routing-evals/hints.yaml, and `routing_eval.py --check` must stay green
(it now also guards displayed recall, so a suppression that hides a real
match fails the gate). Threshold is deliberately NOT tuned here: measured
noise/signal overlap is negative, so no single threshold both quiets noise
and keeps weak legitimate hits firing — only per-skill hints do.

Usage:
  python3 scripts/router_selftune.py           # analyze + report
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOV_DIR = Path.home() / ".codex" / "skill-governance"
LOG_PATH = GOV_DIR / "routing-log.jsonl"

MIN_FIRES_FOR_CONFIDENCE = 25     # attractor analysis below this is low-confidence
ATTRACTOR_DISTINCT_PROMPTS = 5    # fires in >= this many distinct prompts = suspect


def load_routing():
    spec = importlib.util.spec_from_file_location(
        "routing_eval", ROOT / "scripts" / "routing_eval.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def recall_is_green() -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "routing_eval.py"), "--check"],
        capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if "recall@" in l), "recall unknown")
    return r.returncode == 0, line.strip()


def analyze_log(routing) -> dict:
    if not LOG_PATH.is_file():
        return {"emissions": 0, "fires": 0, "attractors": [], "thin": True}
    audit = routing.load_audit_module()
    policy = {s["name"]: s["policy"] for s in routing.collect_skills(audit, None)}
    emissions = fires = 0
    per_skill: dict[str, set] = {}
    heads: dict[str, list] = {}
    for line in LOG_PATH.read_text(errors="ignore").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        emissions += 1
        if not rec.get("fired"):
            continue
        fires += 1
        for cand in rec.get("candidates", []):
            per_skill.setdefault(cand["skill"], set()).add(rec.get("prompt_sha"))
            heads.setdefault(cand["skill"], [])
            if rec.get("prompt_head") and len(heads[cand["skill"]]) < 3:
                heads[cand["skill"]].append(rec["prompt_head"])
    attractors = [
        {"skill": s, "distinct_prompts": len(p), "policy": policy.get(s, "?"),
         "samples": heads.get(s, [])}
        for s, p in per_skill.items() if len(p) >= ATTRACTOR_DISTINCT_PROMPTS
    ]
    attractors.sort(key=lambda x: -x["distinct_prompts"])
    return {"emissions": emissions, "fires": fires,
            "attractors": attractors, "thin": fires < MIN_FIRES_FOR_CONFIDENCE}


def main() -> int:
    routing = load_routing()
    today = dt.date.today().isoformat()
    green, recall_line = recall_is_green()
    log = analyze_log(routing)

    L = [f"# Router self-tune report — {today}", "",
         "## Health",
         f"- recall gate: {'GREEN' if green else 'RED — REGRESSION, investigate'} ({recall_line})",
         f"- log: {log['emissions']} emissions, {log['fires']} fired", ""]

    L += ["## Attractors (over-firing on unrelated prompts)"]
    if log["thin"]:
        L.append(f"- low-confidence: {log['fires']}/{MIN_FIRES_FOR_CONFIDENCE} fires so far; "
                 "treat below as tentative, keep accumulating.")
    if not log["attractors"]:
        L.append("- none above threshold. Router is behaving.")
    for a in log["attractors"]:
        flag = " ⚠ HIGH-COST" if a["policy"] == "suggest-confirm" else ""
        L.append(f"\n### `{a['skill']}` — fired across {a['distinct_prompts']} "
                 f"distinct prompts{flag}")
        L.append("  sampled noise prompts (find the shared pattern to suppress):")
        for h in a["samples"]:
            L.append(f"  - \"{h}\"")
        L.append(f"  → you decide a `negative_triggers` pattern for `{a['skill']}` "
                 "from what you see above.")

    L += ["", "## To apply a fix (your judgment, one edit)",
          "1. Read the sampled prompts; pick a `negative_triggers` substring that "
          "hits the noise but NOT real requests for that skill.",
          "2. Add it under that skill in `routing-evals/hints.yaml`.",
          "3. `python3 scripts/routing_eval.py --check` must stay green — it now "
          "also fails if your suppression hides an expected skill from firing.",
          "4. commit + push.",
          "",
          "The script does not auto-write the pattern: choosing it wrong silently "
          "hides real matches, so it is human-gated by design. Threshold is not "
          "tuned here either (noise/signal overlap makes it the wrong lever)."]

    report = "\n".join(L) + "\n"
    out = GOV_DIR / f"selftune-{today}.md"
    try:
        GOV_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(report)
    except OSError:
        pass

    n_att = len(log["attractors"])
    summary = (f"recall {'GREEN' if green else 'RED!'}; "
               f"{n_att} attractor proposal(s); {log['fires']} fires logged")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{summary}" with title "Skill router self-tune"'],
            capture_output=True, timeout=10)
    except Exception:
        pass

    print(report)
    print(f"[report saved: {out}]")
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main())
