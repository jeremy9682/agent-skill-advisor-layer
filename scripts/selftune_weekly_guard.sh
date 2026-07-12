#!/bin/bash
# launchd weekly fallback for router_selftune (Tier-2 ④ streak assurance).
# The Claude scheduled task is the primary Monday runner; this guard only fires
# when the current ISO week has NO status record yet (machine was asleep/app
# closed on Monday). ISO-week dedupe in revisit_tracker makes double-runs
# harmless, so two runners cannot corrupt the streak.
set -u
STATUS="$HOME/.codex/skill-governance/selftune-status.jsonl"
PY="/Users/zihan/anaconda3/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
WEEK=$("$PY" -c "import datetime;y,w,_=datetime.date.today().isocalendar();print(f'{y:04d}-W{w:02d}')")
# already recorded this week → nothing to do
grep -q "\"week\": \"$WEEK\"" "$STATUS" 2>/dev/null && exit 0
exec "$PY" "$HOME/Projects/agent-skill-advisor-layer/scripts/router_selftune.py"
