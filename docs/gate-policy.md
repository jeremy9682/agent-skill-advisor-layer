# Gate Policy

This policy adapts the useful parts of `no-mistakes` without making a local git
proxy mandatory.

## Push And Ship Rule

Nothing reaches a push target, pull request, deployment, or production action
until the relevant checks are green or the user explicitly accepts the residual
risk.

Green evidence can be:

- focused tests for the changed behavior;
- lint, typecheck, format, or build checks when relevant;
- smoke tests or browser QA for user-facing changes;
- CI checks for release or PR workflows;
- a clear written exception when no automated check exists.

## Intent Statement

Every review or gate run should include an intent statement. Intent is the
user's objective and constraints, not a summary of the files changed. See
[`schemas/intent.md`](../schemas/intent.md).

Thin intent produces noisy review. A good intent tells reviewers what would look
surprising in the diff but was deliberate.

## Finding Classes

| Class | Meaning | Default action |
| --- | --- | --- |
| Safe mechanical | Formatting, imports, spelling, generated artifacts, obvious typo fixes | Agent may fix and verify |
| Gated auto | Concrete low-risk fix proposed, but behavior may be touched | Agent may apply only when the owning workflow permits and verification is available |
| Intent-touching | Product behavior, public API, data model, auth, billing, UX, or tradeoff changes | Escalate to user or plan owner |
| Advisory | Risk, missing coverage, rollout note, or future cleanup | Record without blocking unless severity demands |

## Autonomous Pipeline Approval

Workflows such as `/no-mistakes`, `/lfg`, `ship`, `overnight-execution`, and
multi-agent swarms can create commits, push, open PRs, or watch CI. They are
high-cost workflows.

Do not start them from a broad outcome request like "ship this" or "finish the
feature". Use:

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

Start only after the user explicitly says to run, start, enable, pair, set up,
or launch that specific workflow.

## Active Gate Runs

If a gate workflow is active, do not silently bypass it. Either continue the
gate, stop it intentionally, or tell the user that manual edits will invalidate
the current run and require re-validation.
