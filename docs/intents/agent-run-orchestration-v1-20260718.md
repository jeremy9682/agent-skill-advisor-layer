# Intent and Execution Plan: Agent Run Orchestration V1

Status: implemented locally and evidence-gated on 2026-07-18. The sections
below preserve the approved design, constraints, and benchmark protocol; they
are not a claim that the live value experiment has passed. This document does
not authorize push, merge, release, production deployment, provider billing
changes, or an unbounded live benchmark.

## 1. Decision summary

1. Build V1 in the existing `agent-skill-advisor-layer` repository. Keep all
   credentials, sessions, ledgers, journals, locks, generated worktrees, and
   transient artifacts host-local and outside Git.
2. Preserve Claude Code, Codex, Cursor Agent, and Grok as native coding-agent
   harnesses. The orchestrator calls the existing `agent-run` CLI and consumes
   its observed receipts; it does not replace native sessions with raw model
   API calls.
3. Make concurrency an explicit routing-canon decision. Absence of a
   `serial_group` must never silently mean that a new route is parallel-safe.
4. Start with an explicit, deterministic task graph, a total concurrency limit
   of three, one writer per workspace, worktree isolation, structured result
   envelopes, and a mechanical join before any AI synthesis or final review.
   Global writer admission starts at one and may rise to a hard maximum of two
   only after Phase 4 proves isolated, non-overlapping scopes; merge/join stays
   serial.
5. Separate two proof questions:
   - **P1 capability:** can the control plane schedule, recover, attribute, and
     join work correctly?
   - **P2 value:** does it improve accepted-result latency or quality compared
     with one native agent and today's manual `agent-run` fan-out?
6. Do not make multi-agent fan-out the daily default until the preregistered
   benchmark passes. A negative result keeps useful resume/isolation/join
   capabilities but leaves production work single-agent by default.
7. Use open-source projects as design references and isolated spikes, not as a
   second routing or evidence truth source. Harbor is the leading candidate for
   a later evaluation adapter; no external orchestrator enters the production
   path in V1.

## 2. Current implementation and evidence boundary

The planning worktree is
`$HOME/Projects/.worktrees/agent-run-orchestration-v1`, branch
`feat/agent-run-orchestration-v1`, based on commit `3f1de25`.

V1 now includes the versioned plan/result/event contracts, deterministic DAG
validation, bounded ready-set scheduler, `agent-run` bridge, isolated writer
worktrees, controller-created candidate commits, deterministic join,
dependency bundles, controller lease/fencing, conservative resume
reconciliation, and ownership-checked cleanup. It keeps native CLI sessions as
the provider surface and keeps host-local journals, worktrees, credentials, and
runtime evidence outside Git.

The two explicitly parallel Cursor mechanical routes disable managed skill
injection for the orchestration hot path. `agent-run doctor` reports both
`mechanical` and `mechanical_grok` as `ready`; `serial-lock-disabled` is the
intentional warning for those routes, not a blocker. Cursor native stream JSON
is the primary identity receipt. Exact model attribution accepts an exact,
unique catalog-label mapping only; ambiguous identity remains fail closed.

The review contract is stricter than provider exit status: invocation and
session success are necessary but not approval. The final non-empty line must
be the one unique exact marker `AGENT_RUN_REVIEW_VERDICT: PASS`; `FAIL`,
absence, trailing text, or ambiguity blocks acceptance and cleanup.

Canary history is evidence of this distinction. V1 exposed concurrent
attribution, automatic-skill prompt bloat, and cleanup failures. V2 and V3
completed their provider calls and were incorrectly marked completed by the
old exit-code-only review path even though both stored Claude verdicts were
`FAIL`. That defect drove the fail-closed verdict gate; neither run is accepted
as a successful canary under the corrected contract. V4 first passed the full
controlled path. Fable 5 Max then independently returned `PASS` with no P0/P1
and six fail-closed P2 finding groups; all six were remediated before the final
canary. A fresh incremental Fable pass confirmed those closures and found two
remaining P2 validator gaps; wrapped interpreters and inert read-only/no-writer
acceptance declarations are now rejected as well. A final fresh Fable closure
review recorded in host-local evidence returned `PASS` with no P0/P1/P2. Its
remaining P3 notes clarify that
the interpreter lint is not a universal executable sandbox and that safe
over-rejection is possible; shell-free argv plus frozen-plan, acceptance and
review gates remain authoritative. V5 used a tracked argv-native verifier:
Composer ran for 19.898 s and Cursor Grok ran for 23.248 s. They launched 0.194
s apart, with 25.555 s producer wall time versus 43.146 s serial provider time
(about 1.69x in this canary). Integration then passed and Claude Opus
`claude-opus-4-8` reviewed the exact acceptance argv and verifier semantics
with machine `PASS` in 50.983 s from a 1,410-byte delivered prompt. Only then
was eligible owned-resource cleanup performed.

This establishes capability and a small, controlled latency observation; it
does not establish general quality or total-workflow superiority. The local
synthetic pilot (9 cells) and confirmation (36 cells) prove the benchmark
harness only. The live preflight stopped with exit 3 before its first cell
because `whole-block-headroom-unknown` and missing authentication/host proof
failed closed. It produced no live benchmark output. Daily-default changes
therefore remain blocked pending a properly evidenced, preregistered live
benchmark. Evidence summaries remain local under `~/.agent-runs/`.

## 3. Independent model reviews and reconciliation

### 3.1 Our judgment before external reconciliation

- The current problem is not inability to launch processes concurrently. It is
  the missing control layer around dependency readiness, bounded admission,
  worktree ownership, recoverability, structured collection, and deterministic
  acceptance.
- The hot path must stay thin. Replacing native coding agents with a generic
  framework would discard their repository tools, session semantics, skills,
  approvals, compaction, and existing attribution evidence.
- Multi-agent work should be selected only for genuinely separable tasks. A
  single deep bug in one module is a negative-control workload, not a swarm
  candidate.

### 3.2 Cursor Grok 4.5 High Fast review

Grok independently recommended V1 in the current repository with staged
extraction later. It required a distinction between “the scheduler works” and
“multi-agent work helps,” an explicit required-lock canon test, a thin bridge
to existing `agent-run`, fake-adapter tests before live providers, deterministic
quality measurement, and a three-arm benchmark.

### 3.3 Claude Fable 5 Max review

Fable independently found two implementation-level gaps not explicit in the
first draft: worktree repo-slug drift breaks checkpoint/reviewer evidence, and
the current doctor lacks a runtime fail-closed rule for routes that should be
serialized. It also required plan-writing time to be measured, a negative
control task class, per-family admission limits, and an explicit statement of
what survives if multi-producer fan-out fails the benchmark.

Its 12.6-minute wall time is itself routing evidence: Fable Max is suitable for
high-value architecture or final gates, not as a mandatory member of every
daily parallel graph.

### 3.4 Reconciled position

- Keep V1 in the current repository, but expose a narrow CLI and versioned
  schemas so extraction remains possible.
- Use one thin public entry point and an internal orchestration package rather
  than extending `agent_provider_run.py` or creating another monolith.
- Remove the optional mailbox from V1. Result envelopes and artifact pointers
  flow through the lead/controller; free-form agent-to-agent chat is not shared
  truth.
- Resolve concurrency and worktree slug semantics in Phase 0, before writing a
  scheduler.
- Measure plan time, queue time, provider time, join time, rework, human
  coordination time, and agent-minutes separately.

### 3.5 Four-repository local source audit and second reconciliation

Fresh, pinned local source audits of Claude Squad, Mem0, agtx, and cc-switch
confirmed the V1 architecture but exposed contract gaps that were not yet
written into this plan. Codex, Claude Fable 5 Max, and Cursor Grok 4.5 High Fast
then reconciled those gaps. The durable evidence and accepted run/session
receipts are in `docs/reference-repo-audit-20260718.md`.

The new consensus is:

- a wrapper PID and start fingerprint do not prevent two controllers from
  concurrently starting or resuming the same run; Phase 1 must freeze a
  controller lease, generation/fencing token, and stable attempt identity;
- every generated worktree, branch, and integration resource requires a
  write-ahead ownership manifest. Automatic cleanup requires matching ownership,
  a current fencing token, and an allowed absolute path; unknown resources are
  preserved and reported;
- native adapter permissions are derived from the routing canon and `agent-run`
  mode. An adapter may never add yolo, skip-permission, trust, or equivalent
  bypass flags;
- tracked repository-local agent files naturally present at the frozen commit
  are allowed. Copying host-level agent config, tokens, user MCP state,
  untracked environment files, or arbitrary init hooks into a worktree is not;
- cc-switch remains a human-operated host tool. The orchestrator never invokes
  provider switching, proxy takeover, account rotation, or live-config writes;
- Mem0 is not a V1 task, ledger, or memory runtime. Any future semantic-memory
  sidecar requires a separate plan and a frozen snapshot in comparative tests;
- the paired benchmark must measure context-construction overhead and detect
  credential-stripped provider-config drift, because those are direct threats
  to the original latency question and experimental fairness.

## 4. Local system boundaries and repository ownership

The existing local projects have different responsibilities and should not be
merged into one repository merely because all involve AI agents:

| Project/system | Current responsibility | V1 relationship |
|---|---|---|
| `agent-skill-advisor-layer` | Routing canon, provider runner, governance, schemas, QA | Owns V1 contracts and implementation |
| `agent-sessions-grok` | Session history, discovery, resume UX | Read-only consumer later; not a message bus |
| `Codex-Orchestration` | Role-routing/plugin reference | Pattern reference only |
| `gstack`, `huashu-skills`, external skill repos | Skills and development workflows | Remain independent inputs selected by routing |
| prompt-management/distillation assets | Prompt source and governance | Remain separate; orchestration stores hashes/pointers only |
| `overnight-engine` | OS scheduling and long-running launch policy | May invoke the orchestrator later; does not own its DAG |
| `project-butler` | Human/session handoff memory | Does not become runtime task state |
| `~/.agent-ledger` | Cross-seat checkpoint governance | Remains a ledger, never a task queue |
| `~/.agent-runs/orchestration/` | Current local orchestration events | Private JSONL runtime truth for orchestration runs |

V1 therefore does **not** create a second GitHub repository. A future private
`local-agent-control-plane` extraction begins only when at least one of these
triggers is met:

1. A second real project needs to import the orchestration library rather than
   call its CLI.
2. Host-specific or private runtime policy must be versioned, which cannot live
   in the public governance repository.
3. Orchestration code plus focused tests exceeds roughly 5,000 lines and begins
   to obscure the repository's governance purpose.
4. Two or more orchestration changes in 30 days block skill-governance releases,
   or the two areas require independent release cadences.
5. Publication/security review rejects shipping a local process controller in
   the public repository.

Extraction freezes the CLI/schema boundary first. The routing canon remains in
one place; the new repository consumes it and never forks a second copy.

**Current trigger state:** `scripts/orchestration/*.py` is now approximately
8,823 lines, so trigger 3 has been crossed. This V1 change remains in the
current repository to finish evidence-gated validation; it does not authorize a
move or creation of a repository. Before the next material orchestration
change, an extraction-plan gate is required. That gate must preserve
`routing-policy.yaml` as the single routing canon and prevent the module from
continuing to grow without an explicit repository-boundary decision.

## 5. Open-source landscape and adoption decision

Snapshot date: 2026-07-18. Technical claims below were checked against official
repositories; Reddit and X observations are used only as community experience
signals. Stars are intentionally omitted from the architecture decision because
they drift and do not prove integration fitness. Before any spike, Phase 1 must
record the canonical owner/repository after redirects, pinned commit, license
file and digest, `pushed_at`, snapshot time, and verification URLs.

| Project | Relevant capability | Decision for this system |
|---|---|---|
| [Open Dynamic Workflows](https://github.com/Suraj1235/open-dynamic-workflows) | Script-as-orchestrator primitives, parallel/pipeline/loop/verify/checkpoint, SQLite resume, critic quorum; MIT | Borrow execution semantics; do not use as production runtime |
| [Agent Orchestrator](https://github.com/AgentWrapper/agent-orchestrator) | Native terminal-agent supervision, one worktree per session, CI/review/conflict feedback; official `ComposioHQ` URL currently redirects to `AgentWrapper`; Apache-2.0 at snapshot | Leading full-product comparison; source-study and isolated POC only |
| [Gas Town](https://github.com/gastownhall/gastown) | Multiple native CLIs, durable Beads tasks, worktrees, handoff, watchdog and merge roles; MIT | Borrow lifecycle/recovery ideas; avoid its second ledger and daemon stack |
| [agtx](https://github.com/fynnfluegge/agtx) | Claude/Codex/Gemini/OpenCode/Cursor phases, tmux, worktrees, board, experimental orchestrator; root `LICENSE` says Apache-2.0 while `Cargo.toml` says MIT at the pinned snapshot | Promote atomic-claim semantics only; the frozen tracked commit remains V1's trust anchor, extra trust-hash/TrustStore work is deferred, and no code is extracted while license declarations conflict |
| [Paseo](https://github.com/getpaseo/paseo) | Local daemon for Claude/Codex/Copilot/OpenCode/Pi, attach/send/handoff/loop/committee, worktrees; AGPL-3.0 | Product benchmark only; no embedded reuse because of scope and license |
| [AgentAPI](https://github.com/coder/agentapi) | HTTP/SSE/OpenAPI adapter over real Claude/Codex/Cursor and other CLIs; MIT | Time-boxed adapter spike after the native receipt bridge works |
| [GitHub Agentic Workflows](https://github.com/github/gh-aw) | Durable Claude/Codex/Gemini GitHub Actions workflows with read-only defaults and safe outputs; MIT | Future CI/Issue path and safety reference, not the local hot path |
| [Harbor](https://github.com/harbor-framework/harbor) / [Terminal-Bench](https://github.com/harbor-framework/terminal-bench) | Reproducible container evaluation of Claude Code, Codex CLI, OpenHands and custom agents; Apache-2.0 | Preferred external evaluation layer after the local paired benchmark exists |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | OAuth/account/API protocol proxy and load balancing; MIT | Transport-only docs spike; not a scheduler or evidence source |
| [Mem0](https://github.com/mem0ai/mem0) | LLM-extracted semantic memory with vector search and mutable history; Apache-2.0 | No V1 runtime use; future memory work needs a separate plan, local-only storage, telemetry off, and frozen benchmark snapshots |
| [cc-switch](https://github.com/farion1231/cc-switch) | Human-facing provider/profile/proxy manager that writes live CLI configuration; MIT | Host tool only; orchestrator invocation, proxy takeover, credential/config writes, and account rotation are forbidden |
| OpenHands, OpenCode, oh-my-openagent | Alternative agent platforms/harnesses | Observe and borrow patterns; replacing native CLIs is out of scope |
| LangGraph, Microsoft Agent Framework, CrewAI | General DAG/agent-service frameworks | Too heavy for V1 and require custom adapters that hide native harness evidence |
| [Claude Squad](https://github.com/smtg-ai/claude-squad) and other worktree/session launchers | Parallel terminals and isolated branches; Claude Squad is AGPL-3.0 | Useful UX references only; no embedded code reuse, and they do not solve dependencies or deterministic join |

### 5.1 Exact decision on Open Dynamic Workflows

ODW is useful, but not for the reason its top-level integration list can imply.
Its best ideas are:

- the model writes a bounded execution plan once and leaves scheduling to code;
- explicit `parallel`, `pipeline`, `loop`, `verify`, and `checkpoint` semantics;
- crash-resume and content-addressed node identity;
- bounded concurrency and verification before accepting findings.

Its mismatch with this system is decisive: the official README states that
Codex and Cursor integrations are MCP/plugin bridges, not host-model execution.
The full engine runs through ODW's local daemon using its own API keys, an
OpenAI-compatible endpoint, or Ollama; native host-model execution is available
only where the host exposes the required sampling/plugin API. Direct adoption
would create a second provider router, state database, model attribution path,
and verification system while bypassing current `agent-run` receipts and
subscription CLI sessions.

Therefore ODW is a design reference and mock-provider spike, not the V1 base.
No `integrate all`, global config mutation, API key copy, or production daemon
installation is allowed during the spike.

### 5.2 Community evidence boundary

Community reports consistently support worktree isolation, strict scope
partitioning, deterministic tests, one writer, and independent review. They
also repeatedly report that the first failures are context duplication, manual
fan-in, port/shared-state collisions, orphan processes, merge conflicts, and a
slow tail agent. One detailed Gas Town report found two to three concurrent
agents realistic on a laptop and described orphan-daemon and cognitive-overhead
problems. These are anecdotes, not benchmark results, but they justify the V1
limits and fault-injection tests.

### 5.3 Normative reference-pattern adoption matrix

This matrix is a non-executable provenance and traceability control. It explains
why a local contract or guard exists; it is never runtime input, a route
override, a code-generation source, or a new truth source. The scheduler,
validator, route compiler, bridge, and adapters must not parse it. Runtime
authority remains with the machine routing canon, versioned orchestration
schemas, append-only event journal, observed `agent-run` receipts, Git/test/CI
facts, and the checkpoint ledger within their existing boundaries.

The classifications are deliberately narrower than `adopted`:

- `contract_promoted`: an upstream pattern has been translated into a normative
  local contract, but no upstream runtime or code has been adopted;
- `deferred`: V1 has zero dependency on the pattern and the row names the
  separate trigger required to reconsider it;
- `forbidden`: V1 must reject the behavior through static validation, a
  fail-closed fixture, or an explicit runtime guard.

For every row, `runtime_dependency=false`, `code_reuse=false`, and
`truth_source_effect=none`. `decision_status` is the classification below;
`implementation_status=planned` and `verification_status=not_run` remain true
until the owning schema/module and its fixtures actually land and pass. A row
must never be cited as proof that a capability is already implemented.

Pinned source registry:

| Source ID | Repository and commit | License evidence |
|---|---|---|
| `SRC-CS` | `smtg-ai/claude-squad@5a604f76fc943d29fbc1ee76ec33b4ebd03178e3` | AGPL-3.0, `LICENSE.md` SHA-256 `00352ab19865e23bb0ab7e0f45332206aba6a66d75b3f3cc962fdd6508d63ea4` |
| `SRC-MEM0` | `mem0ai/mem0@ddaa655edf41e3ed375b263fb227da0bcd42ccb9` | Apache-2.0, `LICENSE` SHA-256 `0bbcbe931c353293a2fafce08326181dfeea0e568c566afd4ce8337a70f5e219` |
| `SRC-AGTX` | `fynnfluegge/agtx@9580c47c51ba2f99e087f79bc337eafde4ad3d23` | root Apache-2.0 SHA-256 `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4`; `Cargo.toml` says MIT, SHA-256 `4c3eecb59535bd026272c6fc76a6d1c8770aaf4b7995d95d0d082a13c9043bde` |
| `SRC-CCSW` | `farion1231/cc-switch@997be22bfa5d14161a6f5b1f805631054368cdb0` | MIT, `LICENSE` SHA-256 `912b6a597d10c43b40a0909349ed95b052b17efb6502b4898e1b35dafb896755` |

The upstream evidence locations and full source findings are fixed in
`docs/reference-repo-audit-20260718.md`.

| Pattern ID | Source | Classification | Translated local contract or non-goal | Sole authority and enforcement seam | Required positive fixture | Required fail-closed fixture |
|---|---|---|---|---|---|---|
| `REF-CS-IDENTITY` | `SRC-CS` | `contract_promoted` | Every generated worktree records repo root, absolute path, branch, frozen base SHA, ledger slug, HEAD, and validated diff/integration identity; resume reconciles all of them before reuse. | Phases 0/1/4; plan/event/resource contracts and `scripts/orchestration/worktree.py`. | A matching linked worktree provisions and resumes without changing its identity. | Existing target, checked-out branch, missing manifest, or path/base/HEAD/slug/hash drift stops as `failed-unsafe`; nothing is reused or deleted. |
| `REF-CS-TEARDOWN` | `SRC-CS` | `contract_promoted` | Process teardown, worktree cleanup, and branch cleanup are separate outcomes; one success cannot hide another failure. | Phases 2/4; sole orchestration owner `scheduler.py:record_terminal_cleanup()` consumes runner and `worktree.py` outcomes and writes the task-result/event contracts through `journal.py`. | A terminal receipt with no descendants and matching ownership records all cleanup outcomes independently. | Residual descendants, cleanup failure, or unknown ownership preserves the resource and cannot be folded into success. |
| `REF-CS-SESSION-UI` | `SRC-CS` | `deferred` | tmux/session pause-resume UX and pane capture are not V1 completion or state truth. | No V1 authority or module. Trigger: a separately approved read-only UI proposal after JSON `status/collect` is stable. | V1 operates with no tmux/session-manager dependency. | Pane text, key injection, or session presence cannot satisfy a task or review gate. |
| `REF-CS-DESTRUCTIVE` | `SRC-CS` | `forbidden` | No agent auto-commit, force worktree removal, `RemoveAll`, `branch -D`, trust prompt injection, or pane-derived completion. | Phases 1/4; bridge/worktree static guards and controller-only commit protocol. | A validated uncommitted writer diff becomes exactly one controller-created candidate commit. | Agent commits, multiple commits, bypass input, force flags, or ownership-unknown deletion are rejected and preserved as a dispute. |
| `REF-CS-AGPL-CODE` | `SRC-CS` | `forbidden` | No Claude Squad implementation is copied, vendored, imported, translated line-for-line, or added as a runtime dependency; source observation does not grant code-reuse authority. | Phase 1 source-provenance/dependency inventory and license review, not a bridge/worktree runtime guard. | The dependency and vendored-source inventory contains no Claude Squad code/package, while this plan cites only independently expressed contracts and audit evidence. | A copied/vendored/imported implementation, unresolved provenance, or incompatible license obligation blocks schema freeze and landing. |
| `REF-MEM0-SCOPE` | `SRC-MEM0` | `contract_promoted` | Every orchestration state/resource query binds an explicit `run_id`; any mutation also binds `attempt_id` and the current generation/fencing token. No Mem0 `user_id`/`agent_id` schema is introduced. | Phases 1/2; orchestration event/resource interfaces and `journal.py` query/mutation seams. | `status/collect` and resource lookup return only objects owned by the requested run and current attempt where applicable. | Missing scope, cross-run/task access, stale generation, or path escape fails closed. |
| `REF-MEM0-HISTORY` | `SRC-MEM0` | `contract_promoted` | Runtime history is append-only JSONL with stable event identity, deterministic folding/deduplication, mode `0600`, and a sensitive-field denylist. | Phases 1/2; orchestration-event schema, `journal.py`, and privacy fixtures. | Replaying the same valid event set folds to one deterministic state while retaining allowed IDs, hashes, timestamps, and artifact pointers. | Prompt/response bodies, tokens, cookies, account identifiers, full commands, or mutable replacement of event history are rejected or redacted. |
| `REF-MEM0-SEMANTIC` | `SRC-MEM0` | `deferred` | No vector store, embedding/LLM extraction, mutable memory, telemetry, or semantic-memory write path enters V1. | No V1 authority. Trigger: a separate approved sidecar plan with local storage/embedding, telemetry off, no scheduler/ledger writes, and one frozen snapshot across benchmark arms. | V1 imports and starts no Mem0 component and makes no semantic lookup. | Any Mem0 runtime dependency, PostHog traffic, mutable memory truth, or scheduler/ledger write blocks V1. |
| `REF-AGTX-CLAIM` | `SRC-AGTX` | `contract_promoted` | Command intent and side effects are separated, but only per run: one controller holding the current fencing token may claim and perform mutations; this is not a global single executor. | Phases 1/2; event contract plus `scheduler.py`/`journal.py` with `dispatch_intent`, `dispatch_claimed`, and terminal outcomes. | Two controllers racing the same run yield exactly one valid claim and one side-effect generation while independent runs may still execute concurrently. | Duplicate command, stale token, replayed completed attempt, controller crash, or missing dependency cannot silently dispatch or count as satisfied. |
| `REF-AGTX-IDENTITY` | `SRC-AGTX` | `contract_promoted` | Task, run, attempt, controller generation, observed session receipt, wrapper PID/start fingerprint, worktree, branch, and base SHA form one mechanically reconcilable identity chain. | Phases 1/2/4; sole reconciliation owner `journal.py:reconcile_identity()` consumes the validated plan/result/event/resource schemas and worktree observations. | A complete dependency and identity chain validates, dispatches, and collects one uniquely attributed result. | Missing dependency, cycle, ambiguous session/run attribution, foreign resource ownership, or result for an unknown task fails closed. |
| `REF-AGTX-TRUST-HASH` | `SRC-AGTX` | `deferred` | Frozen tracked Git content is the only V1 project-config trust anchor; no TrustStore or host overlay is added. | No extra V1 authority. Trigger: a separate proposal to consume non-tracked project configuration, with an allowlist and digest contract. | Tracked repository-local agent files arrive only through the frozen base commit. | Host agent config, untracked environment, user MCP state, token files, symlink escapes, or init hooks cannot enter a generated worktree. |
| `REF-AGTX-RUNTIME` | `SRC-AGTX` | `forbidden` | No SQLite task truth/transition queue, global single executor, pane-message join, permission bypass, host-config copy, arbitrary init hook, or upstream code extraction while license declarations conflict. | Phases 1-4; validator, bridge argv checks, worktree provisioning guards, deterministic join, and license gate. | The fake adapter receives only the governed `agent-run` binding and the JSONL/controller path remains the sole runtime state path. | `--yolo`, skip/trust/allow-all/force flags, plan route overrides, SQLite writeback, pane completion, config copy, or `sh -c` init is rejected. |
| `REF-CCSW-ATOMIC` | `SRC-CCSW` | `contract_promoted` | Only controller-owned small replaceable manifests/snapshots use same-directory temp file, flush/fsync, and rename; append-only JSONL is never whole-file rewritten. | Phases 1/2/4; sole implementation owner `journal.py:write_replaceable_manifest()` with a documented platform durability level; `worktree.py` may call it but cannot reimplement atomic writes. | Failure injection before rename and after rename leaves readers with either the complete old version or complete new version. | Rename-before/after failure, half JSON, stale-fencing snapshot, or ownership-free cleanup target never yields an accepted manifest or resurrects stale authority. |
| `REF-CCSW-LOCK` | `SRC-CCSW` | `contract_promoted` | A cross-process per-run controller lease protects journal/resource mutation; a distinct provider-family admission lock protects governed route concurrency. Neither can replace the other or the routing canon. | Phases 0/2; `routing-policy.yaml`, runner admission, scheduler lease, and fencing fixtures. | Same-run controller races serialize while different explicitly parallel runs obey the canon's total/family limits. | A process-local mutex, stale lease, or family lock used to override an explicitly parallel canon declaration fails the contract. |
| `REF-CCSW-FINGERPRINT` | `SRC-CCSW` | `contract_promoted` | Provider configuration is observed only through a credential-stripped fingerprint for benchmark fairness; it is optional in ordinary runs and never affects routing. | Phases 1/5/6; benchmark manifest/report, not the route compiler. | All arms in a paired block use the same predeclared fingerprint and only the digest/coarse provider category is retained. | Mid-block drift marks the whole block `protocol-invalid`; raw URL, token, account, or config body is never journaled. |
| `REF-CCSW-MUTATION` | `SRC-CCSW` | `forbidden` | The orchestrator never invokes cc-switch, writes live provider config, takes over a proxy, rotates/fails over accounts, copies credentials, or treats rollback as authority to mutate provider or unknown Git resources. | Phases 0-6; bridge/preflight guards and the no-live-config-write regression boundary. | Native provider/session configuration remains unchanged throughout dispatch and benchmark execution. | Any provider/profile switch, proxy enablement, account rotation, config backfill, credential copy, or compensating deletion of unknown resources blocks the run. |

The matrix may be edited only as part of a reviewed plan/schema change. Updating
an upstream commit requires a fresh source and license audit; it cannot silently
change an existing local contract.

## 6. Target architecture

```text
versioned plan
     |
     v
validator -----> compiled routes/doctor from existing agent-run
     |
     v
ready-set scheduler -----> family admission + total/writer budgets
     |                              |
     |                              v
     +-----------------------> agent-run CLI bridge
                                      |
                         native Claude/Codex/Cursor/Grok
                                      |
                                      v
                 observed receipt + artifact hashes + local events
                                      |
          +---------------------------+-------------------------+
          |                                                     |
          v                                                     v
 worktree/scope verifier                               deterministic join
                                                                |
                                                                v
                                                   independent final review
```

Public surface:

- `scripts/agent_orchestrate.py`: thin CLI with `validate`, `start`, `resume`,
  `status`, `cancel`, and `collect`.
- `schemas/orchestration-plan.md`: versioned input contract.
- `schemas/task-result.md`: versioned task/result envelope.
- `schemas/orchestration-event.md`: append-only local event contract.

Internal package:

- `scripts/orchestration/plan.py`
- `scripts/orchestration/scheduler.py`
- `scripts/orchestration/journal.py`
- `scripts/orchestration/bridge.py`
- `scripts/orchestration/worktree.py`
- `scripts/orchestration/join.py`

The bridge calls the existing CLI rather than importing provider internals. It
consumes the machine receipt already emitted by `agent-run`. Scoped runner
affordances are limited to pure control/evidence needs: a `--receipt-file`
output if stderr-tail parsing is fragile, and process-group/cleanup outcome
telemetry if existing receipts cannot prove safe termination. Scheduling,
dependency, worktree, join, and retry policy never move into the runner.

The orchestration journal is JSONL, mode `0600`, and contains state transitions,
IDs, hashes, timestamps, failure classes, and artifact paths. It never contains
prompt/response bodies, credentials, cookies, account identifiers, or full
commands. SQLite may be a disposable view later, never the V1 truth.

Each run has a separate cross-process controller lease/lock, not merely a
permission bit on the journal. The event contract carries a stable `run_id`,
`attempt_id`, monotonic `generation` or `fencing_token`, and controller owner
PID/start fingerprint. Only the current fencing generation may append
state-changing events or create/delete managed resources. Small replaceable
snapshots and manifests use temporary-file, flush, and rename semantics;
append-only events retain the existing flock-guarded JSONL pattern.

Every controller-created worktree, branch, candidate commit, and integration
resource has a write-ahead ownership record containing `created_by_run_id`,
`repo_root`, absolute path, branch, and frozen base SHA. Missing or mismatched
ownership never authorizes cleanup.

## 7. Approved implementation contract and retained evolution record

The following phases are the approved implementation contract. V1 capability
work described through the offline harness has landed locally; Phase 6 remains
an evidence gate, not a completed live-value result. The text also preserves
future-extension constraints so that a later change cannot weaken the current
fail-closed boundaries.

### Phase 0 - Correct the current runtime contract

Deliverables:

1. Finish the route-doctor change without treating every absent lock equally.
2. Add an explicit route concurrency enum, for example
   `family_serial | explicitly_parallel`, to the machine canon.
3. Mark only `mechanical` and `mechanical_grok` as explicitly parallel, with a
   Cursor-family admission limit of two.
4. Make `ordinary_bug_fix`, `standard_feature`, judgment, restricted, and all
   review routes family-serial. `serial_group` is required for those routes.
5. Doctor blocks a missing/contradictory concurrency declaration. It reports
   `serial-lock-disabled` only as a warning for explicitly parallel routes.
6. Add a small, independently tested worktree-identity provisioning helper
   before the scheduler exists. Its required order is: create worktree -> read
   the source repository's canonical slug -> validate or write the worktree's
   `.agents/ledger-slug` -> exclude that exact untracked path through the local
   Git info/exclude -> `agent-ledger fold/claim` -> dispatch. A tracked matching
   slug is accepted; a tracked mismatch, untracked conflicting value, missing
   exclusion, or write failure stops closed. It never overwrites tracked
   project `.agents` content or adds a broad `.agents/` ignore rule. An existing
   target path, same-name generated branch, or branch already checked out in
   another worktree stops closed; the helper never reuses, force-removes, or
   force-deletes it.
7. Run a wrapped `composer-2.5-fast` canary and a wrapped
   `cursor-grok-4.5-high-fast` canary. Each must have one observed model, unique
   session ID, unique run ID, complete journal attribution, and exit 0.
8. Change `mechanical_grok` from `cursor-grok-4.5-high` to
   `cursor-grok-4.5-high-fast` only after its governed canary passes.
9. Re-run doctor. The target is `ready` for both mechanical routes when live
   evidence is fresh; a genuine external health failure may remain degraded but
   must not be relabeled green.

Gate:

- complete pytest and functional QA green;
- canon consistency tests prove all non-mechanical routes fail closed without a
  lock;
- no duplicate or ambiguous model/session attribution;
- temporary-repository and linked-worktree fixtures prove the strict
  slug/provision/claim order, exact `info/exclude` behavior, checked-out branch
  handling, and all conflict cases;
- no changes to billing, credentials, or global provider configuration.

### Phase 1 - Freeze contracts and run a time-boxed adopt/reference gate

Deliverables:

- Versioned plan, result, and event schemas. Required contract fields include
  controller/attempt/generation/fencing identity, wrapper PID/start fingerprint,
  deadlines, write-ahead resource ownership, per-writer acceptance,
  `integrated_acceptance[]`, and `shared_interface_paths[]`. A redacted
  `config_fingerprint` is optional in ordinary runs and required by the later
  benchmark protocol.
- Static validation for versions, duplicate IDs, missing dependencies, cycles,
  unknown task shapes, illegal seat/mode/workspace combinations, unsafe paths,
  overlapping writer ownership, shared-interface path classes, reviewer reuse,
  timeouts, resource ownership, and budgets. An omitted task deadline inherits
  its governed route timeout; an explicit task deadline may not exceed it.
- Plan tasks name only governed `task_shape`; they cannot override provider,
  model, effort, seat, execution mode/permission profile, or reviewer
  independence. Permission metadata is a read-only projection of canon, never
  a way to add authority.
- Freeze the non-executable Section 5.3 adoption matrix. Every
  `contract_promoted` row must point to one local authority, versioned
  schema/interface, owning module, positive fixture, and fail-closed fixture.
  Every `deferred` row must state its future trigger and keep V1 at zero runtime
  dependency. Every `forbidden` row must name its static or runtime rejection
  seam. Matrix status never implies implementation or verification.
- A maximum half-day, temporary-source review/spike of ODW checkpoint semantics,
  AgentAPI's adapter contract, Agent Orchestrator worktree lifecycle, agtx's
  atomic transition-claim/config-hash patterns, and Harbor task definitions. No
  global install or existing repo mutation. Each spike must produce the same
  fixed evidence-manifest and build-versus-adopt scorecard sections.
- An evidence manifest for every spiked project: canonical owner/repository
  after redirects, pinned commit, every conflicting license declaration and
  digest, `pushed_at`, snapshot time, and official verification URLs.
- A build-versus-adopt scorecard. Every candidate must satisfy **all** hard
  constraints: preserve native subscription CLI/session behavior; consume the
  existing routing canon; retain observed run/model/session receipts; preserve
  checkpoint/ledger continuity; support isolated worktrees; pass license review;
  require no unapproved global configuration, credential copy, or telemetry;
  and be deterministically testable offline. Candidates that pass are then
  compared on integration surface, added daemon/state ownership, process safety,
  recovery, latency overhead, maintenance burden, and removable/reversible
  deployment.

Gate:

- The schemas can express all benchmark tasks and all failure fixtures.
- Static fixtures prove a stale controller generation cannot dispatch, append a
  state change, or clean a resource; missing resource ownership and undeclared
  shared-interface paths fail closed.
- Phase 1 cannot freeze while any Section 5.3 row lacks its required mapping or
  while its Phase-1-owned schema/static contract fixtures lack both positive and
  fail-closed coverage. Behavioral fixtures owned by later phases must be named
  now and pass at their owning phase; they are not required to execute before
  those modules exist. A deferred/forbidden behavior entering the runtime still
  blocks immediately. The matrix itself must have no runtime loader, parser, or
  route effect.
- A candidate can pause the native build only if it passes every hard constraint
  and reduces estimated integration plus maintenance surface by at least 30%
  without adding more than 10% dispatch overhead in the mock spike. Meeting the
  threshold triggers a user decision; it never authorizes automatic adoption.
  Otherwise the scored evidence explains why the thin native bridge proceeds.

### Phase 2 - Offline scheduler with a fake adapter

Deliverables:

- Ready-set DAG execution, pipeline progress, total and family admission limits.
- Retry classification, dependency failure propagation, deadlines, cancellation,
  event folding, crash-resume, and no replay of completed nodes.
- One cross-process controller lease per `run_id`, implemented with an
  independent lock file/flock. `start`, `resume`, and `cancel` must atomically
  acquire it; a contender may serve read-only status or return
  `already-controlled`, but may not dispatch or mutate state. Every attempt has
  a stable `attempt_id` and current generation/fencing token, and only one
  controller may append state-changing events.
- A write-ahead resource-manifest transition before any fake or real resource
  creation, followed by a separate created/failed confirmation. A crash between
  intent and confirmation leaves a disputable resource, never an unowned
  cleanup target.
- Fake time/process/provider/worktree adapters so tests do not call live models.
- Fake-adapter command assertions that no native yolo, skip-permission, trust,
  bypass, or force flag can be introduced outside the governed bridge.
- Functional QA script following the existing import-with-fixtures pattern.

Cancellation has one V1 meaning: **graceful cancel and drain**. The controller
atomically records `cancel_requested`, stops admitting new tasks, marks pending
nodes canceled, and does not externally kill a live `agent-run` wrapper. Every
live task has a mandatory deadline; its existing runner owns timeout escalation
and provider-process-group cleanup. The plan remains `canceling` until each live
child emits a terminal receipt or its deadline passes. Forced cancellation is
out of scope until the runner has a tested cooperative signal/receipt contract.
`status` and `cancel` expose a conservative `eta_seconds` equal to the maximum
remaining deadline among live children; it is a drain estimate, not a promise.

The controller records wrapper PID, start fingerprint, deadline, and receipt
correlation. After a controller crash, resume first checks whether the exact
wrapper is still live; it drains that process rather than spawning a duplicate.
After a terminal receipt or timeout it verifies the recorded process group has
no descendants. Any unexplained residual process makes the node and plan
`failed-unsafe`; it cannot be resumed or retried automatically.

Gate:

- deterministic event traces across repeated runs;
- property/fixture tests cover cycles, failures, retries, resume, cancel, and
  concurrency bounds;
- two controllers racing `start/resume/cancel` prove that exactly one generation
  can dispatch or append; stale fencing tokens fail closed;
- stable attempts are not replayed after controller restart, and orphaned
  resource-intent fixtures are preserved for inspection;
- existing `agent-run` tests remain unchanged and green.

### Phase 3 - Read-only native dispatcher

Deliverables:

- Spawn `agent-run run auto --task-shape ...` as a subprocess.
- Parse its observed receipt and use journal correlation only as a fallback.
- Record plan-writing time, context/prompt construction time, delivered prompt
  bytes, queue wait, lock wait, provider duration, artifact hashing, join time,
  and total wall time without changing the prompt path merely to measure it.
- Compile command mode/authority only through the governed `agent-run` binding;
  the dispatcher cannot pass provider-native approval bypasses.
- Adaptive backoff for `rate-limited` and `upstream-overload`; terminal handling
  for auth, quota, independence, and attribution failures.
- One explicitly approved three-node read-only canary, followed by kill/restart
  resume testing with fake providers and then live providers.

Gate:

- zero accepted results with ambiguous attribution;
- completed nodes are never replayed after restart;
- graceful cancel drains existing runs, and forced-kill behavior is not claimed;
- any residual provider descendant produces `failed-unsafe` and blocks resume;
- no systematic lock-wait exit 75 caused by the scheduler's own admission logic;
- receipt parsing succeeds in all canaries or the scoped `--receipt-file`
  affordance is added and tested before proceeding.

### Phase 4 - Writer isolation and deterministic join

Deliverables:

- One worktree per write task, based on a frozen base SHA, and never more than
  one writer in the same worktree.
- The frozen tracked tree is the only default workspace input. Tracked
  repository-local `.claude`, `.codex`, and similar project files are part of
  that base. The controller never copies host `~/.claude`, `~/.codex`,
  `~/.cursor`, user MCP state, tokens, untracked `.env` files, or any other
  unapproved overlay into the worktree, and V1 has no per-run init/cleanup hook.
- Canonical ledger-slug stamp, base/head capture, declared `own[]` and
  `do_not_touch[]` scope, acceptance commands, required
  `shared_interface_paths[]`, and keep-on-failure cleanup policy.
- Global writer concurrency remains one through the first writer canary. It may
  be raised to two for an explicitly parallel plan only after the validator
  proves different worktrees, disjoint `own[]` scopes, frozen shared interfaces,
  and no shared migration/config/schema target. Merge and join remain single
  writer. Two is the V1 hard maximum.
- Native writers are required to leave an uncommitted diff in their isolated
  worktree; they do not own Git commits. Before dispatch the controller records
  a clean frozen HEAD. After return it requires HEAD to be unchanged, validates
  tracked and untracked paths against scope, hashes the diff, and runs the
  per-writer acceptance commands. Only then does the controller create exactly
  one candidate commit with a fixed controller identity and plan/task message.
  It verifies one-parent ancestry from the frozen base, a clean post-commit
  worktree, and a base-to-HEAD diff hash matching the validated pre-commit diff.
  An agent-created commit, multiple commits, dirty post-commit state, commit
  failure, changed diff hash, or non-ancestor base stops as a dispute and is
  preserved for inspection.
- The controller creates one dedicated integration worktree and branch at the
  same frozen base, then applies controller-created candidate commits in
  deterministic task-ID order.
  It may proceed only when changed paths are mechanically non-overlapping and
  Git applies each commit cleanly. Any conflict, dirty integration state, rebase
  need, or shared-interface change stops at an explicit dispute; V1 never asks
  an agent to auto-resolve it.
- A fixed shared-interface path-class taxonomy covers public API, schema,
  migration, configuration, and other project-defined coupling points. Any
  writer overlap is serialized or rejected. If the final diff hits the taxonomy
  without a matching `own[]` or `shared_interface_paths[]` declaration, join
  fails closed even when the declaration array was empty.
- Mechanical join checks, in order: result schema, exit/failure class,
  attribution, changed-file scope, source ancestry, clean deterministic apply,
  integration ancestry/head capture, and every required
  `integrated_acceptance[]` command on the integrated candidate. Per-writer and
  integrated acceptance results are recorded separately. That integration HEAD
  is the sole frozen object sent to review.
- Before resume or writer/integration reuse, the controller reconciles
  `git worktree list --porcelain`, absolute path, base SHA, HEAD, ledger slug,
  validated diff hash, and integration HEAD against the current manifest.
  Drift is `failed-unsafe`, not an automatic retry.
- Cleanup is allowed only when the resource manifest identifies the current
  run, the current fencing token is valid, and the absolute path is inside the
  expected generated-worktree root. A resource without a matching manifest is
  reported and preserved; force-delete and same-name branch cleanup are
  forbidden.
- Equivalent findings are deduplicated; disagreements remain explicit disputes.
- Independent final review is dispatched only after the frozen candidate passes
  all mechanical checks.

Gate:

- injected scope breaches and dirty retries fail closed;
- the main checkout remains unchanged by failed writer canaries;
- fixtures prove host-level agent config, untracked environment files, symlink
  escapes, and init hooks cannot enter a generated worktree;
- a two-writer fault test proves non-overlapping changes can run concurrently,
  while any shared-file/interface declaration is serialized or rejected;
- taxonomy hits omitted from declarations fail closed, and writer acceptance
  never substitutes for integrated acceptance;
- deterministic apply creates one uniquely identified integration HEAD, and
  conflict/rebase/dirty fixtures stop without changing the main checkout;
- stale fencing, missing manifest, orphaned worktree, and path-ownership
  fixtures preserve resources and produce a dispute or `failed-unsafe`;
- all acceptance commands rerun on the integrated candidate, not only on each
  writer branch;
- final review cannot reuse the producer session/seat and respects the routing
  canon's family-independence rule.

### Phase 5 - Offline benchmark harness and preregistration

Deliverables:

- A local paired-task manifest, fixtures, result normalizer, blinded artifact
  exporter, and report generator.
- Optional Harbor adapter design after the local harness is stable; Harbor does
  not replace project-specific acceptance tests.
- Commit the metrics, exclusions, thresholds, and stopping rules before live
  trials.
- Freeze the exact reviewer mapping for every paired task: route, model, effort,
  prompt hash, hard timeout, and family-independence rule. A/B/C for a task use
  the same binding, and that reviewer family is excluded from that task's
  producer set. The mapping may vary by task when required for independence,
  but it cannot change after results are visible. A fast governed reviewer is
  used for benchmark cells; Fable Max is reserved for explicit phase/rollout
  gates, not the daily or per-cell hot path.
- Freeze a numeric/provider-specific quota-headroom threshold and minimum
  cooldown or `Retry-After` formula before confirmation. Insufficient headroom
  before any arm postpones the whole paired block; a rate limit caused inside a
  running B/C arm remains a treatment outcome.
- Freeze deterministic definitions of first-pass acceptance, rework, manual-B
  coordination intervals, and provider configuration drift. First pass is the
  first candidate entering common acceptance, with no prior rework, that passes
  every deterministic command on the integration HEAD. Reviewer preference is
  never part of its Boolean value.
- Freeze measurement definitions for `context_construction_ms`, delivered
  prompt bytes, deduplicated shared-context bytes, and any provider-reported
  cache hit. Shared-context and cache evidence are secondary/descriptive and
  receive no cross-provider threshold; instrumentation must not alter the
  prompt path.
- Commit only an irreversible hash manifest and arm-order seed for confirmation
  and reserve tasks. Task bodies, hidden assertions, evaluator fixtures, and
  reserve content stay in a mode-`0700` evaluator-only local directory or
  separate read-only evaluator checkout that is never mounted in producer
  worktrees. The report verifies hashes before unblinding.

Gate:

- offline fixtures reproduce wins, ties, regressions, infrastructure failures,
  attribution exclusions, and misleading-fast-but-wrong cases;
- fixtures reproduce controller/config drift, insufficient pre-block headroom,
  inside-block rate limits, first-pass/rework boundaries, slow reviewer
  warnings, and unavailable cache telemetry;
- all numeric headroom/cooldown values and exact reviewer bindings are present;
  placeholders or operator judgment at dispatch time fail preregistration;
- the report distinguishes provider noise from orchestration defects.

### Phase 6 - Explicit live three-arm benchmark

Live execution is a separate gate because it consumes subscription quota and
creates many provider sessions.

Two hypotheses are evaluated separately:

- **H1 - control-plane value:** C reduces the execution/coordination overhead of
  today's manual multi-agent B.
- **H2 - multi-agent value:** C produces a better quality-adjusted outcome than
  single-agent A. H2, not H1 alone, is required to change the daily default.

All three arms share the same acceptance stage. Each candidate runs the same
deterministic commands and then receives one fresh, blind, independent reviewer
using the same frozen review prompt and route for that task. The reviewer family
is reserved before the experiment and is not used by any producer arm for that
task. Review time, findings, and rework are included in time-to-accepted, so A
does not receive an easier definition of “done.” Reviewer findings are recorded
as severity/dispute evidence, not folded into deterministic first-pass or final
acceptance. Review wall time above
`min(300 seconds, 0.5 * producer_time_to_candidate)` raises a diagnostic warning
but does not invalidate or kill a review; the governed route hard timeout still
applies. Time-to-accepted excluding review is reported only as a secondary
diagnostic.

Production arms:

- **A - single native producer:** one best-fit producer from the task's allowed
  producer set, concurrency one, completes the whole task before the common
  acceptance/review stage.
- **B - current manual fan-out:** the lead manually launches and collects the
  same producer nodes, routes, prompts, decomposition, writer limits, and join
  rules later used by C. B uses one frozen, hashed manual runbook and the same
  launcher contract. Coordination time is derived only from predefined event
  pairs; diaries and operator self-report are forbidden.
- **C - automatic orchestrator:** the control plane executes that frozen B/C
  graph. The treatment difference between B and C is scheduling, recovery,
  collection, and join automation.

The experiment is staged:

1. **Feasibility pilot:** three frozen tasks (one separable feature, one
   single-module negative control, one broad read-only task) x three arms = nine
   trials. It is also operator training. It can block or repair the harness but
   can never enable a default; learning trends are disclosed and never used to
   change thresholds or discard a cell.
2. **Confirmation:** only after separate approval, twelve unseen frozen tasks:
   six separable features, three negative controls, and three read-only breadth
   tasks x three arms = 36 trials. Task/arm order is counterbalanced. Only their
   hash manifest and order seed are committed before execution; private task and
   reserve content remains evaluator-only until each trial is dispatched.

No cell is repeated because its result is near a threshold. A trial may be
replaced at most once only when a predeclared invalid-trial rule is satisfied;
the original remains in the report. A failed confirmation requires a new,
precommitted task set after a mechanism-level correction, not selective reruns
of favorable cells.

Fairness controls:

- same base commit, high-level intent, prompt hash, route policy, acceptance
  commands, deadline, and common blind reviewer for each paired task; B and C
  additionally receive the same frozen decomposition, writer limit, and hashed
  manual/control graph;
- producer worktrees cannot read hidden evaluator assertions, future task
  bodies, reserve tasks, arm labels, or blinded review identities;
- fresh sessions and isolated workspaces;
- the primary clock starts at task handoff. A's in-agent planning is therefore
  included naturally; B/C graph preparation is timed and charged in full to
  each arm. Execution-only timing is reported only as a secondary diagnostic;
- before each paired block, collect the provider health/doctor snapshot,
  preregistered quota/headroom evidence, cooldown state, and a canonicalized,
  credential-stripped provider configuration fingerprint plus coarse
  `official|proxy` category. Store no base URL, token, account, or config body.
  Any configuration drift within the block invalidates the whole block;
- an auth failure, known provider incident, base/acceptance drift, or host outage
  detected before a paired block, or insufficient preregistered headroom for
  any arm, invalidates and reschedules the whole block.
  Rate limits or quota pressure caused by B/C concurrency remain treatment
  outcomes and are never excluded;
- deterministic acceptance is primary; blinded human preference and independent
  review findings are secondary.

Failure taxonomy and denominators:

- `task-quality-failure`: the provider path completes but no accepted candidate
  is produced within the fixed rework/deadline budget;
- `orchestration-infrastructure-failure`: scheduler deadlock, duplicate replay,
  receipt loss, worktree/provisioning error, or deterministic-join defect owned
  by C;
- `provider-environment-failure`: auth, provider outage, quota, or upstream
  failure, excluded only by the paired-block rule above;
- `protocol-invalid`: wrong base/prompt/acceptance/reviewer, configuration
  fingerprint drift, operator deviation, or corrupted fixture; the entire
  paired block is rerun once;
- `failed-unsafe`: scope escape, ambiguous accepted attribution, or residual
  process. This is never excluded and immediately blocks rollout.

The confirmation denominator is twelve paired tasks per arm; the report also
shows all 36 original raw trials and any permitted replacement separately.

Primary metrics:

- time-to-accepted result, including rework;
- first-pass acceptance and final acceptance;
- severity-weighted review findings and unresolved disputes;
- scope violations, merge conflicts, and rework rounds;
- event-derived human coordination minutes as a secondary diagnostic;
- sum of provider duration/agent-minutes, context construction time, and
  delivered prompt length;
- attribution completeness and infrastructure failure rate.

Secondary/descriptive metrics include time-to-accepted excluding review,
deduplicated shared-context bytes, provider-specific cache-hit evidence, and the
learning trend across ordered cells. A pure harness retry of an unchanged
artifact is recorded as infrastructure/protocol evidence rather than rework;
any additional producer call or code change after an acceptance failure is a
rework round.

Preregistered confirmation thresholds:

- **H1 passes** when C versus B reduces median paired inclusive
  time-to-accepted by at least 20%, while C has no more task-quality failures
  and no worse first-pass acceptance than B by more than one of twelve tasks.
  Coordination minutes have zero gate weight and cannot substitute for the
  latency threshold.
- **H2 speed path passes** when C versus A is at least 1.30x faster in median
  paired time-to-accepted across the six separable tasks, C has no more final
  task-quality failures than A, and C's first-pass acceptance is not lower than
  A by more than one of twelve tasks.
- **H2 quality path may substitute for speed** only when C gains at least two
  additional first-pass acceptances across the twelve tasks, has no more final
  task-quality failures, and its median time penalty versus A is at most 10%.
- On the three negative controls, the C wrapper/single-node path adds at most
  10% median overhead. It must select a single producer rather than manufacture
  fan-out for an inseparable task.
- Accepted results require 100% unambiguous run/model/session attribution and
  zero scope violations. Any `failed-unsafe` outcome fails the rollout.
- C's median agent-minutes on separable tasks must not exceed 2.2x A, and no
  paired task may exceed 3x A without a predeclared provider retry explanation.
- One C orchestration-infrastructure failure among twelve original confirmation
  trials blocks default enablement pending a fix and a new confirmation set;
  two or more fails the design without an automatic second correction cycle.

Decision:

- **H1 and H2 pass:** enable orchestration only for the task shapes that passed;
  keep simple tasks single-agent.
- **H1 passes, H2 fails:** the control plane may replace manual B when fan-out is
  explicitly chosen, but multi-agent work does not become the daily default.
- **H2 passes, H1 fails:** multi-agent work may have value, but C is not yet an
  efficient implementation; keep the default unchanged and permit one redesign.
- **Inconclusive or harness-invalid:** fix the mechanism once and use the
  precommitted reserve/new confirmation protocol; never rerun only favorable or
  near-threshold cells.
- **Fail:** retain resume, worktree isolation, deterministic join, and benchmark
  tooling if independently useful; keep multi-producer fan-out opt-in or remove
  it. Do not redefine the metrics after seeing results.

## 8. Verification matrix

| Layer | Required verification |
|---|---|
| Routing/doctor | explicit concurrency-policy tests, missing-lock fail closed, wrapped Composer/Grok canaries, ready/degraded truth |
| Schema | malformed versions, duplicates, missing deps, cycles, unsafe paths, invalid route/seat/mode/permission/workspace, shared-interface declarations, controller generation, attempt/resource ownership, deadline and budget overflow |
| Scheduler | bounded parallelism, family admission, single-controller lease/fencing, stable attempts, single-writer events, pipeline progress, retry classes, deadline, failure propagation, cancel ETA, resume |
| Receipts | run/model/session uniqueness, corrupted receipt fallback, hash mismatch, ambiguous attribution fail closed |
| Worktrees | strict create/stamp/fold/claim order, frozen tracked-only baseline, no host-config/env/hook copy, write-ahead ownership manifest, agent-commit rejection, controller single-commit protocol, ownership collision, scope escape, dirty retry, resume reconciliation, integration worktree, failed cleanup preservation |
| Process safety | graceful cancel/drain, timeout-owned killpg, residual-process `failed-unsafe`, lock wait, controller crash/restart |
| Join/review | deterministic source and integration checks, integrated acceptance, dedupe, dispute preservation, independent reviewer enforcement |
| Privacy | journal mode `0600`, no prompt/response/credentials/full command, path redaction, credential-stripped config fingerprint, no live provider-config mutation |
| Reference adoption | unique stable pattern IDs; pinned source/license evidence; no runtime parser; every promoted contract maps to its owning schema/module plus positive and fail-closed fixtures; every deferred/forbidden row preserves zero dependency or an explicit guard; runtime packages contain no import/read of this Markdown or `REF-*` IDs, and fake scheduling still works when the document is unavailable |
| Regression | complete pytest, Agent Run functional QA, orchestration functional QA, py_compile, `git diff --check` |
| Value | non-decisional training pilot, private evaluator fixtures plus committed hashes, preregistered H1/H2 confirmation, fixed per-task blind reviewer binding, quota/headroom protocol, config-drift invalidation, context diagnostics, fixed invalid-trial rules |

Verification commands after the implementation files exist:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests -q
python3 -m py_compile scripts/agent_orchestrate.py scripts/agent_provider_run.py
python3 scripts/qa_agent_run_functional.py
python3 scripts/qa_agent_orchestrate_functional.py
git diff --check
```

## 9. What not to build in V1

- No second GitHub repository before an extraction trigger.
- No free-form group chat, transcript database, or checkpoint-ledger task bus.
- No AI-generated graph execution before deterministic validation and explicit
  user approval.
- No default swarm of five, ten, or fifty agents; default total concurrency is
  three, global writer admission starts at one, and the gated V1 hard maximum
  is two isolated non-overlapping writers.
- No shared-checkout concurrent writers.
- No duplicate routing canon, provider health logic, process cleanup, failure
  taxonomy, checkpoint enforcement, or reviewer-independence implementation.
- No ODW/Paseo/Gas Town/agtx/Agent Orchestrator runtime in the production path.
- No Claude Squad code/runtime, tmux-pane completion truth, AutoYes/trust-prompt
  key injection, or runtime MCP registration in a user/local scope.
- No Mem0 or other semantic/vector memory in the V1 task, join, ledger, or
  benchmark runtime. A future sidecar is a separate proposal and comparative
  tests require one frozen snapshot for all arms.
- No programmatic cc-switch invocation, provider/profile switch, proxy
  takeover, account rotation, config backfill, or orchestrator write to
  `~/.codex`, `~/.claude`, `~/.cursor`, or `~/.gemini`.
- No adapter-added yolo, skip-permission, trust, allow-all-tools, or equivalent
  authority expansion. Mode and permissions come only from the routing canon
  and governed `agent-run` binding.
- No CLIProxyAPI account pool, OAuth proxy, or raw provider API substitution for
  native `agent-run` execute routes.
- No automatic push, PR, merge into the source/protected branch, release,
  deployment, or destructive worktree cleanup. The isolated integration branch
  is only a frozen review candidate.
- No paid judge and no live model benchmark in CI.

## 10. Completion and rollback conditions

Each phase must leave a reversible boundary:

- Phase 0 is a small canon/doctor change with direct regression coverage.
- Phase 1 cannot freeze until the Section 5.3 matrix is fully mapped, its
  promoted/forbidden seams are covered, every deferred row retains zero V1
  runtime dependency with its reconsideration trigger still unmet, and
  controller lease/fencing, attempt/resource ownership, permission non-override,
  integrated/shared-interface acceptance, and deadline contracts plus
  fail-closed fixtures are complete. Phases 1-2 are
  otherwise new schemas/modules and fake-only tests; removing them restores the
  previous runtime.
- Phase 3 stays read-only until receipts and resume are proven.
- Phase 4 writer worktrees never mutate the main checkout automatically and are
  retained on failure for inspection.
- Phase 5 cannot freeze with placeholder reviewer bindings, headroom/cooldown
  values, configuration-fingerprint rules, first-pass/rework definitions, or
  context-measurement methods.
- Phase 6 cannot change defaults until its preregistered report is reviewed.

The work is complete only after the frozen implementation diff passes the full
verification matrix and a fresh independent reviewer explicitly accepts or
challenges every deliberate tradeoff in this document.
