#!/usr/bin/env bash
# Summarize recent agent-run journal rows (ambiguous / timeout / auth errors).
set -euo pipefail

N="${1:-30}"
JDIR="${HOME}/.agent-runs"

python3 - <<PY
import glob, json, sys
from collections import Counter

n = int("${N}")
rows = []
for path in glob.glob("${JDIR}/*.jsonl"):
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

rows.sort(key=lambda r: r.get("ended_at") or r.get("started_at") or "")
recent = rows[-n:]
counts = Counter()
for r in recent:
    fc = r.get("failure_class") or "none"
    ss = r.get("session_status") or ""
    if "ambiguous" in ss:
        counts["session_ambiguous"] += 1
    if fc != "none":
        counts[fc] += 1
    elif r.get("exit_code") not in (0, None):
        counts[f"exit_{r.get('exit_code')}"] += 1

print(f"Last {len(recent)} journal rows (all repos, by ended_at)")
for key, val in counts.most_common():
    print(f"  {val:4d}  {key}")

print("\nRecent non-success:")
for r in reversed(recent):
    fc = r.get("failure_class") or "none"
    ec = r.get("exit_code", 0)
    ss = r.get("session_status") or ""
    if fc == "none" and ec == 0 and "ambiguous" not in ss:
        continue
    print(
        f"  {r.get('ended_at','?')[:19]}  {r.get('provider','?'):6s}  "
        f"{r.get('seat','?'):22s}  exit={ec}  fc={fc}  ss={ss[:40]}"
    )
PY
