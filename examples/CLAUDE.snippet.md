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

High-cost workflows such as `/no-mistakes`, `/lfg`, `ship`,
`overnight-execution`, multi-agent swarms, and the pinned mattpocock workflows
`research`, `code-review`, `improve-codebase-architecture`, and `wayfinder`
require explicit approval before execution. Suggest first:

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

For reviews and release gates, require an intent statement that captures the
user's goal and constraints, not just the diff summary.

Answer generic adversarial requests such as "push back", "反方审一下", "别顺着我",
or "找漏洞" directly without loading a skill. Start the one-question-at-a-time
`grilling` loop only when the user names `/grill-me`, `/grilling`, or explicitly
asks to run the grilling workflow. This top-level explicit-only rule does not
block internal calls from pinned Matt workflows. It is not a substitute for
code review, QA, security review, or ship gates.

For the published `mattpocock/skills` bundle, local routing policy, seat
independence, checkpoint ledger, risk overlays, final review, and ship gate
override upstream flow prose. Honor `disable-model-invocation` wrappers as
explicit-only. Optional spawn/parallel branches still require current-session
authority; `/implement` is not release authority.
