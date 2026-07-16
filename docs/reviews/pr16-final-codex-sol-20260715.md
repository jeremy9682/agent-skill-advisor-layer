# PR #16 final review (codex-final-review)

run_id: `<redacted-run-id>`
model_observed via agent-run journal.

---

VERDICT: FAIL
BLOCKERS:
- `scripts/agent_provider_run.py:1479` does not implement a real startup timeout: any `OSError`, including executable-not-found, is mislabeled `timeout_startup`; no startup deadline exists.
- Tests cover only `timeout_first_event`. They omit idle, total, startup, successful streamed telemetry, non-broker requested/observed mismatch, `task_focus`, and `reviewer_graph_gaps`, contrary to the intent.
CONCERNS:
- JSONL scanning stops after 400 rows, which can report stale model metadata for long or resumed sessions.
- Focused doctor lists only disabled siblings as optional, so `task_focus` does not fully express required-versus-optional routes.
CONSTRAINTS_IF_MERGE:
- Do not merge until startup classification is corrected and the missing fixture tests are added.
- Preserve exit-0 plus observed-model verification, requested/observed equality for Claude/Codex, and broker health checks.
- Never derive `model_observed` from `model_requested`; keep Codex provider-tool suppression unsupported.
SUMMARY: Model observation is not forged from the requested model, and absent evidence remains unknown.
Codex `exec --json` streaming and first-event/idle/total timers are implemented, but startup timeout is not real.
Doctor status classification and fail-closed route readiness remain intact.
The implementation and coverage gaps make this PR not merge-ready.
