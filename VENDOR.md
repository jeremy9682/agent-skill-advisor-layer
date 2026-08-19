# Vendor provenance

This repository is the **single public distribution** of our dispatch +
integration code. Clone it; do not also clone `dsh-skill-pack`,
`dsh-cursor-codex`, or a harness fork to run the kit.

Copies below are **vendored** (not git submodules). Paths inside them point at
this clone (`$REPO_ROOT` = the directory that contains `routing-policy.yaml`).
Machine-local home paths are never committed.

## Sources

| Vendored path | Upstream (historical) | Upstream commit when copied | License |
| --- | --- | --- | --- |
| `skills/dsh-dispatch/` and `plugins/dsh-dispatch-pack/` | `dsh-skill-pack` (`skills/dsh-dispatch` + a one-skill Cordis bundle) | `b2b6c7f` | MIT, Copyright (c) 2026 Jeremy Liu |
| `gateway/local-gateway.mjs`, `gateway/local-gateway.test.mjs` | `dsh-cursor-codex/gateway/` | `4488064` | MIT, Copyright (c) 2026 Jeremy Liu |
| `server/dsh-mcp.mjs` | `dsh-cursor-codex/server/dsh-mcp.mjs` | `4488064` | MIT, Copyright (c) 2026 Jeremy Liu |
| `skills/zcode-delegate-to-dsh/`, `templates/zcode/` | `dsh-cursor-codex` skills/templates | `4488064` | MIT, Copyright (c) 2026 Jeremy Liu |
| `plugins/dsh-llm-cursor-acp/` | `dsh-cursor-codex/cursor-provider` (src + dist, no `node_modules`) | `4488064` | MIT, Copyright (c) 2026 Jeremy Liu; third-party notices in that directory |

Each vendored tree keeps its original MIT copyright notice. This repository is
also MIT (`LICENSE`).

Not vendored (third-party runtimes you install yourself):

- Official DeepSeek Harness: `npm i -g @deepseek-ai/dsh`
- LiteLLM, Cursor Agent CLI, Claude Code CLI, Codex CLI
- Optional Command Code CLI if you opt into DSH's `subagent-command-code` (see `docs/harness-opt-in.md`)

Do not copy the rest of `dsh-skill-pack` (handoff, triage, teach, …). Those
skills are unrelated to dispatch. Do not vendor `deepseek-harness`.
