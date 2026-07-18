# External Provider Orchestration

`agent-run` is the local seam between CLI model providers and the existing
three-seat/checkpoint/skill governance. Provider identity never replaces seat
identity.

## Current V1 implementation and evidence boundary

The V1 control plane is implemented locally: versioned DAG plans, bounded
ready-set scheduling, isolated worktrees and controller-created candidates,
deterministic integration/join, dependency bundles, controller leases and
fencing, exact resume reconciliation, ownership-checked cleanup, and the thin
`agent-run` bridge. It preserves native provider CLIs and their observed
receipts; it is not a raw-API multi-model replacement.

Cursor uses its native `--output-format stream-json` evidence first. A Cursor
model is attributed only when the stream identity is exact, including the
unique exact catalog-label-to-ID mapping where Cursor emits a display label;
ambiguous or conflicting identity fails closed. Artifact correlation remains a
bounded fallback, not a guess.

The `mechanical` and `mechanical_grok` bindings set `managed_skills: disabled`
for the orchestration hot path, preventing unrelated automatically selected
skill bodies from consuming the provider prompt budget. Their current doctor
state is `ready`; the intentionally parallel routes retain only the
`serial-lock-disabled` warning.

Provider invocation success is not review approval. A review is accepted only
when its final non-empty line is the one unique exact marker
`AGENT_RUN_REVIEW_VERDICT: PASS`. The corresponding `FAIL` marker, a missing
marker, trailing text, or multiple/ambiguous markers fail closed, even if the
provider process exited zero and returned a session receipt. Cleanup is allowed
only after integration and that final review acceptance both succeed.

Real local canaries exposed and then exercised this boundary. V1 exposed
concurrent-attribution, skill-prompt-bloat, and process-cleanup defects. V2 and
V3 exposed a further defect: the old scheduler marked the runs completed from
review-provider exit zero even though their stored Claude verdicts were
`FAIL`. The new verdict gate makes those artifacts retrospective negative
evidence rather than accepted canaries. V4 completed two concurrent native
producers, deterministic integration, and a Claude Opus review with the exact
machine `PASS` marker, then performed eligible cleanup. A subsequent Fable 5
Max audit passed with no P0/P1 and identified six fail-closed P2 gaps. Those
gaps were remediated: stream attribution is accepted consistently, provider
failures map to bounded retries with backoff, dirty writer retries stop before
a new session, legacy review uses the same verdict projection, command-string
interpreters and credential keys are rejected, and private candidate evidence
now includes exact acceptance argv. A fresh incremental Fable audit confirmed
all six groups closed and no P0/P1, then identified two residual P2 hardening
items. The final validator now detects wrapped interpreters such as
`env bash -c` and rejects acceptance declarations on read-only/no-writer plans
instead of silently recording commands it cannot execute. A final fresh Fable
closure review recorded in host-local evidence returned `PASS` with no
remaining P0/P1/P2. The interpreter check is an auditability guard for canonical `-c`
forms, not a universal executable sandbox; shell-free argv, frozen plans and
acceptance/review remain the actual safety boundaries.

V5 reran the final hardened path with an argv-native tracked verifier:

- Composer: `composer-2.5-fast`, 19.898 s.
- Cursor Grok: `cursor-grok-4.5-high-fast`, 23.248 s.
- The producers launched 0.194 s apart; producer wall time was 25.555 s versus
  a 43.146 s serial provider-time sum (about 1.69x for this canary only).
- Claude review: `claude-opus-4-8`, 50.983 s, delivered prompt 1,410 bytes.

These are capability and one-canary latency observations, not proof of a
general quality or end-to-end speed improvement. The local evidence summary is
under `~/.agent-runs/`; it remains host-local and is not a Git artifact.
The final local regression result was 392 pytest passes, 47/47 agent-run
functional checks, passing orchestrator functional QA, compilation, scoped
Ruff and diff checks.

## Install the local entrypoint

```bash
ln -sfn \
  /path/to/agent-skill-advisor-layer/scripts/agent_provider_run.py \
  ~/.local/bin/agent-run
chmod +x /path/to/agent-skill-advisor-layer/scripts/agent_provider_run.py
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

The current routes map mechanical work to Cursor `composer-2.5-fast`/low
(Shuttle Seal 飞梭; alternate `mechanical_grok` →
`cursor-grok-4.5-high-fast`), ordinary bugs to Codex Terra/medium, judgment and
restricted-zone direction to Claude Opus/high, and dual-seal final review to
Fable max + GPT-5.6 Sol xhigh (`fable_final_review` / `codex_final_review`).
Both Cursor routes queue on the same local provider-family lock only when
`serial_group` is set on the route; `mechanical` / `mechanical_grok` omit it so
parallel Cursor work is allowed.
A provider route is enabled only after both a direct CLI run and a wrapped
canary prove its primary turn succeeds.
GPT-5.6 Sol/xhigh has two distinct routes: `codex_final_review` is the canonical
cross-family pass for Claude-family producers, while `secondary_final_review` is
an independent same-family supplement for Codex producers and cannot replace the
required cross-family final in that case. Route model,
effort and seat fields are immutable. Explicit provider runs remain available for
Grok second opinions and Cursor named-model execution/review.

Claude Opus/high remains a normalized-governance-xhigh `claude_final_review`
route for Codex-produced work, including risk overlays. The
`fable_final_review` and `arbitration` routes request the exact native model ID
`claude-fable-5`; they were enabled only after direct alias and exact-ID Fable 5
Max read-only calls plus an attributed wrapped canary succeeded on 2026-07-18.
Their exact receipts remain host-local. A broker may list a model
whose CLI still returns a data-policy acknowledgement
error until the account accepts that model's retention policy; catalogue presence alone does
not enable the route. Provider
runs default to a **300-second** timeout when the caller omits `--timeout-seconds`;
governed review/direction routes in `routing-policy.yaml` carry **`timeout_seconds`**
(600 for long direction reads, **900 for final review**) that `agent-run` applies
automatically for `run auto --task-shape ...`.

**Serial execution:** governed routes declare `serial_group` in
`routing-policy.yaml` (e.g. `claude-family` for Fable/Opus review and Claude
direction). Explicit `agent-run run claude|codex|grok` also serializes via the
same family lock. Routes without `serial_group` (including mechanical Cursor
routes) do not take a family lock. Before spawning the primary provider process, `agent-run` takes an exclusive
flock under the configured journal root (`~/.agent-runs/locks/*.lock` by default).
Lock acquisition queues for at most the smaller of the run timeout and 900 seconds.
The journal's `stage_telemetry.serial_lock` records the group, wait timestamps,
wait duration and outcome; a lock-wait timeout is journaled with exit `75`.
Do not parallelize two runs in the same provider family on one host.

**Session attribution:** Claude runs use `--output-format stream-json` when possible;
`session_id` is taken from the stream (`session_attribution: stream-json`) with
file-diff as fallback only. Codex likewise reads `thread_id` from its `--json`
stream. The attribution method is one of `stream-json`, `file-diff`, or
`ambiguous`; `session_status` retains finer confidence details.

**Process cleanup:** Provider subprocesses run in a new session; timeouts call
`killpg` to reap child trees (Codex node orphans).

Failure receipts inspect both stdout and stderr. Auth expiry, quota exhaustion,
rate limiting and provider overload are separated as `auth-expired`,
`quota-exhausted`, `rate-limited`, and `upstream-overload`; textual deadline or
timeout failures remain `timeout` rather than generic `provider-error`.

`agent-run routes` and `agent-run doctor` expose effective route timeout and
serial group. Doctor emits `serial-lock-disabled` when no lock group can be
resolved for a route.

`--no-skills` is an audited per-run escape hatch when auto-selected skill bodies
exceed the provider prompt budget. For isolated Codex audits, `--minimal-runtime`
adds `--ignore-user-config` so global plugins and hooks do not consume the review
budget; authentication and repository files remain available.

Review routes resolve `--producer-run-id` only from the current repository's
append-only journal. They require a successful write-capable producer run, reject
same-seat producers before launch, and reject same-session reuse after native
session attribution. Brokered producers additionally require verified health evidence
plus exactly one accepted attribution state: `attributed-single-artifact`,
`attributed-correlated-artifacts`, or `attributed-stream-json`; unknown and ambiguous
states fail closed. The checkpoint must be open and claimed by the exact run seat;
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
wrapper reports `ambiguous-concurrent-artifacts` even when only one candidate
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
clock skew. Subscription quota/cooldown is deliberately outside the pilot
preflight contract: it is neither collected nor inferred, and an in-run rate
limit is recorded as an execution outcome rather than a preflight blocker.

`model_observed` is honest audit metadata for every provider: when adapters still
report `unknown`, the journal records `run-succeeded-health-unverified` rather
than forging identity from `model_requested`. That is recoverability, not a
doctor-green ritual.

## Live verification lessons (synthetic examples)

These patterns are worth preserving as timeless protocol guidance:

- **Grok health gate**: a governed Grok route requires exit `0` *and* an attributed
  native session whose model IDs match the request with zero session errors. Missing
  attribution journals as `provider-health-unverified` and fails closed — exit `0`
  alone is not sufficient.
- **Auxiliary stderr vs main turn**: failure classification follows the main process
  exit/result, not an isolated stderr substring from an auxiliary request in the
  same CLI invocation.
- **Cursor broker attribution**: the wrapper correlates JSONL + SQLite pairs into
  one session rather than guessing by newest mtime. Concurrent sessions can yield
  `ambiguous-concurrent-artifacts`; model matching cannot prove ownership.
- **Headless workspace trust**: Cursor read-only smokes fail fast without explicit
  `--trust-workspace` when the provider is selected explicitly. For a governed
  `run auto --task-shape ...` route, `agent-run` derives the same native trust
  decision from the fixed provider manifest and compiled route binding; an
  orchestrator adapter neither passes nor overrides that flag. The journal
  records whether trust came from `explicit-cli` or `governed-route-binding`.
- **Spending-limit signals**: HTTP 402 and similar spending-limit classes are
  transient live states, not evidence that the CLI never worked and not an
  authentication failure. The portable manifest remains capability configuration;
  callers must distinguish `discover` (installed/config-enabled) from a live run
  that hit a spending limit and fail closed until the limit clears.
- **Cross-family review hardening**: the layer ships a shared pure
  ledger-history validator, an explicit `codex_final_review` cross-family route for
  Claude-family producers, and a normalized reciprocal `claude_final_review`
  governance tier for Codex-produced risk work. The wrapper prevents explicit-provider
  route spoofing, binds reviewers to a successful producer in the same repository,
  validates the full ledger schema, normalizes governance effort, and requires
  primary-session evidence for governed Grok runs.
