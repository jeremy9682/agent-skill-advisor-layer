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
STATUS_PATH = GOV_DIR / "selftune-status.jsonl"   # machine-readable weekly status

MIN_FIRES_FOR_CONFIDENCE = 25     # attractor analysis below this is low-confidence
ATTRACTOR_DISTINCT_PROMPTS = 5    # fires in >= this many distinct prompts = suspect
ATTRACTOR_WINDOW_DAYS = 7         # stale router noise must not block a clean-week streak
ATTRACTOR_FALLBACK_LINES = 500    # use recent records when legacy logs have no timestamp
REVISIT_CLEAN_WEEKS = 4           # Tier-2 ④: N consecutive clean weeks → revisit Codex routing-hook port


def _iso_week(date_str: str) -> str:
    """ISO year-week bucket, e.g. '2026-W28', from an ISO date string."""
    y, w, _ = dt.date.fromisoformat(date_str).isocalendar()
    return f"{y:04d}-W{w:02d}"


def revisit_tracker(today: str, green: bool, attractor_count: int, thin: bool) -> dict:
    """Track the Tier-2 ④ revisit condition mechanically instead of by memory.

    One status record per **ISO week** (running twice in one week overwrites,
    it does not advance the streak — so N daily runs cannot fake N weeks). The
    streak counts consecutive clean weeks that are also **calendar-adjacent**:
    a gap week (the watchdog didn't run, or ran not-clean) breaks it. A clean
    week = recall GREEN, zero attractors, non-thin data.

    Fail-closed: a corrupt status line or a failed persist makes this run
    report ``met=false`` with an ``error`` — a governance signal must not claim
    the revisit condition is satisfied off data it could not read or write.
    """
    week = _iso_week(today)
    clean = bool(green) and attractor_count == 0 and not thin
    records: list[dict] = []
    error: str | None = None
    if STATUS_PATH.exists():
        try:
            existing = STATUS_PATH.read_text().splitlines()
        except OSError:
            # file exists but is unreadable (permissions, etc.): fail-closed —
            # don't crash the whole weekly report, but don't trust the streak.
            existing = []
            error = "status file unreadable — cannot trust streak"
        for line in existing:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                error = "corrupt status line — cannot trust streak"
                continue
            if not isinstance(rec, dict):    # valid JSON but not a record object
                error = "non-object status line — cannot trust streak"
                continue
            if rec.get("week") != week:      # dedupe by ISO week (today's is rewritten)
                records.append(rec)
    records.append({"week": week, "date": today, "clean": clean, "green": bool(green),
                    "attractors": attractor_count, "thin": thin})
    records.sort(key=lambda r: str(r.get("week", "")))  # str-coerce: a manually-corrupted non-str week must not crash the sort
    try:
        GOV_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    except OSError:
        error = "failed to persist status — cannot trust streak"

    # Streak: walk back from the most recent week, requiring clean AND that each
    # step is exactly the previous ISO week (no missing weeks in between).
    streak = 0
    prev_week: str | None = None
    for rec in reversed(records):
        rw = rec.get("week", "")
        if prev_week is not None and rw != _prev_iso_week(prev_week):
            break  # calendar gap — streak is broken here
        if rec.get("clean"):
            streak += 1
            prev_week = rw
        else:
            break
    met = streak >= REVISIT_CLEAN_WEEKS and error is None
    return {"clean": clean, "streak": streak, "met": met, "need": REVISIT_CLEAN_WEEKS,
            "weeks_recorded": len(records), "error": error}


def _prev_iso_week(week: str) -> str:
    """The ISO week immediately before ``week`` ('2026-W28' → '2026-W27')."""
    y, w = week.split("-W")
    monday = dt.date.fromisocalendar(int(y), int(w), 1) - dt.timedelta(days=7)
    yy, ww, _ = monday.isocalendar()
    return f"{yy:04d}-W{ww:02d}"


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


def _record_time(rec: dict) -> dt.datetime | None:
    """Normalize the router log's timestamp fields to UTC.

    ``write_log`` stamps ``ts`` with a *naive local* ``datetime.now()`` (no
    tzinfo), so a naive value must be read as local wall-clock and converted —
    treating it as UTC (the earlier behavior) skewed the window by the local
    UTC offset. Epoch numbers and explicitly-offset strings are already
    absolute and only need converting.
    """
    value = next((rec[key] for key in ("ts", "t", "timestamp") if rec.get(key) is not None), None)
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        if isinstance(value, str):
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            # astimezone() reads a naive datetime as local time, then converts;
            # an already-aware value just gets normalized to UTC.
            return parsed.astimezone(dt.timezone.utc)
    except (OverflowError, ValueError):
        pass
    return None


def _adoption(audit, fires: int) -> dict:
    """Same-window adoption context: transcript-backed skill invocations vs the
    number of prompts the router fired on, both over ATTRACTOR_WINDOW_DAYS.

    IMPORTANT — this is a *non-causal period ratio*, NOT a conversion rate. The
    invocation count is every skill used in the window, including ones reached
    via the CLAUDE.md decision table or explicit calls that the router never
    suggested, and it is not linked per-prompt/session to any fire. It can
    therefore exceed 1.0 and must never be read as "fraction of fires that
    converted"; a true fire→invoke rate needs session-level attribution (future
    work). ``available`` is False when usage evidence could not be gathered —
    kept distinct from a genuine zero so the report never prints a bare "0".
    """
    unavailable = {"available": False, "fires": fires, "invocations": None, "ratio": None}
    health: dict = {}
    try:
        usage = audit.estimate_usage(
            audit.discover_skills(), ATTRACTOR_WINDOW_DAYS, 400, 3_000_000, health=health)
        invocations = sum(counts.get("actual_skill_invocation", 0) for counts in usage.values())
    except Exception:  # usage evidence must never make the watchdog unavailable
        return unavailable
    # estimate_usage swallows per-file scan errors, so a bare 0 is ambiguous
    # between "no usage" and "every scan failed". The health signal disambiguates:
    # a 0 is only an observed zero when at least one transcript file was actually
    # scanned. files_scanned == 0 (no recent files, OR every scan threw) means the
    # zero is a scan gap — report unavailable rather than assert an observed zero.
    if invocations == 0 and health.get("files_scanned", 0) == 0:
        return unavailable
    ratio = invocations / fires if fires else None
    return {"available": True, "fires": fires, "invocations": invocations, "ratio": ratio}


def _window_label(window: dict) -> str:
    if window.get("kind") == "last_lines":
        return f"last {window['lines']} log lines (no timestamps)"
    return f"last {window.get('days', ATTRACTOR_WINDOW_DAYS)} days"


def _adoption_label(adoption: dict) -> str:
    """Render the same-window adoption line. Never prints a bare '0' when usage
    evidence was simply unavailable (that is not an observed zero)."""
    fires = adoption["fires"]
    if not adoption.get("available", True):
        return f"usage evidence unavailable ({fires} router-fires this window)"
    invocations = adoption.get("invocations")
    ratio = adoption.get("ratio")
    if not fires:
        return f"{invocations} skill-invocations, 0 router-fires this window"
    return (f"{invocations} skill-invocations vs {fires} router-fires this window "
            f"— non-causal ratio {ratio:.2f} (not per-prompt attributed)")


def analyze_log(routing) -> dict:
    if not LOG_PATH.is_file():
        return {"emissions": 0, "fires": 0, "attractors": [], "thin": True,
                "window": {"kind": "last_days", "days": ATTRACTOR_WINDOW_DAYS},
                # No routing log yet: usage was never scanned, so report
                # unavailable rather than assert an observed "0 skill-invocations".
                "adoption": {"available": False, "fires": 0, "invocations": None, "ratio": None}}
    audit = routing.load_audit_module()
    policy = {s["name"]: s["policy"] for s in routing.collect_skills(audit, None)}
    lines = LOG_PATH.read_text(errors="ignore").splitlines()
    records: list[dict] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)

    dated = [(rec, _record_time(rec)) for rec in records]
    if any(timestamp is not None for _, timestamp in dated):
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ATTRACTOR_WINDOW_DAYS)
        records = [rec for rec, timestamp in dated if timestamp is not None and timestamp >= cutoff]
        window = {"kind": "last_days", "days": ATTRACTOR_WINDOW_DAYS}
    else:
        records = []
        for line in lines[-ATTRACTOR_FALLBACK_LINES:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
        window = {"kind": "last_lines", "lines": ATTRACTOR_FALLBACK_LINES}

    emissions = fires = 0
    per_skill: dict[str, set] = {}
    heads: dict[str, list] = {}
    for rec in records:
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
            "attractors": attractors, "thin": fires < MIN_FIRES_FOR_CONFIDENCE,
            "window": window, "adoption": _adoption(audit, fires)}


def pin_gate(routing) -> dict:
    """Weekly supply-chain pin gate (Tier-2 ⑤): every external skill must hold
    an immutable identifier. Fail-closed: any error running the check reports
    not-ok rather than silently green."""
    try:
        audit = routing.load_audit_module()
        pins = audit.pin_check(audit.discover_skills())
        return {"ok": pins["unpinned_count"] == 0,
                "unpinned": pins["unpinned"], "unpinned_count": pins["unpinned_count"],
                "external": pins["external_count"], "error": None}
    except Exception as exc:  # noqa: BLE001 — governance signal must not crash the report
        return {"ok": False, "unpinned": [], "unpinned_count": -1,
                "external": -1, "error": str(exc)[:160]}


def main() -> int:
    routing = load_routing()
    today = dt.date.today().isoformat()
    green, recall_line = recall_is_green()
    log = analyze_log(routing)
    window = log.get("window", {"kind": "last_days", "days": ATTRACTOR_WINDOW_DAYS})
    adoption = log.get("adoption", {"available": False, "fires": log["fires"], "invocations": None, "ratio": None})

    L = [f"# Router self-tune report — {today}", "",
         "## Health",
         f"- recall gate: {'GREEN' if green else 'RED — REGRESSION, investigate'} ({recall_line})",
         f"- log: {log['emissions']} emissions, {log['fires']} fired ({_window_label(window)})",
         f"- adoption (same window, non-causal): {_adoption_label(adoption)}", ""]

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

    pg = pin_gate(routing)
    L += ["", "## Supply-chain pin gate (Tier-2 \u2464)",
          (f"- PASS \u2014 all {pg['external']} external skills pinned"
           if pg["ok"] else
           f"- **FAIL** \u2014 {pg['unpinned_count']} unpinned external skill(s)"
           + (f" (check error: {pg['error']})" if pg["error"] else ""))]
    for u in (pg["unpinned"] or [])[:5]:
        L.append(f"  - {u['runtime']}/{u['name']}: {u['reason']} ({u['path']})")

    rv = revisit_tracker(today, green, len(log["attractors"]), log["thin"])
    L += ["", "## Codex routing-hook revisit (Tier-2 ④)",
          f"- clean-week streak: {rv['streak']}/{rv['need']} consecutive ISO weeks "
          f"({'this week CLEAN' if rv['clean'] else 'this week NOT clean'}; "
          f"{rv['weeks_recorded']} weeks recorded)"]
    if rv["error"]:
        L.append(f"- ⚠ {rv['error']} → streak NOT trusted this run (fail-closed).")
    L.append(
        "- **REVISIT CONDITION MET** — Claude-side router stable for "
        f"{rv['need']}+ consecutive clean weeks. Re-evaluate porting the routing "
        "hook to Codex (`user_prompt_submit` is supported, confirmed 2026-07-12)."
        if rv["met"] else
        "- not yet met — routing hook stays deferred on Codex (installing an "
        "un-tuned router causes bad suggestions). Keep accumulating clean weeks.")

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
    revisit_note = " ④ REVISIT MET" if rv["met"] else f" ④ {rv['streak']}/{rv['need']} clean"
    pin_note = " pins OK" if pg["ok"] else f" PINS FAIL({pg['unpinned_count']})"
    summary = (f"recall {'GREEN' if green else 'RED!'}; "
               f"{n_att} attractor proposal(s); {log['fires']} fires logged;"
               f"{revisit_note};{pin_note}")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{summary}" with title "Skill router self-tune"'],
            capture_output=True, timeout=10)
    except Exception:
        pass

    print(report)
    print(f"[report saved: {out}]")
    return 0 if green and pg["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
