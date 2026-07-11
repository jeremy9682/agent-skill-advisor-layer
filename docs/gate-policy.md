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
  gate still logged nothing** — lack of trust is NOT the blocker in exec
  mode. Root cause narrowed by v4 Codex re-review + probe #3: it is the
  `spawn_agent` handler in codex-cli 0.144.1 exec mode that emits no
  PreToolUse payload — NOT a blanket "PreToolUse fires only for Bash"
  (apply_patch/MCP handlers do emit payloads, and probe #3 shows one
  interactive spawn path emits them too).
- 2026-07-11 probe #3 (scripted interactive session — user's idea): drove the
  real Codex TUI via tmux, approved the user-level `pre_tool_use` hook trust
  through the actual review UI (trusted hash now persisted in
  `config.toml [hooks.state]`; the claude-mem plugin's new PreToolUse hook was
  deliberately left untrusted — outside the approval's scope). Result is
  **mixed and supersedes probe #2's "upstream-inert" conclusion**:
  - Two real `spawn_agent` dispatches hit the gate the moment trust landed
    (log lines 11-12): first missing `fork_turns` → code path reaches
    `_deny`; second arrived with `fork_turns` added → allowed. That is a
    live **deny → retry-with-params → allow** sequence: the gate's first
    real-traffic catch.
  - An explicit param-less spawn probe in the same session **succeeded with
    no gate log line** — a second spawn path (likely MultiAgent V2's
    `collaboration.` namespace, openai/codex#31814) bypasses the hook
    pipeline entirely. `codex exec` mode dispatches nothing (probe #2).
- 2026-07-11 probe #4 (the V2 bypass CLOSED): two changes landed together.
  (a) `~/.codex/config.toml` `[features.multi_agent_v2]`
  `hide_spawn_agent_metadata = false` + `tool_namespace = "agents"` — the
  community workaround for openai/codex#31814/#31864: it restores
  `model`/`reasoning_effort` visibility in the spawn schema AND moves the tool
  out of the reserved `collaboration.` namespace into `agents.*`. (b) The
  hook matcher in `~/.codex/hooks.json` was widened from `spawn_agent|Agent`
  to `(agents\.?)?spawn_agent|Agent` (dot optional — final-review hardening:
  the dispatched name normalizes to `agentsspawn_agent`, so a literal-dot
  branch alone relies on undocumented match semantics; the gate script also
  now uses `tool.endswith("spawn_agent")` as a second line) so it fires on
  the renamed tool. Result:
  a param-less `agents.spawn_agent` in a fresh trusted session was **blocked**
  — `spawn-gate.log` line 13 records `tool: "agentsspawn_agent"` (Codex
  normalizes the dot out) and the model received the full deny + resend
  guidance. The gate's internal classifier caught it via the
  `task_name`+`message` field heuristic, not the tool-name string, which is
  why normalization didn't defeat it. Separately confirmed the schema now
  accepts explicit `reasoning_effort="low"` + `fork_turns="none"` (they were
  hidden before the metadata flag).
- Conclusion (supersedes probe #3): **the gate is trusted and live on the
  interactive V2 spawn path — the same path that burned the quota on
  2026-07-10.** Remaining known gap: `codex exec` non-interactive mode still
  emits no PreToolUse payload for spawn (probe #2), so scripted exec spawns
  are ungated; and the metadata-visibility flag is a community workaround the
  upstream fix may obsolete. The AGENTS.md prose spawn table stays the
  belt-and-suspenders defense. Canary after each Codex CLI upgrade: re-run the
  interactive param-less `agents.spawn_agent` probe in a **fresh** session and
  look for the in-session deny message; if upstream ships the real #31814
  fix, revisit whether the `multi_agent_v2` overrides and the widened matcher
  are still needed.
- 2026-07-11 probe #5 (post-hardening re-verification). After the final-review
  hardening (matcher dot-optional + `endswith` second line + completion-check
  `{}` fix), a bare `agents.spawn_agent` in a **fresh** session was again
  **blocked with the gate's full deny message**. Two operational facts
  learned the hard way:
  1. **Trust changes arm only at session start.** Trusting a modified hook
     mid-session persists the hash, but the running session's hook set is
     already loaded — a probe in that same session shows the gate dead. The
     first re-verification "failure" was exactly this. Canary runs MUST use
     a fresh session after any hooks.json change.
  2. **The deny message, not the log, is the canary signal.** In probe #5 the
     deny fired but `spawn-gate.log` gained no line — hook filesystem writes
     can be lost depending on how the startup trust prompt was answered, and
     the script's fail-open `try/except` swallows the write error. Check the
     in-session "Tool call blocked by PreToolUse hook" message; treat the log
     as best-effort diagnostics only.
  Also observed: with the metadata flags on, upstream itself now rejects
  explicit `model`/`reasoning_effort` on full-history forks ("Full-history
  forked agents inherit the parent…") **before** hooks run — an extra
  upstream guard-rail on the worst-case (`fork_turns="all"`) path.
  Note: commit `1097769`'s message claims this section; the insert silently
  failed there and actually lands in this commit.
