# Local Reference Repository Audit: Agent Run Orchestration V1

Date: 2026-07-18

Scope: local, read-only source audit of four upstream repositories plus a
three-model architecture reconciliation. No upstream package was installed or
executed, no credential or provider configuration was changed, and no upstream
runtime was added to Agent Run.

## 1. Pinned local snapshots

The repositories are shallow reference clones under
`$HOME/Projects/agent-orchestration-reference-repos/`. They are not a
new GitHub repository, not a runtime dependency, and not a routing, ledger, or
memory truth source.

| Repository | Local commit | Commit date | License evidence | Decision |
|---|---|---|---|---|
| `smtg-ai/claude-squad` | `5a604f76fc943d29fbc1ee76ec33b4ebd03178e3` | 2026-06-17 | root `LICENSE.md`: AGPL-3.0; SHA-256 `00352ab19865e23bb0ab7e0f45332206aba6a66d75b3f3cc962fdd6508d63ea4` | Design observation only; no code extraction or runtime adoption |
| `mem0ai/mem0` | `ddaa655edf41e3ed375b263fb227da0bcd42ccb9` | 2026-07-17 | root `LICENSE`: Apache-2.0; SHA-256 `0bbcbe931c353293a2fafce08326181dfeea0e568c566afd4ce8337a70f5e219` | Reject from V1 runtime; future memory work requires a separate proposal |
| `fynnfluegge/agtx` | `9580c47c51ba2f99e087f79bc337eafde4ad3d23` | 2026-07-18 | root `LICENSE`: Apache-2.0, SHA-256 `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4`; `Cargo.toml`: MIT, SHA-256 `4c3eecb59535bd026272c6fc76a6d1c8770aaf4b7995d95d0d082a13c9043bde` | Patterns only until the conflicting declarations are resolved |
| `farion1231/cc-switch` | `997be22bfa5d14161a6f5b1f805631054368cdb0` | 2026-07-17 | root `LICENSE`: MIT; SHA-256 `912b6a597d10c43b40a0909349ed95b052b17efb6502b4898e1b35dafb896755` | Human-operated host tool only; orchestrator must never invoke its switching/proxy path |

## 2. Findings by repository

### 2.1 Claude Squad

Claude Squad is a parallel interactive session launcher, not a task
orchestrator. Each instance owns a tmux session and a Git worktree. Instance
storage records branch, worktree path, base SHA, program, and diff statistics
in a JSON snapshot (`session/storage.go:10-42,56-93`). New instances create a
worktree and then start tmux (`session/instance.go:217-273`); pause may commit a
dirty worktree, remove the worktree, and preserve the branch before resume
recreates it (`session/instance.go:411-551`).

Useful independent patterns:

- record the frozen base SHA and worktree identity;
- check that a branch is not already checked out before resume;
- separate process teardown from worktree teardown and report both errors.

Rejected patterns:

- no DAG, dependency admission, structured result envelope, deterministic join,
  or independent reviewer contract;
- setup and cleanup can force-remove a worktree, `RemoveAll` a path, and
  `branch -D` a same-name branch (`session/git/worktree_ops.go:38-78,103-155`);
- pause creates an agent-derived commit, conflicting with controller-only
  commit ownership (`session/instance.go:449-461`);
- trust/permission prompts may be answered through terminal key injection;
- AGPL-3.0 prevents copying implementation into the current repository without
  changing its licensing obligations.

### 2.2 Mem0

Mem0 is a semantic memory SDK, not a process or task control plane. A memory
operation must be scoped by at least one of `user_id`, `agent_id`, or `run_id`
(`mem0/memory/main.py:287-370`). By default, `add(..., infer=True)` uses an LLM
to extract facts and decide whether to add, update, or delete memories
(`mem0/memory/main.py:721-833,872-925`). Search requires a caller-supplied scope
filter (`mem0/memory/main.py:1335-1417`).

Useful independent patterns:

- require an explicit scope for every lookup;
- retain an append-style history of ADD/UPDATE/DELETE events;
- use a broad sensitive-field redaction test corpus.

Rejected V1 patterns:

- it would add another LLM/embedding/provider path and mutable vector-memory
  truth alongside the routing canon, run journal, ledger, prompt system, and
  existing project memory;
- update/delete operate by memory ID rather than by an immutable task authority
  contract (`mem0/memory/main.py:1771-1842`);
- OSS telemetry defaults on and sends lifecycle/runtime metadata to PostHog
  (`mem0/memory/telemetry.py:14-20,50-73,76-118,192-220`);
- semantic retrieval adds extraction and embedding latency and therefore is not
  a demonstrated cure for repeated coding-agent context reads.

Any future memory sidecar is non-V1. It requires a separate plan gate, local
storage/embedding, telemetry disabled, no ledger or scheduler writes, and a
frozen snapshot shared by all benchmark arms if it is ever evaluated.

### 2.3 agtx

agtx is the closest of the four to a task control surface. It stores tasks,
statuses, sessions, worktrees, agents, transitions, and notifications in
SQLite. MCP calls enqueue transition requests and a single TUI executor claims
and performs side effects (`src/db/schema.rs:525-601`). Tasks can reference
dependencies, and the UI exposes task-to-worktree-to-tmux lifecycle and a
notification path.

Useful independent patterns:

- separate command enqueue from the single side-effect executor;
- use an atomic conditional claim instead of allowing an agent to mutate
  runtime state directly;
- store task/session/worktree identity together for reconciliation;
- hash explicitly trusted project configuration before executing it;
- render dependency state as a read-only diagnostic.

Rejected runtime patterns:

- its dependency check can treat a missing referenced task as satisfied,
  whereas V1 must fail closed (`src/db/schema.rs:434-446`);
- native commands hard-code permission expansion such as Claude
  `--dangerously-skip-permissions`, Copilot `--allow-all-tools`, Gemini yolo,
  and Cursor `--yolo` (`src/agent/mod.rs:36-74`);
- worktree creation may remove partial directories and force-delete a same-name
  branch; cleanup uses force removal (`src/git/worktree.rs:36-75,329-350`);
- worktree initialization copies `.claude`, `.codex`, and other agent config
  directories and can run an arbitrary `sh -c` init script while treating
  failures as warnings (`src/git/worktree.rs:97-158,253-276`);
- its SQLite board/transition queue would become a second task truth, and its
  pane-read/message-injection orchestrator is not a deterministic join;
- the root Apache-2.0 license and `Cargo.toml` MIT declaration conflict, so V1
  uses patterns only and extracts no code.

### 2.4 cc-switch

cc-switch is a desktop provider/profile/proxy manager. It intentionally treats
its database as provider truth and projects that state into live Claude,
Codex, Gemini, and other CLI configuration. Its per-application switch lock is
an in-process Tokio mutex (`src-tauri/src/proxy/switch_lock.rs:1-41`), not a
cross-process lock against Agent Run or another CLI.

Useful independent patterns:

- temporary-file, flush, and rename for small non-append state files;
- snapshot-before-change and rollback after a multi-file apply failure;
- make provider switching explicit and serialize it per application.

Rejected V1 patterns:

- direct writes to `~/.codex`, `~/.claude`, `~/.gemini`, authentication files,
  model catalogs, endpoints, and proxy placeholders;
- proxy takeover, account rotation, failover, and credential copying inside the
  orchestration path;
- a second provider/profile database that can silently change the model behind
  an unchanged CLI command;
- in-process locking as a substitute for controller and provider-family
  cross-process fencing.

The orchestrator may observe a redacted configuration fingerprint for
benchmark fairness. It must never invoke cc-switch, enable its proxy, change an
account, or write live provider configuration.

## 3. Three-model reconciliation

The three independent positions were Codex local source analysis, Claude Fable
5 Max, and Cursor Grok 4.5 High Fast. Fable and Grok were run through governed
`agent-run` read-only calls. Accepted receipt identifiers remain in host-local
evidence; the Git artifact retains only the audit stages and elapsed times:

- Fable independent audit: exit 0, 646,679 ms;
- Grok independent audit: exit 0, 89,290 ms;
- Grok plan-delta rebuttal: exit 0, 86,869 ms;
- Fable cross-discussion: exit 0, 507,321 ms;
- Grok final attributed retry: exit 0, 59,486 ms.

One earlier Fable attempt was quota-blocked before analysis, and one Grok final
attempt produced an ambiguous session receipt. Neither result was accepted as
model evidence; both were retried with unique attribution.

The reconciled result is:

1. Phase 0 remains GO and the thin native Agent Run architecture remains the
   preferred V1.
2. Phase 1 schema cannot freeze until controller lease/fencing, stable attempt
   identity, resource ownership, permission non-override, and optional redacted
   config-fingerprint fields are written into the contracts.
3. Phase 2 must prove one controller per run, stable attempts, single-writer
   events, and write-ahead resource ownership before dispatch.
4. Phase 4 must reconcile worktree identity before resume and permit cleanup
   only when manifest ownership, current fencing token, and an allowed absolute
   path all agree. Unknown resources are reported and preserved.
5. Worktrees may naturally contain tracked repository-local agent files from
   the frozen commit. The controller must not copy host-level agent config,
   untracked environment files, tokens, user MCP state, or run arbitrary init
   hooks into them.
6. Permission and sandbox behavior are projections of the routing canon and
   Agent Run mode. Adapters cannot add yolo/bypass flags.
7. Phase 5/6 must freeze a credential-stripped provider configuration
   fingerprint per paired block. Drift makes the whole block
   `protocol-invalid`; daily orchestration is not gated by this fingerprint.
8. The benchmark must measure context construction time. Deduplicated shared
   context bytes and provider cache-hit evidence are preregistered secondary
   diagnostics without a cross-provider threshold.
9. None of the four repositories becomes a runtime, task queue, ledger, memory
   bus, provider router, or UI dependency in V1.

## 4. Effect on the seven-phase plan

The phase order, repository ownership, native CLI bridge, deterministic DAG,
writer isolation, mechanical join, independent review, and A/B/C benchmark do
not change. The audit adds contract and safety gates rather than a new runtime:

| Phase | Added or clarified requirement |
|---|---|
| Phase 0 | Existing worktree path/branch collision and checked-out-branch cases stop closed; linked-worktree identity/exclude behavior is tested |
| Phase 1 | Freeze controller/attempt/fencing/resource fields, permission non-override, shared-interface/integrated-acceptance fields, and fixed reference-spike evidence |
| Phase 2 | Cross-process controller lease, stable attempt ID, single-writer journal, graceful cancel ETA, write-ahead resource manifest |
| Phase 3 | Native adapter argv cannot expand authority; context construction and delivered prompt size are timed |
| Phase 4 | Tracked-only baseline, no host config copy/init hook, deterministic integration acceptance, ownership-gated cleanup, full resume reconciliation |
| Phase 5 | Freeze reviewer binding, quota/headroom rules, config fingerprint protocol, first-pass/rework definitions, and context diagnostics |
| Phase 6 | Preserve paired fairness; config drift invalidates a whole block; context metrics remain diagnostic and do not change H1/H2 after results are visible |

This audit does not prove that automatic multi-agent orchestration is faster or
better. Only the preregistered live three-arm benchmark can establish that.

## 5. Normative adoption disposition

The execution plan now contains a stable, non-executable reference-pattern
matrix in Section 5.3. This audit remains the upstream source-evidence record;
the matrix is only the source-to-local-contract-to-fixture traceability index.
Neither document is scheduler input, a route canon, a task queue, or runtime
state.

The current matrix contains sixteen decisions:

- nine `contract_promoted` patterns: frozen worktree identity, separate
  teardown outcomes, explicit run/attempt resource scope, append-only/redacted
  history, per-run atomic controller claim, cross-schema identity
  reconciliation, controller-owned atomic manifests, distinct cross-process
  controller/provider locks, and benchmark-only provider fingerprints;
- three `deferred` patterns: tmux/session UI, semantic memory, and additional
  project-config trust stores or host overlays;
- four `forbidden` decisions: Claude Squad destructive/auto-authority behavior,
  a separate Claude Squad AGPL provenance/code-reuse prohibition, agtx
  runtime/permission/second-queue behavior, and cc-switch live provider or
  credential mutation.

All sixteen decisions currently have `implementation_status=planned` and
`verification_status=not_run`. Their presence in the plan is not evidence that
the schemas, modules, or fixtures already exist. Phase 1 cannot freeze until
each promoted contract and its Phase-1-owned schema/static fixtures have landed
with positive and fail-closed coverage, each later behavioral fixture is named
as a gate of its owning phase, each deferred row still has zero V1 dependency,
and each forbidden row is enforced at its owning seam.

## 6. Phase-1 reference spike / adopt gate (2026-07-18)

This is the fixed, time-boxed source study required by Phase 1.  The five
repositories below were read from their shallow clones only.  No package,
daemon, service, test suite, or example was installed or executed; no upstream
code was copied; and no production, host, credential, or routing configuration
was changed. `not measured` below means precisely that, rather than an inferred
pass.

### 6.1 Evidence manifests

Snapshot date: **2026-07-18**.  `pushed_at` and GitHub license are the primary
GitHub API values read on that date; the canonical remote and commit timestamp
are the local shallow-clone observations. The GitHub URLs are the primary
verification URLs; local `file:line` citations below are pinned to the stated
snapshot, not a claim about an un-fetched upstream tip.

| ID | Canonical repository / primary URL | Pinned commit (local commit date) | GitHub primary metadata | License declarations and SHA-256 evidence |
|---|---|---|---|---|
| `SRC-ODW` | [`Suraj1235/open-dynamic-workflows`](https://github.com/Suraj1235/open-dynamic-workflows) | `972bb98494ea23f907df88850024bd7022b099d4` (2026-07-09T22:02:53+05:30) | default `main`; `pushed_at=2026-07-09T16:35:47Z`; GitHub `MIT`; not archived/disabled | root `LICENSE:1` says MIT; SHA-256 `64025de67cf0d7d680624a8d52790cdfbc54a52a402406984599bf0f7795b313` |
| `SRC-AGENTAPI` | [`coder/agentapi`](https://github.com/coder/agentapi) | `9ff117e231822f670305254ef24f6389f75953f4` (2026-05-27T20:35:55+05:30) | default `main`; `pushed_at=2026-05-27T15:06:20Z`; GitHub `MIT`; not archived/disabled | root `LICENSE:1-7` is the MIT grant (no SPDX string in that file); SHA-256 `6a11e5fb1fdbffeb88c33f160214a034a7198fed8aedc28ba709a299413d64bf` |
| `SRC-AO` | [`AgentWrapper/agent-orchestrator`](https://github.com/AgentWrapper/agent-orchestrator) | `9eb587fb3c97f7e49edc16d97a24f79a3be4be18` (2026-07-18T11:54:11+05:30) | default `main`; `pushed_at=2026-07-18T14:00:51Z`; GitHub `Apache-2.0`; not archived/disabled | root `LICENSE:1` and [`README.md:193`](https://github.com/AgentWrapper/agent-orchestrator/blob/9eb587fb3c97f7e49edc16d97a24f79a3be4be18/README.md#L193) declare Apache-2.0; SHA-256 `1a2219722b7ef58364065e9073a2cb2831891eb147a785742a31431c9cddad1d` |
| `SRC-AGTX` | [`fynnfluegge/agtx`](https://github.com/fynnfluegge/agtx) | `9580c47c51ba2f99e087f79bc337eafde4ad3d23` (2026-07-18T10:30:33+02:00) | default `main`; `pushed_at=2026-07-18T08:30:34Z`; GitHub `Apache-2.0`; not archived/disabled | **conflict:** root `LICENSE:1` Apache-2.0, SHA-256 `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4`; [`Cargo.toml:7`](https://github.com/fynnfluegge/agtx/blob/9580c47c51ba2f99e087f79bc337eafde4ad3d23/Cargo.toml#L7) says MIT, SHA-256 `4c3eecb59535bd026272c6fc76a6d1c8770aaf4b7995d95d0d082a13c9043bde` |
| `SRC-HARBOR` | [`harbor-framework/harbor`](https://github.com/harbor-framework/harbor) | `678bbb6d60985c1d172b845f30572ce73af65192` (2026-07-17T20:50:13-07:00) | default `main`; `pushed_at=2026-07-18T03:50:14Z`; GitHub `Apache-2.0`; not archived/disabled | root `LICENSE:1` Apache-2.0, SHA-256 `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4`; [`pyproject.toml:6-7`](https://github.com/harbor-framework/harbor/blob/678bbb6d60985c1d172b845f30572ce73af65192/pyproject.toml#L6-L7) Apache-2.0, SHA-256 `65b38a57ea090d4d3f4d6a9ad677118b5b8222b136887bf2ee24af0fe69a713b` |

The canonical remotes exactly matched the five repository URLs above.  Redirect
resolution was therefore not needed. GitHub metadata is evidence of repository
identity and activity only; it does not establish runtime fitness.

### 6.2 Time-boxed source findings

| Candidate | Narrow pattern observed | Local primary evidence | V1 disposition |
|---|---|---|---|
| ODW checkpoint | Guest `checkpoint(data)` hands arbitrary data to a host bridge. The daemon persists a random checkpoint ID, workflow/phase/key and JSON state, then emits a checkpoint event. It is a daemon-owned SQLite snapshot, not an append-only controller-fenced checkpoint ledger. | [`guest-prelude.js:255-257`](https://github.com/Suraj1235/open-dynamic-workflows/blob/972bb98494ea23f907df88850024bd7022b099d4/packages/daemon/src/guest-prelude.js#L255-L257); [`runtime.js:256-266`](https://github.com/Suraj1235/open-dynamic-workflows/blob/972bb98494ea23f907df88850024bd7022b099d4/packages/daemon/src/runtime.js#L256-L266) | Keep only the idea that phase-labelled snapshots are explicit. Do not adopt its daemon, provider HTTP fan-out, script-defined control plane, SQLite state, or checkpoint format. |
| AgentAPI adapter | AgentAPI is an HTTP/OpenAPI and SSE wrapper around a persistent terminal/ACP conversation. A `raw` message writes terminal keystrokes and is not journaled. | [`README.md:60-87`](https://github.com/coder/agentapi/blob/9ff117e231822f670305254ef24f6389f75953f4/README.md#L60-L87); [`models.go:44-82`](https://github.com/coder/agentapi/blob/9ff117e231822f670305254ef24f6389f75953f4/lib/httpapi/models.go#L44-L82) | Useful contrast for a future bounded transport study only. It adds a server, mutable conversation state, and terminal-keystroke injection; it cannot replace the native receipt bridge or ledger. |
| Agent Orchestrator worktree | Its workspace port distinguishes safe destroy, preservation, and force destroy. Shutdown captures uncommitted state and commits its restore marker before force removal. | [`outbound.go:122-144`](https://github.com/AgentWrapper/agent-orchestrator/blob/9eb587fb3c97f7e49edc16d97a24f79a3be4be18/backend/internal/ports/outbound.go#L122-L144); [`manager.go:933-975`](https://github.com/AgentWrapper/agent-orchestrator/blob/9eb587fb3c97f7e49edc16d97a24f79a3be4be18/backend/internal/session_manager/manager.go#L933-L975) | Promote only the invariant “record durable ownership/preservation before destructive cleanup.” Its long-running daemon, own state, session model, and `ForceDestroy` capability are not V1 dependencies. |
| agtx claim/config hash | The SQLite conditional update is an actual atomic single winner claim. Its trust store hashes a project `.agtx/config.toml` and suppresses configured dangerous fields on mismatch. | [`schema.rs:581-590`](https://github.com/fynnfluegge/agtx/blob/9580c47c51ba2f99e087f79bc337eafde4ad3d23/src/db/schema.rs#L581-L590); [`config/mod.rs:649-718`](https://github.com/fynnfluegge/agtx/blob/9580c47c51ba2f99e087f79bc337eafde4ad3d23/src/config/mod.rs#L649-L718) | Promote the abstract conditional-claim and redacted/config-fingerprint concepts into local contracts only. Do not adopt its queue, SQLite board, user trust store, configuration handling, or code while the license declarations conflict. |
| Harbor task definition | A task directory binds `instruction.md`, `task.toml`, environment, solution and tests. `TaskConfig` has versioned task, verifier, agent, environment, solution, steps and artifact fields, parsed from TOML. | [`paths.py:11-30`](https://github.com/harbor-framework/harbor/blob/678bbb6d60985c1d172b845f30572ce73af65192/src/harbor/models/task/paths.py#L11-L30); [`config.py:790-901`](https://github.com/harbor-framework/harbor/blob/678bbb6d60985c1d172b845f30572ce73af65192/src/harbor/models/task/config.py#L790-L901) | Use only as a fixture-design reference: explicit inputs, environment, verifier and artifacts. Do not adopt Harbor's evaluation framework, dependency graph, sandbox/environment runtime, or task schema as the scheduler input. |

### 6.3 Hard-constraint scorecard

Legend: `pass` means the source study demonstrated the property at its boundary;
`fail` means it conflicts with the Phase-1 requirement; `not measured` is not a
pass. A candidate needs every cell to be `pass` before the 30% / 10% comparison
is even considered.

| Candidate | Native subscription CLI / sessions | Existing routing canon | Run/model/session receipts | Checkpoint / ledger continuity | Isolated worktrees | License review | No unapproved global config, credential copy, telemetry | Offline deterministic testability | Result |
|---|---|---|---|---|---|---|---|---|---|
| ODW | fail — direct provider/daemon control plane | fail | fail | fail | fail | pass | fail — daemon/integration control plane; telemetry behavior not independently executed | not measured | **hard fail** |
| AgentAPI | partial/fail — invokes native CLIs but mediates a persistent HTTP/PTy/ACP server | fail | fail — conversation messages are not governed receipts | fail | fail | pass | fail — server plus terminal-keystroke path; runtime configuration not executed | not measured | **hard fail** |
| Agent Orchestrator | fail — its daemon/session runtime owns execution | fail | fail | fail | pass | pass | fail — long-running daemon and `~/.ao` state | not measured | **hard fail** |
| agtx | fail — its own agent/queue control plane | fail | fail | fail | partial/fail — it owns worktree lifecycle | **fail** — Apache/MIT declaration conflict unresolved | fail — own trust/config/state layer and authority-expanding agent behavior already recorded in §2.3 | not measured | **hard fail** |
| Harbor | fail — evaluation/sandbox framework, not the governed native CLI bridge | fail | fail | fail | fail — task sandboxing is not Git worktree isolation | pass | not measured | not measured | **hard fail** |

### 6.4 Build-versus-adopt scorecard and mock-dispatch method

| Candidate | Integration + maintenance surface versus thin native bridge | Added daemon/state ownership and process safety | Recovery value | Reversible/removable deployment | Mock dispatch overhead result | Build vs adopt conclusion |
|---|---|---|---|---|---|---|
| ODW | substantially larger: daemon, sandbox, SQLite, provider adapters and workflow language | new long-running process plus second durable state | crash resume, but for its own workflow state | removal would require state/process migration | **not measured** | build thin native bridge |
| AgentAPI | larger: HTTP/OpenAPI/SSE server, PTY/ACP conversation formatting | persistent server and terminal injection boundary | conversation persistence, not controller-fenced recovery | server/session state must be retired | **not measured** | build thin native bridge |
| Agent Orchestrator | substantially larger: daemon, DB, lifecycle, UI and worktree manager | owns cleanup and session recovery; force cleanup capability | useful preservation pattern only | own state/worktree lifecycle to retire | **not measured** | build thin native bridge |
| agtx | larger: SQLite board, TUI, MCP transition queue and trust store | second task truth and config-trust authority | conditional claim pattern only | own DB/config state to retire | **not measured** | build thin native bridge |
| Harbor | larger: task/evaluation and sandbox environment framework | environment/runtime ownership outside the local Git orchestration boundary | evaluation artifacts, not scheduler recovery | framework and task convention removal | **not measured** | build thin native bridge |

The preregistered mock method, if a later candidate clears all hard constraints,
is: run the same fixed fake-adapter plan with (A) the native bridge and (B) a
candidate adapter, after a warm-up; collect monotonic timestamps at dispatch
intent, claim, adapter call start, fake-adapter accepted receipt, and terminal
receipt; calculate per-attempt `dispatch_overhead = accepted_receipt -
dispatch_intent`; compare median B/A across the fixed trial set and report
`(median_B / median_A - 1) * 100`. Record process count, extra durable state,
and all exclusions. The candidate passes this sub-gate only at `<= 10%` added
overhead. This read-only source spike intentionally did not run that method, so
there is no numeric overhead result and no claimed performance reduction.

### 6.5 Gate decision

**Do not trigger a user adoption decision.** All five candidates fail at least
one mandatory hard constraint, and none has a measured mock-dispatch result.
Consequently none can establish the required `>=30%` lower estimated integration
plus maintenance surface while adding `<=10%` measured dispatch overhead. The
Phase-1 plan therefore continues with the thin native bridge and local contract
fixtures; the observations above are pattern references only, never runtime
dependencies or authorization to adopt code.
