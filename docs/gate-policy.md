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

## High-Cost Skill Enforcement on Codex (Tier-2 item ③)

Claude Code gates high-cost skills with `disable-model-invocation` + the
skill-advisor prose. Codex ignores that field and only reads `name` +
`description`, so the only Codex-side mechanical lever is the skill's own
injected description. Whether that lever is usable depends on each skill's
disk topology. The table below maps the **declared** high-cost list (CLAUDE.md /
skill-advisor / AGENTS.md) plus deploy-capable skills surfaced during review.
It is **NOT an exhaustive census** of every state-changing skill Codex injects:
Codex loads from `~/.codex/skills`, `~/.agents/skills`, `~/gstack/.agents/skills`,
**and the plugin cache** (`~/.codex/plugins/cache/**`, ~222 SKILL.md files), so
a full "which injected skill can commit/push/deploy" audit is open-ended and
tracked as a follow-up (see below) — not claimed complete here. Map 2026-07-12:

| High-cost skill | Codex-injected via | Topology | Gateable in place? |
| --- | --- | --- | --- |
| huashu-agent-swarm | `~/.codex/skills` | real dir, upstream-synced (`huashu-skills`) | no — editing forks upstream, blocks sync |
| huashu-design | `~/.codex/skills` | real dir, upstream-synced (`huashu-design`) | no — same |
| gstack-pair-agent / gstack-retro / gstack-setup-gbrain | `~/.codex/skills` | **symlink** → shared `~/gstack` worktree | no — edit leaks to gstack + every runtime symlinking it |
| ship (as `gstack-ship`) | `~/gstack/.agents/skills` | shared gstack worktree | no — same leak |
| **overnight-execution** | `~/.agents/skills` | **real dir, no upstream, not shared** (separate inode from the `~/.claude` copy) | **yes — safe** |
| no-mistakes / lfg | absent from every Codex root | not injected | n/a |

**Deploy-capable skills NOT on the declared list** (surfaced by review; behave
high-cost — commit/push/PR/deploy — but not currently gated). Adding any to the
enforced list is a policy decision that touches all three policy surfaces + the
Tier-1 consistency lint, so these are **candidates pending the user's call**,
not silently added; all are shared/plugin sources (Option A anyway):

| Candidate | Codex-injected via | What it does |
| --- | --- | --- |
| land-and-deploy (`gstack-land-and-deploy`) | `~/.codex/skills` symlink → gstack worktree | merge PRs, drive a deploy |
| yeet (`github` plugin) | `~/.codex/plugins/cache/**/github/**` | commit, push, open a PR |

**Follow-up (open):** a full census of injected skills that perform
state-changing git/deploy operations — across all roots including the 222-file
plugin cache — is open-ended and deferred. The scalable answer is an audit
signal that flags any injected skill whose description matches
commit/push/deploy/PR but is not on the gated list, rather than maintaining this
table by hand. Until then the enforced scope is the declared list; this table is
illustrative, not a completeness guarantee.

**Corrected conclusion:** most Codex-side high-cost skills are either shared
symlinks (edit leaks) or upstream-synced copies (edit forks upstream), which
stay on **Option A** (AGENTS.md skill-advisor prose + the Tier-1 cross-file
consistency lint). But **`overnight-execution` is a clean, locally-owned real
dir** — the one safe target — and it is now **gated in place**: its description
in `~/.agents/skills/overnight-execution/SKILL.md` carries a `⚠️ HIGH-COST —
do NOT auto-start` marker (the only enforcement lever Codex honors). That edit
is a local skill-file change, not tracked in this repo, because the skill lives
outside it; backup at `SKILL.md.bak-gate`. Re-evaluate the same in-place gate
for any future high-cost skill that lands as a locally-owned real dir.

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
  meant to be left untrusted — but see probe #6: it was accidentally trusted
  during these multi-session probes and later removed). Result is
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
- 2026-07-11 probe #6 (Codex 5.6-sol re-review — Block, then fixed). The
  re-review (run once the `codex exec` stall recipe was fixed — see below)
  caught that `config.toml [hooks.state]` had **trusted** the claude-mem
  `pre_tool_use` hook (`codex-hooks.json`, the Codex variant of the same
  per-Bash `node` file-context worker audited earlier), contradicting the
  stated "left untrusted" decision. Root cause: over ~6 tmux probe sessions a
  trust keystroke landed on it (exact session unrecovered; effect corrected,
  not the forensics). Fix: removed **only** that one trust-state entry
  (backup `config.toml.bak-claudemem`), leaving the plugin enabled and its
  other hooks (`session_start`/`stop`/etc.) untouched — those remain trusted
  and are the source of the AGENTS.md "Memory Context" prepend and the
  recurring "Stop hook: invalid JSON" noise; scoping them is a separate,
  user-facing decision, not silently bundled here. Removing a trust entry is
  a de-grant (reverts to re-prompt-on-next-session), which is why hand-editing
  it is acceptable where hand-adding a trust hash is not.
- codex exec stall (found while running probe #6): background `codex exec`
  from Claude reliably hangs. Three compounding causes — (1) non-TTY stdin
  makes it wait on `Reading additional input from stdin...` until EOF that a
  background pipe never sends [primary]; (2) it inherits the full config.toml
  MCP set (npm/uvx cold-resolve, no global startup timeout); (3) it leaves
  MCP child processes orphaned, holding the stdout pipe open so `| tail`
  never sees EOF. Safe recipe for any background review:
  `timeout 1500 codex exec --ignore-user-config -m gpt-5.6-sol -c
  model_reasoning_effort=high --sandbox read-only "$PROMPT" < /dev/null >
  out.txt 2>&1`. Full write-up in the user's memory `codex-exec-stall-recipe`.
