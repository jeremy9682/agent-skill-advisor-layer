#!/usr/bin/env bash
# Advisory dual-seal read-only review: Fable (900s) then Codex Sol (900s).
# agent-run owns family serial locks; legs run sequentially by design.
#
# For governed final review (producer independence + checkpoint), use:
#   agent-run run auto --task-shape codex_final_review \
#     --producer-run-id <id> --checkpoint-event evt-... --cwd "$CWD" "$PROMPT"
# fable_final_review remains disabled until live canary passes.
set -euo pipefail

CWD="${1:?usage: agent_run_review.sh <repo-cwd> <review-prompt>}"
PROMPT="${2:?usage: agent_run_review.sh <repo-cwd> <review-prompt>}"
shift 2 || true

echo "== Fable final review (serial, 900s, advisory explicit route) =="
agent-run run claude \
  --seat fable-final-review \
  --model fable \
  --effort max \
  --mode read-only \
  --timeout-seconds 900 \
  --no-skills \
  --cwd "${CWD}" \
  "${PROMPT}"

echo "== Codex Sol final review (900s, minimal-runtime, advisory explicit route) =="
agent-run run codex \
  --seat codex-final-review \
  --model gpt-5.6-sol \
  --minimal-runtime \
  --mode read-only \
  --timeout-seconds 900 \
  --no-skills \
  --cwd "${CWD}" \
  "${PROMPT}"
