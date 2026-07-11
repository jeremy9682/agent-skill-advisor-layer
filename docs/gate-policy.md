# Gate Policy

This policy adapts the useful parts of `no-mistakes` without making a local git
proxy mandatory.

## Push And Ship Rule

Nothing reaches a push target, pull request, deployment, or production action
until the relevant checks are green or the user explicitly accepts the residual
risk.

Green evidence can be:

- focused tests for the changed behavior;
- lint, typecheck, format, or build checks when relevant;
- smoke tests or browser QA for user-facing changes;
- CI checks for release or PR workflows;
- a clear written exception when no automated check exists.

## Intent Statement

Every review or gate run should include an intent statement. Intent is the
user's objective and constraints, not a summary of the files changed. See
[`schemas/intent.md`](../schemas/intent.md).

Thin intent produces noisy review. A good intent tells reviewers what would look
surprising in the diff but was deliberate.

The final reviewer must state accept or challenge, with a reason, for every
deliberate tradeoff listed in the intent. Rejecting a listed tradeoff by
default as "not best practice" is a review defect, not a finding.

## Evidence Is A Gate, Not A Reviewer

Green checks are a necessary condition, never an exemption. Changes touching a
public interface, schema, permissions, or a migration still require
cross-family review even when all checks pass. Final review runs at high
reasoning effort by default and must escalate to maximum effort when the diff
touches any restricted-zone trigger. Downshifting under a process budget
shrinks review scope, never review depth.

## Finding Classes

| Class | Meaning | Default action |
| --- | --- | --- |
| Safe mechanical | Formatting, imports, spelling, generated artifacts, obvious typo fixes | Agent may fix and verify |
| Gated auto | Concrete low-risk fix proposed, but behavior may be touched | Agent may apply only when the owning workflow permits and verification is available |
| Intent-touching | Product behavior, public API, data model, auth, billing, UX, or tradeoff changes | Escalate to user or plan owner |
| Advisory | Risk, missing coverage, rollout note, or future cleanup | Record without blocking unless severity demands |

## Autonomous Pipeline Approval

Workflows such as `/no-mistakes`, `/lfg`, `ship`, `overnight-execution`, and
multi-agent swarms can create commits, push, open PRs, or watch CI. They are
high-cost workflows.

Do not start them from a broad outcome request like "ship this" or "finish the
feature". Use:

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

Start only after the user explicitly says to run, start, enable, pair, set up,
or launch that specific workflow.

## Active Gate Runs

If a gate workflow is active, do not silently bypass it. Either continue the
gate, stop it intentionally, or tell the user that manual edits will invalidate
the current run and require re-validation.

## Runtime Verification Evidence

- 2026-07-11 spawn-effort-gate probe #1: the hook is registered in
  `~/.codex/hooks.json` (PreToolUse) and passes 9/9 unit tests, but
  `~/.codex/config.toml` `[hooks.state]` carries **no trusted hash for the
  user-level `pre_tool_use` entry** (only `session_start`/`stop` are trusted),
  and a live probe session asking for a param-less `spawn_agent` produced zero
  new lines in `~/.codex/hooks/spawn-gate.log` (the 10 existing lines are two
  sub-second unit-test bursts from 2026-07-10).
- 2026-07-11 probe #2 (user approved gate activation): re-ran the param-less
  `spawn_agent` probe under `--dangerously-bypass-hook-trust`, which removes
  the trust barrier for that one invocation. **The spawn succeeded and the
  gate still logged nothing** — lack of trust is NOT the blocker. Upstream
  docs and third-party testing agree: Codex CLI PreToolUse currently
  dispatches reliably **only for shell (Bash) tool calls**; `apply_patch`,
  most MCP tools, and collab tools like `spawn_agent` never reach the hook
  pipeline. This resolves the MF-2 uncertainty flagged in the gate's own
  design memo — negatively.
- Conclusion: **the gate is upstream-inert; no local action (including hook
  trust approval) can activate it on codex-cli 0.144.1.** The spawn effort
  rule stays prose (AGENTS.md spawn table: explicit `model` +
  `reasoning_effort` + `fork_turns` on every `spawn_agent`), same failure
  family as openai/codex#31814. Canary: after each Codex CLI upgrade, re-run
  the probe (`codex exec --dangerously-bypass-hook-trust` asking for a
  param-less spawn) and check `spawn-gate.log` for a deny line; flip this
  entry to "active" only on log evidence.
