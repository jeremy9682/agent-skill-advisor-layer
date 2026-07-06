# Routing Evals

Skill routing (which skill should fire for this task) fails silently: the
skill is healthy, but nobody calls it. This layer measures that instead of
guessing.

## What Runs

`scripts/routing_eval.py` scores every case in `routing-evals/cases.yaml`
against locally installed skill names/descriptions with a deterministic
lexical baseline (CJK bigrams + IDF). No LLM, no network, CI-safe.

Reported per run:

- `recall@3` — expected skill surfaced in top-3 candidates.
- `gate-dependency events` — suggest-confirm skills that surfaced; these are
  the prompts where the approval gate is load-bearing.
- `unexpected high-cost candidates` — suggest-confirm skills surfacing for
  prompts that never asked for them (fails `--check`). A measured, tolerated
  leak can be documented per-case as `known_leaks:` with a dated comment; it
  stays visible in reports but does not fail the gate until the entry is
  removed.
- description lint — trigger-clause and confirm-language checks.
- supply-chain evidence — root, path, SKILL.md hash, frontmatter issues for
  every skill evaluated (gate evidence for the skill layer itself).

## What This Is Not

A lexical baseline cannot judge task intent. Passing means the trigger
contract did not regress, not that routing is semantically correct. Do not
optimize descriptions into keyword soup to game recall; lint checks
structure, evals check regressions, humans check meaning.

## Growing The Case Set

New cases come from real routing failures (missed triggers, wrong
suggestions, rejected suggestions), appended to the `candidates:` zone in
`cases.yaml` and promoted after human review. Planned next iteration:
a `skill doctor` step that records these events per session automatically.
Eval before router: no retrieval hook lands until this eval can measure
whether it helped.
