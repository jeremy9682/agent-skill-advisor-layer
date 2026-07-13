# Harness Minimization Experiment — 2026-07-13

## Decision

Run a reversible observation period before deleting or uninstalling anything:

- disable the Claude and Codex Superpowers plugin entries;
- preserve the distilled local invariants: confirm destructive actions,
  establish root cause before fixing, use fresh verification before completion
  claims, and keep final review independent from the producing seat;
- keep the pinned `mattpocock/skills` bundle intact;
- treat `grilling` as explicit-only for top-level entry routing while allowing
  pinned Matt workflows to invoke `/grilling` internally;
- keep `/grill-me` as the compatibility entrypoint.

The Codex restriction is policy-enforced, not mechanism-enforced: Codex does
not mechanically honor upstream `disable-model-invocation`. The global
instructions and manifest classification remove active inducement, but they do
not isolate the file; the router-hints refinement is a separate follow-up (this
change does not yet touch the lexical router overlay).

## Duration and sample floor

- Observation window: up to 14 days, starting 2026-07-13.
- Minimum evidence: five qualifying tasks spanning at least three of these
  shapes: bug diagnosis, multi-step implementation, feature/TDD, ambiguous
  request clarification, adversarial product or design review.
- If the sample floor is not reached in 14 days, extend the observation rather
  than approving deletion from calendar time alone.

## Failure and rollback conditions

Rollback the relevant change if any of these occurs:

1. one unconfirmed destructive or irreversible action;
2. two fixes attempted before a reproducible root cause or evidence-based
   diagnosis;
3. two important completion claims without fresh, relevant verification;
4. one final review performed by the seat/session that produced the diff;
5. one top-level `grilling` execution when the user did not explicitly name
   `/grill-me`, `/grilling`, or the grilling workflow;
6. one broken internal `/grilling` dependency from a pinned Matt workflow;
7. a material increase in rework attributable to a missing Superpowers process
   detail.

For every experiment-period `--enforce-pins` run, separate new unpinned entries
inside this experiment's change scope (an immediate rollback-level regression)
from already-ledgered concurrent provenance work (record and cross-reference,
but do not misattribute it to this experiment).

## Verification and rollback

Verify configuration parsing, routing cases, focused tests, skill audit and pin
checks, the `mattpocock/skills` 21/21 exact tree, `grilling=explicit-only` with
`merge-only` unchanged, and absence of the Superpowers SessionStart injection
in a fresh Claude session.

Rollback is configuration-first: re-enable the plugin flag or restore the
previous routing text. Do not delete plugin caches, the standalone checkout,
the `/grill-me` wrapper, or pinned Matt skill files during this experiment.
