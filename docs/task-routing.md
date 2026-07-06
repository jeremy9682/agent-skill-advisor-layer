# Task Routing

Use the smallest workflow that protects the user's goal.

## Routing Table

| Task shape | Default route | Required evidence |
| --- | --- | --- |
| Small fix | Codex edits directly, runs focused verification, reports result | Test, lint, typecheck, or a concrete reason none applies |
| New feature | Claude reads project context and `docs/solutions/`, writes an implementation-ready plan, then implementation proceeds against that plan | Plan with repo-relative paths, test paths, decisions, and rationale |
| Broad refactor | Claude plans and may implement in bounded units; Codex reviews against the plan before ship | Plan traceability, migration or rollback notes where relevant |
| Bug or failing test | Systematic debugging first: reproduce, form hypotheses, prove root cause, then fix | Regression test or characterization evidence for the root cause |
| Code review | Reviewer compares the diff to an intent statement, not only local style | Findings with severity, file references, and verification needs |
| Release or ship | High-cost gate workflow after explicit approval | Green checks before push or production action |
| Retrospective or learning | Capture only reusable learnings into project-local `docs/solutions/` | A solution note that future planning can read |
| Skill install or update | Supply-chain change: Codex-led review of the SKILL.md diff and any `scripts/`, provenance pinned | Manifest provenance entry (repo, ref, tree hash) from `skill_audit` |

## Default Agent Split

Claude handles the plan-heavy side: product ambiguity, architecture, long-context
understanding, cross-file refactors, and implementation-ready plan creation.

Codex handles the landing side: precise edits, bug fixes, diff review, focused
regression checks, and confirming that the gate evidence is real.

Direction ownership follows task shape: fix-shaped work (bugs, diff
convergence, regressions) may take direction from Codex; judgment-shaped work
(architecture, product tradeoffs, schema, 0-to-1) takes direction from the
Claude side. No agent holds direction and sole final review of the same
judgment-shaped change.

## Seats Rule

Each change has three seats: direction, landing, final review.

- Landing seat and final-review seat are never the same agent.
- Direction seat and final-review seat are never the same agent.
- Final review defaults to Codex (cross-family autopsy); keep it there even
  when other seats rotate.
- Direction-seat fallback when Fable is unavailable or not warranted:
  Fable -> Opus -> Claude session plus a blind Codex plan review.

Monitoring is not a seat: replace live monitoring with three gate events —
plan gate (only when a change spans 2+ modules, a public interface, or a data
model), final diff review, and the ship gate.

## Repo Profiles

Restricted-zone-heavy repos (money, permissions, migrations, DocType, PII —
e.g. a production ERP) default non-mechanical work to:

```text
plan (Claude) -> blind plan review (Codex) -> implement -> final review (Codex)
```

Other repos use the conditional triggers above. Small fixes bypass everywhere.
Every task gets a process budget up front; when exceeded, drop extra agents
and keep only minimal verification plus final review.

## Scale Guardrail

Do not run brainstorm, plan, simplify, review, and compound steps for work that
is obviously a small fix. For small fixes, the correct workflow is:

```text
intent -> edit -> verify -> report
```

For feature work, the correct workflow is:

```text
read solutions -> plan -> implement -> simplify when useful -> review -> gate -> compound when reusable
```

For bug work, the correct workflow is:

```text
reproduce -> root cause -> fix -> regression evidence -> gate -> solution note when root cause is non-obvious
```
