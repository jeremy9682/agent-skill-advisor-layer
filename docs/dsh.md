# Using this governance layer with DeepSeek Harness

The routing canon, gate policy, and schemas in this repository are provider-neutral. This page is the DSH-facing entry point. **Clone this repository**; do not clone a separate skill-pack or cursor-codex repo.

Daily dispatch from this clone is `agent-run-dispatch` / `agent-ledger-dispatch` (see the README Quick Start). PATH `agent-run` may be a Beads wrapper onto a different tree; do not use it for this canon. `install.sh` links `@deepseek-ai/schemastery` and `@deepseek-ai/dsh-skill-filesystem` from the official DSH install into gitignored `node_modules` so headless plugin boot can resolve those peers.

## What to use where

| Asset | DSH usage |
| --- | --- |
| [`docs/task-routing.md`](task-routing.md) | Read the routing table as-is; map "edits" and "reviews" onto your DSH seats (`dsh --profile headless` for bounded edits, `dsh --profile acp` for editor-native delegation, `subagent`/`workflow` for fan-out inside one DSH session). |
| [`docs/model-dispatch-matrix.md`](model-dispatch-matrix.md) | Task-type → seat/model/effort, DSH preset map (`standard`/`code`/`minimal`/`cordis`), peak-hour freeze. Canon is still `routing-policy.yaml`. |
| [`docs/litellm-proxy.md`](litellm-proxy.md) | Optional quota gateway. Example config only; do not silently retarget DSH settings. |
| [`docs/gate-policy.md`](gate-policy.md) | Green evidence before push and "the diff is the gate" apply unchanged to DSH runs. |
| [`docs/development-workflow-standard.md`](development-workflow-standard.md) | Point your project `AGENTS.md` files at the standard instead of copying workflow text into every repo. |
| `skills/skill-advisor/SKILL.md` | High-cost suggestion matrix (Codex/Claude copy, or DSH via the dispatch pack if you add that skill yourself). |
| `skills/dsh-dispatch/SKILL.md` | Portable dispatch table. Install for DSH with `dsh plugin add ./plugins/dsh-dispatch-pack` (or `./scripts/install.sh`). |
| `plugins/dsh-llm-cursor-acp/` | Cursor ACP model adapter. Required on **headless** as well as web if the default provider is `cursor-acp`. |
| `scripts/skill_audit.py` | Run as-is for Codex/Claude/Cursor skill directories; DSH's own skill roots can be added as extra scan roots. |
| `routing-policy.yaml` / `agent-providers.yaml` | Provider routing is orthogonal to DSH's own Models page / profile patches. |

## A DSH-native loop, end to end

1. Install official DSH if needed: `npm i -g @deepseek-ai/dsh`. See [harness-opt-in.md](harness-opt-in.md).
2. From this clone: `./scripts/install.sh` (adds the dispatch pack and Cursor ACP adapter to **web** and **headless**, then links headless peer packages from the installed DSH `node_modules`).
3. Let `dsh-dispatch` pick seat/model/preset; let `skill-advisor` gate expensive workflows with one-suggestion-plus-approval.
4. Route bounded edits to `dsh --profile headless` or `node gateway/local-gateway.mjs run --via dsh …`.
5. Close every change the same way: green evidence, then read the diff before merge.

## Related

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) — the official runtime (not vendored here).
- [VENDOR.md](../VENDOR.md) — which files in this clone came from our older integration repos.
