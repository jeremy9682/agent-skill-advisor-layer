# Agent Run Live Pilot Enablement Gate

Date: 2026-07-18

Status: approved implementation gate for the nine-cell feasibility pilot only.
It does not authorize a remote repository, push, merge, confirmation run, or
daily-default routing change.

## Trigger and decision

`scripts/orchestration/*.py` exceeded the V1 extraction trigger. The frozen V1
candidate is commit `d67df7ba7d01ab66d9d9d4c15f7cdafad72ff3d4`.

The feasibility pilot should measure the frozen implementation before a
repository migration changes its packaging and startup path. A narrowly scoped,
removable live-enablement layer may therefore remain in
`agent-skill-advisor-layer` for the pilot. No other material orchestration
feature may be added here.

If the pilot is promising, the next material step is an extraction to a private
`agent-run-orchestrator` repository before the 36-cell confirmation. If the
pilot fails or is unsafe, the enablement layer is removed or archived and no
new repository is created.

## Sole canon and ownership boundary

- `routing-policy.yaml` remains the only routing canon.
- The benchmark pins its real SHA-256 and never copies or edits provider
  configuration.
- Provider credentials, account identifiers, raw configuration, quota pages,
  run/session receipts, evaluator tasks, worktrees, and evidence remain local.
- Git stores only schemas, validators, launch-free control code, tests, and
  redacted aggregate conclusions.

## Allowed implementation surface

1. A `preflight` CLI command that is structurally unable to call the experiment
   runner or provider launcher.
2. A versioned attested evidence bundle consumed by live preflight and live run.
3. Per-paired-block revalidation so later blocks cannot reuse stale evidence.
4. Strict protocol validation that rejects placeholder base commits and route
   policy hashes.
5. Local generation of three low-risk disposable Git fixtures with real clean
   HEADs, lifecycle contracts, acceptance argv, ready-set runbooks, and frozen
   private evaluator hashes.

Changes are limited to the benchmark CLI/runtime/contract modules, focused
tests, this intent, and local-only evaluator material. Scheduler, join, provider
routing, skill governance, provider configuration, and unrelated repositories
are out of scope.

## Evidence bundle trust contract

The bundle is a regular non-symlink file with mode `0600`. It contains only an
allowlisted schema:

- schema version, observation time, expiry/freshness window, attesting actor;
- exact required provider-family keys;
- per-family `auth_ok`, `host_healthy`, `provider_incident`, numeric
  `headroom_fraction`, cooldown elapsed, and retry-after seconds;
- credential-stripped configuration fingerprint and coarse
  `official|proxy` category;
- host/check-out identity needed to prevent cross-run reuse.

Unknown headroom, missing families, stale or future-dated observations,
world-readable files, symlinks, unexpected keys, credential-shaped keys or
values, identity mismatch, policy drift, or configuration drift fail closed.
There is no override flag. Numeric headroom may be an explicit operator
attestation when a provider exposes no machine-readable residual fraction; the
software must never invent one from a successful call.

## Launch and fixture boundaries

- `preflight` may read validated local evidence, journals, Git metadata, hashes,
  and evaluator manifests. It cannot import or reach the live experiment runner.
- `run --live` accepts the same validated evidence path and repeats the gate
  before every paired block.
- Disposable fixtures have no remote, no production data, no credential or
  provider configuration, and no path outside their owned local root.
- The evaluator manifest pins the real fixture HEAD, route-policy digest,
  prompts, graphs, runbooks, acceptance argv, and independent reviewer binding.
- A synthetic fixture or placeholder such as a seven-character fake commit or
  repeated-character SHA cannot preregister as executable live material.

## Required tests and stopping rules

- rejection matrix for permissions, symlinks, schema, freshness, identity,
  privacy, headroom, cooldown, policy/config drift, and unexpected fields;
- proof that `preflight` creates no output root and cannot reach launch code;
- absent evidence preserves today's blocked production default;
- per-block evidence expiry prevents later blocks from launching;
- real fixture validation and negative tests for all former placeholders;
- full pytest, both functional QA suites, compile, Ruff, diff check, and provider
  doctor before any real call.

Any unsafe result, ambiguous attribution, scope escape, provider-config write,
or orchestration-infrastructure failure stops the pilot. The nine-cell pilot
cannot enable a daily default; it can only justify extraction and a separately
approved confirmation.
