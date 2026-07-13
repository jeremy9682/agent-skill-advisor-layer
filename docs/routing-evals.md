# Routing Evals

Skill routing (which skill should fire for this task) fails silently: the
skill is healthy, but nobody calls it. This layer measures that instead of
guessing.

## What Runs

`scripts/routing_eval.py` scores every case in `routing-evals/cases.yaml`
against locally installed skill names/descriptions with a deterministic
lexical baseline (CJK bigrams + IDF). No LLM, no network, CI-safe.

The same command also checks `model_routing_cases` in `cases.yaml`. These cases
do not score prompts or call models. They are deterministic policy regressions:
given `task_shape`, `risk_zone`, and `repo_profile`, the runner computes the
expected direction/landing/final-review seats, effort tier, and required gates,
then compares them with the case expectation.

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
- `model routing policy` — deterministic seat/effort/gate checks for task
  scale decisions. Failures mean the documented policy drifted, not that a
  runtime model should be switched.

## What This Is Not

A lexical baseline cannot judge task intent. Passing means the trigger
contract did not regress, not that routing is semantically correct. Do not
optimize descriptions into keyword soup to game recall; lint checks
structure, evals check regressions, humans check meaning.

Model-routing evals are also not a provider router. Provider fallback, budget
routing, rate limits, and latency-based routing belong in a model gateway such
as LiteLLM or a platform-native gateway, not in this repository.

## Codex Routing-Hook Revisit (Tier-2 item ④)

The prompt-time routing hook lives only on the Claude side. Porting it to
Codex is **deferred by design**: installing an un-tuned router causes bad
suggestions, so the Claude-side router must prove stable first. The revisit
condition is now **machine-tracked**, not remembered — `router_selftune.py`
appends a weekly status record to `~/.codex/skill-governance/selftune-status.jsonl`
and computes the streak of consecutive *clean* weeks (recall GREEN, zero
attractors, non-thin data). After `REVISIT_CLEAN_WEEKS` (4) clean weeks the
weekly report surfaces **REVISIT CONDITION MET**, at which point re-evaluate the
port (Codex supports the `user_prompt_submit` hook event, confirmed 2026-07-12,
so injection is technically feasible). Until the streak is met the hook stays
deferred. As of 2026-07-12 the streak is 0 (the router is currently over-firing
on ~14 attractors), which is exactly why porting now would be premature.

## Adoption Pivot & 30-Day Stop-Loss (2026-07-13 三席评估)

A three-seat assessment (Claude + Fable 5 + gpt-5.6-sol) found the governance
machinery is solid but the router's actual goal — the right skill getting
**used** — is not met: 294 skills installed, ~4 real invocations, and the
router **suggests a different set of skills than the ones actually used**
(it fires `huashu-design`/`social-monitor`; the used skills — `dev-workflow`,
`superpowers:*`, `codex` — are reached via the CLAUDE.md decision table, not
lexically). Verdict: **freeze governance-layer building**, pivot to adoption.
Three cheap changes landed (not a rebuild):

1. **Pre-filter** — `should_skip_prompt` now skips harness/system injections
   (`[SYSTEM NOTIFICATION`, `<system-reminder`, `<task-notification>`, etc.).
   These were the biggest noise source: a task-notification's dense text
   scored into `huashu-design`, and the `<task-notification>` tag sat past the
   agent-pattern window behind the preamble.
2. **Hot-route shrink** — `skill_router_hook.py` drops the confirmed
   non-converting content-creation attractors from the auto-suggest surface
   (`DEFAULT_HOT_ROUTE_EXCLUDE`, overridable via
   `GOV_DIR/hot-route-exclude.json`; an empty list restores prior behavior).
   Only removes suggestions, never adds; excluded skills stay reachable via the
   decision table / explicit invocation.
3. **Honest measurement** — `router_selftune.py` now windows attractor
   analysis to the last `ATTRACTOR_WINDOW_DAYS` (stale noise no longer blocks
   the clean-week streak forever) and reports a same-window **adoption** line:
   transcript-backed skill-invocations vs router-fires. This is a **non-causal
   period ratio, NOT a conversion rate** — the invocation count includes skills
   reached via the decision table / explicit calls that the router never
   suggested, and it is not linked per-prompt to any fire, so it can exceed 1.0.
   A true fire→invoke rate needs session-level attribution (future work); until
   that exists, do not read the ratio as "fraction of fires that converted".

**Stop-loss (hard):** for **30 days from 2026-07-13**, watch two signals.

- **Primary (causal-agnostic):** the absolute count of real, transcript-backed
  skill invocations. This does not depend on attribution — if the whole system
  drives only single-digit invocations, adoption has failed regardless of which
  layer gets the credit.
- **Secondary (context only):** the non-causal adoption ratio above and the
  attractor list. These *describe* noise; they must not be the sole basis of the
  decision (a near-zero ratio with no per-prompt linkage cannot by itself prove
  the router is useless — the absolute count does that).

If real invocations stay single-digit at day 30, the lexical auto-router has
failed its core hypothesis: **downgrade it to experimental / off**, and scope
the system down to what demonstrably creates value — the CLAUDE.md decision
table + the supply-chain audit (pin gate, ledger). Do NOT respond to still-low
adoption by adding more governance machinery. If you later want a real
conversion metric, build the per-prompt fire→invoke linkage first, then judge.

**Blocking follow-up before the day-30 decision (raised in final review, sol):**
`estimate_usage` (skill_audit.py) swallows per-file scan errors and returns an
all-zero dict, so "sources present but every scan throws" reads as an observed
zero. `_adoption`'s `_usage_sources_present` proxy only catches *missing*
sources, not this case. Before day 30, expose scan-health from
`estimate_usage` (e.g. a scanned/failed file count) so a total scan failure is
reported as unavailable — otherwise the primary stop-loss signal (absolute
invocation count) cannot be trusted as a real zero. This lives in a currently
do-not-touch file, so it is intentionally out of the adoption-pivot PR.

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

Do not add model/effort classification to this hook. Model routing is recorded
in intent and checked offline so the prompt hot path stays no-LLM, no-network,
advisory-only, and failure-silent.

## Growing The Case Set

New cases come from real routing failures (missed triggers, wrong
suggestions, rejected suggestions), appended to the `candidates:` zone in
`cases.yaml` and promoted after human review. `routing_eval.py --doctor`
drafts candidate stanzas from the router log; promotion stays manual.
Eval before router held: the hook landed only after this eval measured
the hints overlay (recall@3 44% -> 100%, 2026-07-06).
