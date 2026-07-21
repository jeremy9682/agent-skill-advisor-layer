# Orchestration Plan Contract v1

`scripts/orchestration/plan.py` is the runtime authority. This document is a
human-readable description, not executable routing input.

## Root object

Required fields:

- `version`: integer `1`.
- `repo_root`: absolute repository path. It is normalized by the validator.
- `tasks`: non-empty array of task objects.

Optional fields:

- `run_id`: stable, caller-selected run identifier. The controller supplies one
  when omitted.
- `base_sha` and `ledger_slug`: required whenever a task is an
  `isolated-writer`; they bind every write-ahead resource identity.
- `budgets.total_concurrency`: `1..3`, default `3`.
- `budgets.writer_concurrency`: `1..2`, default `1`, never greater than total.
- `budgets.family_concurrency`: map of provider-family/serial-group limits.
  Omission inherits `routing-policy.yaml.concurrency_policy`; a plan may only
  lower a known canon limit. Serial groups remain hard `1` and unknown family
  keys are rejected.
- `integrated_acceptance`: arrays of argv arrays, valid only when the plan has
  at least one `isolated-writer`; shell/Python command strings are forbidden,
  including when an interpreter is wrapped by `env` or `timeout`.
- `config_fingerprint`: only `{digest: <sha256>, provider_category:
  official|proxy}`. It is observational and never affects routing.
- `metadata`: non-authoritative caller metadata.

## Task object

Required: `id` and governed `task_shape`. Optional fields are `depends_on`,
`workspace`, `deadline_seconds`, `retry`, `input_ref`, `acceptance`,
`reviewer_for`, `result_contract`, and non-authoritative `metadata`.

`result_contract`, when present, is exactly `analysis-v1`. The provider must
emit one bounded `AGENT_RUN_ANALYSIS_RESULT:` JSON marker. The controller
validates its allowlisted semantic fields and writes a separate mode-`0600`,
SHA-256-bound artifact. Raw prose remains private and is never substituted for
the structured contract.

`acceptance` is valid only on an `isolated-writer` task. Read-only tasks and
no-writer plans may not declare acceptance commands that the runtime cannot
execute and record.

For every non-review task with `depends_on`, the controller creates a bounded
mode-`0600`, SHA-256-bound dependency bundle only after every declared
dependency has succeeded. The bundle contains terminal identity/status facts
and verified candidate/artifact pointers, never prompts, responses, free-form
chat, credentials, checkpoint authority, commands, or environment. The
consumer receives only a read-only appendix naming the bundle and digest; mode,
path, size, hash, run/task/attempt identity, and dependency membership are
revalidated immediately before dispatch. This is deterministic result handoff,
not an agent mailbox or second task truth source.

`task_shape` must be a currently enabled key in
`routing-policy.yaml.runtime_routes`. Provider, model, effort, seat, execution
mode, permission profile, review independence, command, environment, account,
profile, and serial-group values are forbidden plan inputs. The compiler adds
the governed `binding`, `family`, `family_limit`, and read-only
`permission_projection`.

`workspace.kind` is `read-only` (default) or `isolated-writer`. A writer must
declare non-overlapping repository-relative paths in `own`; all paths reject
absolute paths, `..`, backslashes and `.git`. `shared_interface_paths` must be
literal paths covered by the same task's `own`. `do_not_touch` may not overlap
ownership. Read-only tasks cannot claim paths.

An omitted deadline inherits the governed route timeout (or the V1 default of
300 seconds). An explicit deadline cannot exceed it. Retry count is `1..3` and
the only retryable classes are `provider-transient`, `provider-rate-limit`,
`provider-preflight-transient`, and `adapter-transient`. The preflight class
is for a bounded provider-wrapper failure before a provider run is established;
it is not a quota/usage probe and does not make quota exhaustion retryable.

Review tasks use a governed task shape containing `review`, depend on every
task in `reviewer_for`, and cannot share one reviewed target with another
review task.

The compiler resolves each exact governed model to `model_family` using the
read-only capability facts and broker-family glob rules in
`agent-providers.yaml`; that manifest does not supply routes. A `cross-family`
review rejects undisclosed families and equal producer/reviewer families.
`independent-supplement` must target a route listed by the review binding's
`eligible_producer_routes`. Every accepted review receives a
`reviewer_independence_projection` requiring a distinct attempt and fresh
session; plans cannot provide or reuse a session ID.
When a reviewed producer declares `analysis-v1`, the review bundle contains the
validated semantic artifact pointer and digest. A passing reviewer must emit an
`AGENT_RUN_CONSUMED_ARTIFACTS:` list matching every such digest before its final
verdict; missing or mismatched consumption fails closed.

`input_ref`, when present, is either an existing repository-relative file or
an opaque `evaluator:<id>` pointer. Inline prompts and escaping paths are not
accepted. Metadata is recursively limited to short annotations and cannot
shadow routing, permission, command, prompt/response, credential, account, or
provider-configuration fields.

## Example

```yaml
version: 1
run_id: demo-run
repo_root: /absolute/repository
base_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
ledger_slug: repository
budgets:
  total_concurrency: 3
  writer_concurrency: 1
tasks:
  - id: inspect
    task_shape: ordinary_bug_fix
    workspace: {kind: read-only}
  - id: implement
    task_shape: standard_feature
    depends_on: [inspect]
    workspace:
      kind: isolated-writer
      own: [scripts/orchestration]
      shared_interface_paths: [scripts/orchestration/__init__.py]
    acceptance: [[python3, -m, pytest, tests/test_agent_orchestration_core.py, -q]]
```

Unknown fields, duplicate IDs, missing dependencies, cycles, unsafe paths,
writer overlaps, disabled/unknown task shapes, authority overrides, unsafe
native flags, deadline overflow and budget overflow fail closed.
