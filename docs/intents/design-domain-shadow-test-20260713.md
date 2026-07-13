# Intent: deterministic design shadow selector (2026-07-13)

Goal: turn an explicit, structured design task contract into reviewable
per-deliverable selection records, so the design catalog can be tested before
any prompt-time routing is contemplated.

User-facing outcome: a UI/deck/media task has one named visual author, bounded
baseline and overlay facets, advisory gates, and a truthful distinction between
selection and evidence of actual skill usage.

In scope: a local CLI, a Markdown record schema, and 19 deterministic cases.
The selector reads local YAML only and can write a YAML record when a human
runs it explicitly.

Out of scope: LLM calls, natural-language prompt classification, UserPromptSubmit
or any runtime hook, embeddings, plugin-cache enforcement, automatic
`DESIGN.md` generation, skill invocation, model selection, and mutation of
the existing phase-1 catalog audit.

Deliberate choices:

- Do not infer direction from prose.  Unqualified decks and explicitly
  unresolved direction return `needs_direction`.
- Keep one record and one visual author per deliverable.  Every status uses the
  same record shape, with `reason: null` only for a successful selection. A
  task with two deliverables creates two records rather than blending authors.
- Scope `design-systems` facets.  In the Apple+CJK regression only
  `cjk-typography` and `cjk-spacing` are active; `erp-structure` is explicitly
  suppressed.  CJK typography wins over Apple typography-micro rules for
  letter-spacing.
- Treat Apple Design as an interaction/material overlay, never a whole-page
  author.
- Verify every selected author, baseline, overlay, and gate declares support
  for the requested surface in the catalog; unsupported combinations become
  `needs_direction`, never silent attachment.
- A selection is not proof of use: a requested public usage claim needs a
  task-, deliverable-, skill-, UTC-time-, and SHA-bound `read` or `invocation`
  attestation. Reads must resolve inside that skill's catalogued installation;
  invocation receipts must contain the matching event. `artifact` is rejected
  because a deliverable alone does not establish skill use.
- Do not invent review coverage. Video export uses the catalogued
  `review-animations` only when `motion_source: html-interface` is explicit;
  generic video and image output record a `gate_note` until a truthful gate is
  catalogued.

Verification: focused pytest loads all 19 YAML contracts, asserts exact
Apple+CJK facets/precedence, tests ambiguous/multi-deliverable/evidence paths,
invokes the CLI without network access, confirms malformed YAML exits cleanly
without a Python traceback, and runs committed Chromium interaction checks.
