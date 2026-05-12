## Skill Usage Routing

Before starting non-trivial work, check whether the task clearly matches an
installed local skill by name, trigger wording, domain, or file path. If it
does, load that skill's `SKILL.md` first and follow its workflow.

Do not load every skill preemptively. Load the smallest relevant set.

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

Suggestion format:

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

Skip this suggestion layer when the user is asking a small, urgent, or clearly
single-step task. Never let suggestion text block the requested work.

Routing QA expectation: for these four skills, the default answer is "suggest
and wait for approval", not "execute directly".

