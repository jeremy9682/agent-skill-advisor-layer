# External Provider Orchestration

`agent-run` is the local seam between CLI model providers and the existing
three-seat/checkpoint/skill governance. Provider identity never replaces seat
identity.

## Install the local entrypoint

```bash
ln -sfn \
  /Users/zihan/Projects/agent-skill-advisor-layer/scripts/agent_provider_run.py \
  ~/.local/bin/agent-run
chmod +x /Users/zihan/Projects/agent-skill-advisor-layer/scripts/agent_provider_run.py
```

## Discover providers

```bash
agent-run discover
agent-run routes
agent-run doctor
agent-run ibom --run-id <run-id>
```

The checked-in manifest is portable across devices. It contains no account or
credential material. Each device still logs in to its provider independently.
`routing-policy.yaml` is the sole executable route canon. The provider manifest
contains adapters and capability discovery only; a top-level `routes` key is a
hard error.

## Run read-only work

```bash
agent-run run cursor \
  --seat codex-landing \
  --model composer-2.5 \
  --trust-workspace \
  --cwd /path/to/repo \
  "Review the existing implementation. Do not edit files."
```

The wrapper defaults to the existing skill router. Auto-eligible matches are
embedded with both the governance tree digest and a hash of the exact delivered
`SKILL.md` bytes. High-cost and explicit-only matches are
recorded as deferred and are not silently launched.

Embedded skill paths must resolve beneath explicit trusted content roots; a
tampered manifest cannot point the wrapper at an arbitrary local file.

Use an explicit skill when the user has approved or named it:

```bash
agent-run run claude --seat claude-direction --skill codebase-design "..."
```

## Write mode

Write-capable work requires both flags:

```bash
agent-run run cursor --seat codex-landing --model composer-2.5 \
  --mode execute --allow-write --trust-workspace \
  --checkpoint-event evt-... \
  "Implement the frozen intent."
```

This is only a provider permission. Governed routes and every write run require
an existing open/claimed checkpoint event. Worktree ownership, risk overlays
and ship gates remain responsibilities of the caller and repository canon.

## Evidence and privacy

Local events append to `~/.agent-runs/<project-slug>.jsonl` with mode `0600`.
They include provider/model/seat/session pointers, duration, exit code, separate
user/delivered prompt hashes, output hashes, checkpoint pointer and skill evidence.
They do not include prompt text, response
text, credentials, account IDs, API keys, cookies, or full command arguments.

Schema-v4 records also contain an Instruction BOM. Its digest covers the routing
canon and binding, observable AGENTS/CLAUDE instruction candidates, intent,
prompt-template and user-prompt hashes, selected skill digests, provider adapter,
model/effort/seat/risk and an MCP capability digest. Native provider instructions
that the wrapper cannot prove were loaded are marked
`provider-native-candidate-not-wrapper-confirmed`; provider built-in prompts use
`opaque:<client-version>`. I-BOM never stores instruction, prompt, response or
credential text.

`read_or_invoked: unknown` is deliberate when the native transcript does not
prove semantic skill use. Wrapper delivery proves exposure, not compliance.

The wrapper removes known API-key, auth-token and custom billing-endpoint variables
from child environments, so these adapters are directed toward the already logged-in
CLI account path. It never creates subscriptions, buys credits, enables auto top-up
or alters billing. This is a best-effort local guard, not proof that a provider will
never charge an already-paid account; account plan and billing state remain provider-side.

## Responsibilities

- Agent Sessions: transcript visibility and resume UX. Cursor is supported
  natively; Grok needs an upstream native provider patch and an Xcode build.
- `agent-run`: provider invocation, session pointer discovery, timing and skill
  evidence.
- checkpoint ledger: cross-seat ownership and handoff decisions.
- git/tests/CI: fact sources; journal and ledger claims never outrank them.

Future CLIs gain governed invocation and run-journal tracking after adding a
validated manifest entry. Native session attribution also needs a named adapter;
unknown adapters fail closed, and concurrent artifacts are recorded as ambiguous
instead of being guessed. Native Agent Sessions UI support remains a separate
source-code integration when that app lacks a provider plugin API.

## Deterministic task routing and route doctor

`routing-policy.yaml` carries the explicit runtime bindings. `agent-run` does not
ask an LLM to classify the prompt:

```bash
agent-run run auto --task-shape ordinary_bug_fix \
  --checkpoint-event evt-... \
  --cwd /path/to/repo "Diagnose this failing test without editing."

agent-run run auto --task-shape final_review \
  --producer-provider codex \
  --producer-run-id <journal-run-id-that-produced-the-diff> \
  --checkpoint-event evt-... \
  --cwd /path/to/repo "Review the frozen diff against its intent."

# Claude-family producer -> canonical Codex cross-family review
agent-run run auto --task-shape codex_final_review \
  --producer-provider claude \
  --producer-run-id <journal-run-id-that-produced-the-diff> \
  --checkpoint-event evt-... \
  --cwd /path/to/repo "Review the frozen diff against its intent."
```

The current routes map mechanical work to Claude Sonnet/low, ordinary bugs to
Codex Terra/medium, judgment and restricted-zone direction to Claude Opus/high,
and cross-family final review to Grok 4.5/high. Grok is enabled after both a
direct CLI run and a wrapped canary proved the primary Grok 4.5 turn succeeds.
GPT-5.6 Sol/xhigh has two distinct routes: `codex_final_review` is the canonical
cross-family pass for Claude-family producers, while `secondary_final_review` is
an independent same-family supplement for Codex producers and cannot replace the
required cross-family final in that case. Route model,
effort and seat fields are immutable. Explicit provider runs remain available for
Grok second opinions and Cursor named-model execution/review.

Claude Opus/high remains a normalized-governance-xhigh `claude_final_review`
route for Codex-produced work, including risk overlays. Fable is
`fable_final_review` and remains disabled until a live run succeeds. Cursor now
lists Fable 5, but its CLI returns `ActionRequiredError: Review Data Policy` until
the user acknowledges the model's retention policy; catalogue presence alone does
not enable the route. Provider
runs default to a 300-second timeout and journal timeouts as exit `124`;
`--no-skills` is an audited per-run escape hatch when auto-selected skill bodies
exceed the provider prompt budget. For isolated Codex audits, `--minimal-runtime`
adds `--ignore-user-config` so global plugins and hooks do not consume the review
budget; authentication and repository files remain available.

Review routes resolve `--producer-run-id` only from the current repository's
append-only journal. They require a successful write-capable producer run, reject
same-seat producers before launch, and reject same-session reuse after native
session attribution. The checkpoint must be open and claimed by the exact run seat;
malformed ledger rows fail closed instead of being skipped.
Risk triggers are passed with repeated `--risk-trigger` flags. Non-restricted
execution routes fail closed and direct the caller to `restricted_zone`; a final
review carrying a risk trigger must be both cross-family and `xhigh`. Provider
effort vocabularies are normalized separately: Grok's maximum `high` maps to the
governance `xhigh` floor without pretending the CLI accepts a nonexistent flag.

For a governed Grok route, process exit `0` is necessary but not sufficient. The
wrapper also requires an attributed native session whose `current_model_id` and
`primaryModelId` equal the requested model and whose session error count is zero.
If that evidence is missing, the run is journaled as `provider-health-unverified`
and fails closed.

Cursor is a formal broker provider. Its `models` output is parsed dynamically, so
Composer 2.5, Cursor Grok 4.5 and future model IDs do not require a second static
model list. Catalogue presence is still only `catalog-listed`; a successful run
plus an attributed native session whose observed model ID exactly matches the
requested ID provides `live-run-verified` evidence. Review
independence follows the concrete model family: Cursor Grok is xAI, Cursor Claude
is Anthropic, GPT models are OpenAI, Composer is Cursor, and Auto remains
undisclosed. If changed artifacts resolve to more than one Cursor session ID, the
wrapper now reports `ambiguous-concurrent-artifacts` even when only one candidate
matches the requested model; model matching cannot prove which concurrent process
owns that session. A single unrelated concurrent session that emits the only full
artifact pair remains an explicit residual until Cursor exposes a run-scoped ID.

`agent-run doctor` is non-generative: it inspects binaries, catalogues, recent
journal evidence, route blockers and the producer-family to reviewer-family graph.
Doctor readiness is a **diagnostic** surface (per-task required routes), not a
daily-work or ship gate: missing / stale / health-unverified evidence may show as
`degraded` or block *reported* readiness in `doctor`/`routes`, but normal
`agent-run run` does not require a periodic all-green canary ritual. Journal
evidence ages out after `live_evidence_max_age_seconds` (currently six hours) as
`stale-live-evidence` for honesty of that diagnostic view; it also rejects
timestamps more than five minutes in the future, while tolerating minor host
clock skew. Quota cooldown stays unknown unless a structured reset was observed.

`model_observed` is honest audit metadata for every provider: when adapters still
report `unknown`, the journal records `run-succeeded-health-unverified` rather
than forging identity from `model_requested`. That is recoverability, not a
doctor-green ritual.

## Live verification snapshot (2026-07-14, historical)

> Historical entitlement snapshot from the Phase 1 landing window. Not an
> ongoing requirement to keep doctor fully green or re-run serial canaries.
> Further live-evidence hardening / Phase 2 canary sweep was **cancelled**
> 2026-07-15 — see `docs/intents/doctor-live-evidence-hardening-20260715.md`.


- Grok CLI `0.2.101` completed a governed `grok-4.5/high` read-only route with
  an open checkpoint and a native session ID.
- Cursor Agent `2026.07.09-a3815c0` completed governed named-model smokes for
  `composer-2.5` and `cursor-grok-4.5-high`. The
  wrapper correlated each JSONL + SQLite pair into one session rather than guessing
  by newest mtime.
- After restarting Agent Sessions `4.3.1`, both new Cursor transcript sessions
  appeared as native `source=cursor` rows. DB-only state is a source input; the
  canonical indexed path is the emitted Cursor transcript JSONL.
- Agent Sessions still has no runtime provider plugin for Grok. A native source
  patch is prepared independently at
  `/Users/zihan/Projects/agent-sessions-grok`. Its real-format redacted fixture,
  discovery/parser, focused tests, Grok indexer, Unified index/search injection,
  enablement and basic badge/color wiring exist. Peripheral exhaustive UI switches
  still need a full Xcode compile to enumerate and close. Local typecheck/build/install
  remains blocked until full Xcode is present, so the installed App is unchanged.

Later the same day, one attempt emitted `402 spending-limit` and Grok was
conservatively disabled. A direct CLI canary then completed successfully with exit
`0`; its native session `019f60af-9aad-73f3-a948-0208977c21a7` records
`current_model_id=grok-4.5`, `primaryModelId=grok-4.5`, zero session errors and a
completed turn. The `402` belonged to the auxiliary `grok-build` title request,
not the main Grok 4.5 turn. Grok invocation and review routes are therefore enabled
again. Failure classification follows the main process exit/result, not an isolated
stderr substring. The wrapper still never upgrades, subscribes or buys credits.

The same review prompt was also used for a small baseline comparison. Codex Terra
(`77.9s`) and Grok 4.5 (`89.6s`) independently agreed on route override, session
attribution and billing-wording risks; Grok additionally identified the adapter-key
seam and delivered-prompt hash gap. Cursor's first full audit failed in `1.3s`
because headless workspace trust was missing; adding explicit `--trust` made two
subsequent read-only smokes pass in `17.0s` and `17.3s`. This supports using Grok as
an adversarial second opinion and Cursor Auto as a fast execution/review path, not
letting an opaque auto-router replace the three governance seats.

Final-gate status: Fable remains outside automatic use; Cursor now lists it, but
the required data-retention acknowledgement has not been accepted. Grok's direct
CLI canary proved that the main Grok
4.5 turn was available and completed; the earlier auxiliary-title `402` no longer disables
the provider. A wrapped Grok canary then completed as run
`e839a306-17f0-4c2c-9531-2ec2f6724b5d` with exit `0` and an attributed native
session. Claude Opus, Cursor Composer and Grok supplied independent architecture
reviews; a resumed Sol xhigh audit produced concrete hardening findings. The
wrapper now prevents explicit-provider route spoofing, binds reviewers to a
successful producer in the same repository, validates the full ledger schema,
normalizes governance effort, and requires primary-session evidence for governed
Grok runs. Cursor Auto remains undisclosed, while named Cursor models use their
concrete family and native model metadata for independence checks.

After those successful runs and a full Grok final-review pass, a later focused
re-review reached the free Grok Build rolling limit: the primary `grok-4.5`
request returned `429 subscription:free-usage-exhausted` with usage
`2,007,409 / 2,000,000` and a rolling 24-hour reset. This is a transient live
quota state, not evidence that the CLI never worked and not an authentication
failure. No upgrade, subscription, credit purchase or auto-top-up was attempted.
The portable manifest remains capability configuration rather than a stale account
quota database; callers must distinguish `discover` (installed/config-enabled)
from a live canary (currently quota-blocked until usage rolls off).

The final hardening pass added a shared pure ledger-history validator, an explicit
`codex_final_review` cross-family route for Claude-family producers, and normalized
the reciprocal `claude_final_review` governance tier for Codex-produced risk work.
The full repository suite then passed with 180 tests plus Ruff and `py_compile`.
Grok completed the earlier full cross-family review before its rolling limit was
reached; Sol xhigh passed the final focused ledger/evidence re-review, and Claude
Opus/high passed the latest cross-family route-and-ledger re-review.
