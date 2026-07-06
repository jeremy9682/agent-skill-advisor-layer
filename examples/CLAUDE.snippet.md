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
- The agent that authored a substantial change should not be the only reviewer.

High-cost workflows such as `/no-mistakes`, `/lfg`, `ship`,
`overnight-execution`, and multi-agent swarms require explicit approval before
execution. Suggest first:

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

For reviews and release gates, require an intent statement that captures the
user's goal and constraints, not just the diff summary.
