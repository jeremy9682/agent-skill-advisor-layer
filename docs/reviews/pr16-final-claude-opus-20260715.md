# PR #16 final review (claude-final-review)

run_id: `37320374-af0e-4117-9b8b-a18f82c7603f`
model_observed via agent-run journal.

---

I've reviewed the review pack plus the actual source and test files in the worktree. Here is my cross-family final-review verdict.

VERDICT: PASS_WITH_CONCERNS

BLOCKERS: none

CONCERNS:
- **Timeout-class coverage is partial.** `run_codex_json_process` implements four real timeout classes, but only `timeout_first_event` has a direct unit test (`test_run_codex_json_process_classifies_first_event_timeout`). The `timeout_idle`, `timeout_total`, and `timeout_startup` (OSError) branches are live code with no regression test. `classify_failure` passes `timeout_class` through generically, so surfacing is safe, but the streaming loop's idle/total/startup detection is unverified.
- **`codex-json-stream` model-enrichment path is untested.** In `run_provider` (agent_provider_run.py:878–886) the fallback that fills `model_observed` from `extract_codex_model_from_events` and stamps reason `"codex-json-stream"` has no test. I verified it reads only real event fields (`model`/`current_model_id`/`item.model`) and never `model_requested`, so it does not violate the anti-forge invariant — but the branch itself has no coverage.
- **`task_focus` and `reviewer_graph_gaps` are emitted but not asserted.** `build_route_doctor` builds both structures; tests assert `reviewer_graph` and route `status`/`blockers`, but no test pins `task_focus` (required/optional route split) or `reviewer_graph_gaps` shape. Regressions there would pass CI.
- Minor: `mark_progress(now)` ignores its `now` argument and re-reads `utc_now()`; harmless but dead parameter.

CONSTRAINTS_IF_MERGE:
- Keep `latest_provider_evidence` fail-closed intact: `run_status=="completed"` AND `exit_code==0` AND `model_observed not in {"","unknown"}`, with `model_observed==model_requested` for claude/codex and verified broker health + attributed session for cursor/grok. Do not weaken future-skew (`live_evidence_future_skew_seconds`) or TTL (`live_evidence_max_age_seconds`) branches.
- `model_observed` must continue to originate only from parsed session/stream artifacts; the `-> "unknown"` sentinels in `extract_codex_model_from_jsonl` / `extract_claude_model_from_jsonl` / `parse_session` must never be replaced by `model_requested`.
- Codex `exec --json` must remain the read-only/execute template (`agent-providers.yaml`) that triggers the streaming dispatch; do not revert to blocking capture.
- `--no-provider-tools` stays Claude-only; no new coordination plane / no new Cursor seat.
- Before shipping, add regression tests for the idle/total/startup timeout classes and the `codex-json-stream` enrichment path.

SUMMARY:
All five must-check invariants hold. `model_observed` is never forged from `model_requested` — the parsers and the stream fallback return `"unknown"` when identity is absent, and downstream evidence marks such runs `run-succeeded-health-unverified` (tested per provider). Codex `--json` streaming is a genuine selector-based reader with real `timeout_startup|first_event|idle|total` classification, and the manifest emits `exec --json`. Doctor `ready|degraded|blocked|disabled` semantics are correct and directly tested via `classify_route_status` plus doctor integration tests. Fail-closed live-evidence gating (exit0 + observed-model + freshness) is intact and not relaxed. The concerns are test-coverage gaps on real but independently-untested branches, not correctness defects — safe to merge with the constraints above and a follow-up to close the coverage gaps.
