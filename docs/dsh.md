# Using this governance layer with DeepSeek Harness

The routing canon, gate policy, and schemas in this repository are provider-neutral: they apply to any skill-based agent. This page is the DSH-facing entry point.

## What to use where

| Asset | DSH usage |
| --- | --- |
| [`docs/task-routing.md`](task-routing.md) | Read the routing table as-is; map "edits" and "reviews" onto your DSH seats (`dsh --profile headless` for bounded edits, `dsh --profile acp` for editor-native delegation, `subagent`/`workflow` for fan-out inside one DSH session). |
| [`docs/model-dispatch-matrix.md`](model-dispatch-matrix.md) | Task-type → seat/model/effort, DSH preset map (`standard`/`code`/`minimal`/`cordis`), peak-hour freeze. Canon is still `routing-policy.yaml`. |
| [`docs/litellm-proxy.md`](litellm-proxy.md) | Optional quota gateway. Example config only; do not silently retarget `~/.dsh/settings.yaml`. |
| [`docs/gate-policy.md`](gate-policy.md) | Green evidence before push and "the diff is the gate" apply unchanged to DSH runs. |
| [`docs/development-workflow-standard.md`](development-workflow-standard.md) | Point your project `AGENTS.md` files at the standard instead of copying workflow text into every repo. |
| `skills/skill-advisor/SKILL.md` | The machine-specific edition (personalized suggestion matrix) lives here. The portable edition ships inside [dsh-skill-pack](https://github.com/jeremy9682/dsh-skill-pack) v0.2.0+, installable with `dsh plugin --profile web add @jeremy9682/dsh-skill-pack`. |
| `scripts/skill_audit.py` | Run as-is for Codex/Claude/Cursor skill directories; DSH's own skill roots (`$DSH_HOME/skills`, profile bundles) can be added as additional scan roots. |
| `routing-policy.yaml` / `agent-providers.yaml` | Provider routing is orthogonal to DSH; DSH's own LLM provider and model routing stays in the harness (Web Models page / profile patches). |

## A DSH-native loop, end to end

1. Install the skill pack: `dsh plugin --profile web add @jeremy9682/dsh-skill-pack`.
2. Let `skill-advisor` gate the expensive workflows (overnight runs, multi-session plans, shipping gates) with one-suggestion-plus-approval.
3. Route the work per [`task-routing.md`](task-routing.md): bounded edits to `dsh --profile headless`, fan-out to `subagent`/`workflow`.
4. Close every change the same way: green evidence, then read the diff before merge.

## Related DSH-facing material

- [dsh-cursor-codex](https://github.com/jeremy9682/dsh-cursor-codex) — Cursor/Codex ↔ DSH integration kit; its [`docs/cookbook-fleet-governance.md`](https://github.com/jeremy9682/dsh-cursor-codex/blob/main/docs/cookbook-fleet-governance.md) is the DSH edition of this canon.
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) — the harness itself.
