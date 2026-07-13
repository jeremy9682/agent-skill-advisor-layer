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
5. Seats rule: neither the landing seat nor the direction seat final-reviews
   its own change; final review defaults to Codex.
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
- Superpowers plugin: disabled by default after its useful discipline was
  distilled into the local canon. Keep root-cause-first debugging, fresh
  verification, QA, review, and retro practices, routed by task size; do not
  restore an always-on harness without evidence from the retirement experiment.
  gstack is not part of this retirement — its engineering-discipline practices
  stay in effect.

## Standard Artifacts

- Intent statement: see [`schemas/intent.md`](../schemas/intent.md).
- Reusable learning note: see [`schemas/solution.md`](../schemas/solution.md).
  If a project has no `docs/solutions/` yet, planning should offer to create
  it from [`examples/project-overrides/docs/solutions/`](../examples/project-overrides/docs/solutions/),
  not fail; only complex or recurring problems require a note.
- Task routing: see [`docs/task-routing.md`](task-routing.md).
- Gate policy: see [`docs/gate-policy.md`](gate-policy.md).
- Routing evals: see [`docs/routing-evals.md`](routing-evals.md).

## Spec-to-ticket constraint preservation

When decomposing an ADR / frozen intent into a spec or tickets, the authoring
seat MUST preserve verbatim the upstream hard constraints that spec/ticket owns
or depends on — they decay silently otherwise (YunChouAI M1a: a spec softened the
ADR's authorization set, atomicity, and concurrency guarantees into looser prose,
and a strong-model implementation passed its own tests while five governance gaps
remained, which a cross-family review later surfaced; M1b: a spec restated the
`{boss, founder}` authorization set as "boss/finance"). Rules:

- Every spec/ticket cites a stable upstream anchor (an ADR section, a
  frozen-intent heading, or an immutable permalink), and does not paraphrase the
  closed enums, authorization sets, atomicity, concurrency, or audit-field
  requirements it owns or depends on — quote them.
- Produce a constraint-coverage matrix: `upstream anchor → original hard
  constraint → owning ticket/PR → acceptance test/evidence → waiver/amendment (if
  any)`.
- An accidental omission is fixed in the downstream spec/ticket; a conflict or a
  deliberate deviation stops and goes through an amendment/waiver approved by the
  upstream owner (never self-approved by the landing seat) and cited in the
  matrix. The landing agent is told to stop and report on any downstream↔upstream
  conflict (ticket↔spec, spec↔ADR, ticket↔ADR), never to quietly touch a frozen
  artifact.
- Prefer vertical-slice tickets (one independently demonstrable end-to-end path)
  over horizontal technical layers; "vertical" is the default, not an absolute —
  a safety boundary, frozen oracle, or cross-cutting infra may be its own ticket,
  but the trade-off is stated and the constraint stays covered in the matrix. The
  non-negotiable is constraint traceability, not slice shape.

## Non-Goals

- This repo is not a mirror of every useful skill.
- This repo does not store project-private `docs/solutions/` entries.
- This repo does not make autonomous shipping the default.
- This repo does not replace project-specific tests, CI, or review rules.
