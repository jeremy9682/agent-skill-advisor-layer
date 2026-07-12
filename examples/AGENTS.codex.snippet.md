## Skill Usage Routing

Before starting non-trivial work, check whether the task clearly matches an
installed local skill by name, trigger wording, domain, or file path. If it
does, load that skill's `SKILL.md` first and follow its workflow.

Do not load every skill preemptively. Load the smallest relevant set.

## Skill-First Development Standard

Use the shared standard as the canonical source:

- Workflow standard: `~/Projects/agent-skill-advisor-layer/docs/development-workflow-standard.md`
- Task routing: `~/Projects/agent-skill-advisor-layer/docs/task-routing.md`
- Gate policy: `~/Projects/agent-skill-advisor-layer/docs/gate-policy.md`
- Intent schema: `~/Projects/agent-skill-advisor-layer/schemas/intent.md`
- Solution schema: `~/Projects/agent-skill-advisor-layer/schemas/solution.md`

Default split:

- Claude handles 0-to-1 design, long-context synthesis, implementation-ready
  plans, and broad refactors.
- Codex handles scoped edits, bug fixes, diff review, regression checks, and
  final gate confirmation.
- Seats rule: landing seat never final-reviews itself; direction seat never
  final-reviews itself; final review defaults to Codex. Direction fallback:
  Fable -> Opus -> Claude session + blind Codex plan review.
- Direction ownership: fix-shaped work may take direction from Codex;
  judgment-shaped work takes direction from the Claude side.
- No live monitoring; three gates instead: conditional plan gate (2+ modules /
  public interface / data model), final diff review, ship gate.
- Restricted-zone-heavy repos default non-mechanical work to
  plan -> blind plan review -> implement -> final review; small fixes bypass.
- Executor may downshift effort freely (final review backstops it); final
  review runs high by default, max on restricted-zone diffs, never below high.

For reviews and release gates, require an intent statement that captures the
user's goal and constraints, not just the diff summary.

Use `grilling` as a lightweight daily adversarial pressure-test when the user
asks to be grilled, challenged, red-teamed, or says "push back", "challenge my
thinking", "拷问我", "质询我", "反方审一下", "别顺着我", or "找漏洞". `/grill-me` is
the explicit wrapper that delegates to `/grilling`. This is not a high-cost
workflow and must not replace code review, QA, security review, or ship gates.

The published `mattpocock/skills` bundle is a pinned external source. Local
routing policy, seat independence, checkpoint ledger, risk overlays, final
review, and ship gates override upstream flow prose. Treat upstream
user-invoked wrappers as explicit-only even though Codex ignores
`disable-model-invocation`. `research`, `code-review`,
`improve-codebase-architecture`, and `wayfinder` use the suggest-confirm gate;
any optional spawn/parallel branch also requires current-session authority.

## High-Cost Skill Suggestions

Some skills are valuable but costly, permissioned, or operationally disruptive.
Do not run these skills automatically. Instead, when the signal is strong,
suggest exactly one of them and wait for explicit user approval before running
it.

A user describing a large task or desired outcome is not itself approval to
start the high-cost skill. Approval must explicitly say to run, start, enable,
pair, set up, or launch that specific workflow after the suggestion is made.

Use `skill-advisor` as the routing reference for this layer.

Suggest these high-cost skills in these situations:

- `huashu-agent-swarm`: suggest when a task is large, multi-module, and
  parallelizable, such as building a full feature across backend, frontend,
  tests, docs, and QA, or when the user asks for multi-agent / swarm work.
- `gstack-pair-agent`: suggest when another agent such as Claude, OpenClaw,
  Hermes, Cursor, or another Codex session needs shared browser/page access or
  live QA context.
- `gstack-retro`: suggest after a sprint, week, large repair sequence, deploy,
  or multi-fix session when a retrospective would capture shipped work,
  repeated failure patterns, and next risks.
- `gstack-setup-gbrain`: suggest when the user asks for persistent project
  brain, gbrain, MCP-backed memory, long-term knowledge capture, or repeatedly
  loses project context across sessions.
- `no-mistakes`: suggest when the user wants a release gate, safe push,
  no-mistakes validation, PR/CI gate, or asks to ship with full validation.
- `lfg`: suggest when the user explicitly wants a hands-off implementation
  pipeline from plan through PR and CI watch.
- `ship` or `overnight-execution`: suggest before production-facing or
  long-running autonomous execution.
- `research`: suggest when a background agent should produce a cited
  primary-source Markdown artifact.
- `code-review`: suggest for the specific Standards + Spec parallel-sub-agent
  review, not ordinary diff or final review.
- `improve-codebase-architecture`: suggest for a broad Explore-agent
  architecture-health scan, not a known scoped refactor.
- `wayfinder`: suggest for a foggy multi-session effort that needs an issue map.

Suggestion format:

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

Skip this suggestion layer when the user is asking a small, urgent, or clearly
single-step task. Never let suggestion text block the requested work.

Routing QA expectation: for these high-cost skills, the default answer is
"suggest and wait for approval", not "execute directly".
