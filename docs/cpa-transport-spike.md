# CPA Transport Spike (Read-Only Only)

Status: **docs-only spike** — no runtime integration until stability items 1–6
(route timeouts, flock serial, killpg, stream attribution, failure classes, and
doctor/routes reporting) are validated.

## Scope

- Optional **model transport** via CLIProxyAPI for an A/B of the **read-only
  judgment seat only**.
- Execute routes, harness tool loops, checkpoint writes, and dual-seal governance **remain
  on native CLI via `agent-run`**.
- CPA is explicitly **not replacing execute or the harness** until items 1–6 are
  validated and a later decision authorizes code integration.

## Out of scope (until explicit founder approval)

- Replacing `claude` / `codex` / `cursor-agent` / `grok` CLI for landing seats.
- Stripping `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` guard without a documented billing
  policy exception.
- Using HTTP 200 as a substitute for journal receipt, I-BOM, or cross-family review binding.

## A/B plan (when enabled)

1. Same frozen prompt + repo cwd.
2. Arm A: `agent-run run auto --task-shape judgment ...` (native CLI, 600s, serial).
3. Arm B: HTTP client → local CPA `/v1/messages` (same model alias), journal records
   `transport: cpa` + endpoint digest only.
4. Compare: wall time, failure_class, attribution clarity, cost path — not just exit 0.

## Decision gate

Proceed only if Arm B improves **attribution or auth rotation** without losing review
independence checks. Otherwise keep CPA as personal IDE sidecar, not production canon.
