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
- `displayed recall` — expected skill would actually be shown by the
  production firing rule, not merely ranked.
- `negative precision` — prompts marked as no-skill-needed stayed silent.
  These cases catch over-triggering such as agent-to-agent review briefs being
  misread as design or content tasks.
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

## Router Hook And Hints

`scripts/skill_router_hook.py` (UserPromptSubmit) suggests up to three
candidate skills per prompt using the same index plus the
`routing-evals/hints.yaml` overlay — extra triggers (mostly Chinese),
negative triggers to damp black-hole skills, and cwd domain scoping. Hints
never edit third-party SKILL.md. The hook fires only above
`FIRE_THRESHOLD` (single source in `routing_eval.py`, calibration table in
its header), speaks in advisory language only, degrades to `{}` on any
internal failure, and logs each emission (sha + 80-char head, repo
basename only) to `~/.codex/skill-governance/routing-log.jsonl`.

The hook also skips known meta-prompts such as task notifications and
agent-to-agent review briefs. Those prompts are already instructions to another
agent, not user intent for local skill routing.

## Growing The Case Set

New cases come from real routing failures (missed triggers, wrong
suggestions, rejected suggestions), appended to the `candidates:` zone in
`cases.yaml` and promoted after human review. `routing_eval.py --doctor`
drafts candidate stanzas from the router log; promotion stays manual.
Eval before router held: the hook landed only after this eval measured
the hints overlay (recall@3 44% -> 100%, 2026-07-06).
