## Intent

Goal: Establish the smallest auditable `design` domain catalog and offline selection contracts so local UI/design skills can be selected with a single visual author, clear baseline/overlay boundaries, and honest provenance.

User-facing outcome: Future UI, dashboard, deck, and motion work can explain which skill owns the deliverable, which CJK/design-system or interaction rules constrain it, and which review gates apply—without claiming that an overlay alone makes a page “Apple design.”

In scope: Add a ≤12-entry `design-skill-catalog.yaml` covering the eight entries in the shared CLAUDE.md design decision table plus `apple-design`; add a short `routing-policy.yaml` reference/invariant stanza; add 3–5 offline design selection contracts and a deterministic consistency audit; record current installation hashes and provenance boundaries; record plugin cache only as `sources_observed`.

Out of scope: Changing existing task/model routes, adding a runtime filter, embedding router, automatic `DESIGN.md` generation, full plugin-cache migration, pin enforcement changes, or changing any third-party SKILL.md.

Deliberate tradeoffs: Reuse the complete manifest `call_policy` vocabulary instead of inventing a new one, and permit overrides only when they tighten protection. Treat locally installed items without a verified git root as `local-derivative` with `installed_commit: null`, even when an upstream-looking name exists. Keep gstack projection hashes per installation because live manifest evidence shows they differ. Keep candidate skills from being automatically promoted to approved authors. Treat phase-1 design cases as locked oracle contracts, not evidence that a runtime selector already exists or works.

Constraints: One `visual_author` per deliverable at a time; project design context/CJK/accessibility baselines outrank optional style overlays; `apple-design` is an interaction/material/typography-micro overlay rather than a whole-page visual author; a claimed invocation requires read or invocation evidence. Preserve unrelated working-tree changes.

Verification expected: YAML parsing succeeds; catalog contains no more than 12 entries, all nine required names, only existing manifest call-policy values, and truthful null pins for local derivatives; each oracle contract has a scalar `expect.visual_author` and invocation-evidence requirement; the locked Apple-style Chinese UI contract is exactly `frontend-design` + `design-systems` baseline + `apple-design` overlay + `design-review`; live manifest/CLAUDE.md/policy consistency audit and focused regression tests pass.

Task shape: judgment

Risk zone: normal governance metadata; no runtime routing or production mutation

Model seats: direction=Claude-side judgment; landing=Codex implementation; final_review=Claude Fable 5 independent review plus Codex gpt-5.6-sol independent final review

Effort budget: Luna/low for manifest extraction; Terra/medium for catalog/eval authoring; current Codex for integration; Claude Fable 5 and GPT-5.6-Sol at final-review depth only

Scale gates: plan gate, independent design-governance review, final diff review
