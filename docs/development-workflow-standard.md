# Skill-First Development Workflow Standard

Version: 2026-07-06

This is the canonical cross-agent development standard for Codex, Claude Code,
and other skill-based agents. Local `AGENTS.md` and `CLAUDE.md` files should
link here instead of copying long, drifting workflow text.

## Core Doctrine

1. Every unit of engineering work should make the next unit easier.
2. Small fixes stay small. Do not wrap a single-file fix in a six-step process.
3. Complex work needs a durable plan before implementation.
4. Review uses the user's intent, not just the diff.
5. The agent that writes a substantial change should not be the only reviewer.
6. Nothing gets pushed or shipped without green evidence.
7. Autonomous pipelines are high-cost workflows. Suggest them, then wait for
   explicit approval.

## Role Boundaries

| Layer | Owns | Does not own |
| --- | --- | --- |
| Shared GitHub standard | Canonical rules, schemas, routing tables, gate policy | Project-private learnings or machine-local config |
| Fable or strongest available model | Low-frequency standard review, skill distillation, conflict arbitration | Daily code execution |
| Claude | 0-to-1 design, long-context synthesis, implementation-ready plans, broad refactors | Final review of its own large change |
| Codex | Scoped code edits, bug fixes, diff review, regression checks, gate confirmation | Architecture-scale 0-to-1 decisions |

## Adopted Sources

- [`no-mistakes`](https://github.com/kunchenguid/no-mistakes): adopt gate
  semantics, intent-first review, and the mechanical-vs-intent-touching finding
  split. Do not default to its git proxy or disposable-worktree full pipeline.
- [`compound-engineering-plugin`](https://github.com/EveryInc/compound-engineering-plugin):
  adopt the plan artifact quality bar, `docs/solutions/` learning loop,
  simplify-before-review step, and report-only review contract. Do not default
  to `/lfg`.
- Superpowers and gstack: keep the engineering discipline, systematic debugging,
  verification, QA, review, and retro practices, routed by task size.

## Standard Artifacts

- Intent statement: see [`schemas/intent.md`](../schemas/intent.md).
- Reusable learning note: see [`schemas/solution.md`](../schemas/solution.md).
  If a project has no `docs/solutions/` yet, planning should offer to create
  it from [`examples/project-overrides/docs/solutions/`](../examples/project-overrides/docs/solutions/),
  not fail; only complex or recurring problems require a note.
- Task routing: see [`docs/task-routing.md`](task-routing.md).
- Gate policy: see [`docs/gate-policy.md`](gate-policy.md).
- Routing evals: see [`docs/routing-evals.md`](routing-evals.md).

## Non-Goals

- This repo is not a mirror of every useful skill.
- This repo does not store project-private `docs/solutions/` entries.
- This repo does not make autonomous shipping the default.
- This repo does not replace project-specific tests, CI, or review rules.
