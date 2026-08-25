# Skill Advisor Layer

**Model dispatch canon + local integration kit.** One public repository: clone it, run `./scripts/install.sh`, and use the routing policy, `agent-run-dispatch`, DSH plugins, and ZCode/MCP gateway from this tree. You do **not** clone our other repos.

[中文说明](README.zh-CN.md)

## Quick Start

**Required:** a Cursor **or** Claude **or** Codex subscription (the CLI you actually call). Optional: official DeepSeek Harness. **LiteLLM is not required** and is not on this path.

```bash
git clone https://github.com/jeremy9682/agent-skill-advisor-layer.git
cd agent-skill-advisor-layer
./scripts/install.sh
```

`install.sh` will:

1. Install **this clone's** launcher and ledger helper. If `~/.local/bin/agent-run` is already taken (hard-fail wrapper, or historically Beads `agent_run_beads_bridge.py`), it **leaves that file alone** and installs `~/.local/bin/agent-run-dispatch`. Same rule for the ledger: `agent-ledger-dispatch` when `agent-ledger` is occupied; `agent-ledger` only when that name is free.
2. If `dsh` is on `PATH`, `dsh plugin add` **this clone's** `plugins/dsh-dispatch-pack` and `plugins/dsh-llm-cursor-acp` into both **web** and **headless**, then symlink `@deepseek-ai/schemastery` and `@deepseek-ai/dsh-skill-filesystem` from the installed DSH `node_modules` into **gitignored** `node_modules` in this clone.
3. Run `node gateway/local-gateway.mjs doctor`.

### Daily commands (this clone)

**PATH `agent-run` is a hard-fail trap (exit 2).** Do not use it for dispatch. Old Beads orchestration uses `agent-run-beads`. Daily dispatch uses the `-dispatch` names:

```bash
cd <clone>
./scripts/install.sh
agent-run-dispatch routes
agent-run-dispatch doctor --task-shape mechanical
python3 scripts/agent_ledger.py open …  # or agent-ledger-dispatch
python3 scripts/agent_ledger.py claim …
agent-run-dispatch run auto --task-shape mechanical --checkpoint-event "$EVT" --cwd "$PWD" --timeout-seconds 480 '…read-only…'
```

`node gateway/local-gateway.mjs run --via agent-run` is **not** the daily entry: it does not take a checkpoint, and PATH `agent-run` is a hard-fail trap. Set `AGENT_RUN_BIN` to `agent-run-dispatch` (or this clone's `scripts/agent_provider_run.py`) if the gateway must hit this canon.

| Command | Typical target | Canon |
| --- | --- | --- |
| `agent-run` | hard-fail wrapper (stderr warning, exit 2) | prevents accidental Beads / Grok 4.5 |
| `agent-run-beads` | Beads bridge → `~/.agent-skill-advisor-layer-governance-clean` | Grok 4.5; old Beads orchestration |
| `agent-run-dispatch` | this clone `scripts/agent_provider_run.py` | Grok 4.6 + `stage_gate` |
| `agent-ledger` | often the same governance-clean tree | leave it if occupied |
| `agent-ledger-dispatch` | this clone `scripts/agent_ledger.py` | this clone's ledger helper |

Do not edit the governance-clean git tree from this install.

### Headless plugin peers

If `dsh --profile headless` fails to boot (`Cannot find module '@deepseek-ai/schemastery'` or `@deepseek-ai/dsh-skill-filesystem`):

1. Install official DSH: `npm i -g @deepseek-ai/dsh`
2. Re-run `./scripts/install.sh` (it symlinks those packages from DSH's `node_modules`; links are gitignored)
3. Or, in this clone: `npm install --no-save @deepseek-ai/schemastery @deepseek-ai/dsh-skill-filesystem`

Do not commit `node_modules`. A home-directory manual symlink is not the supported install path.

```bash
node gateway/local-gateway.mjs doctor
```

Official DSH: `npm i -g @deepseek-ai/dsh` — see [docs/harness-opt-in.md](docs/harness-opt-in.md). You do **not** need `dsh-skill-pack`, `dsh-cursor-codex`, or a harness fork. Optional quota proxy (not required): [docs/litellm-proxy.md](docs/litellm-proxy.md) / `examples/litellm/` — copy the example **outside** git; never commit keys. A VPN/DNS interceptor may resolve `api.commandcode.ai` to `198.18.0.123`. `GET /v1/models` must send the LiteLLM master key.

Layout of what this clone ships:

```text
routing-policy.yaml              Machine canon (task_shape → seat/model)
scripts/agent_provider_run.py    agent-run-dispatch launcher
scripts/agent_ledger.py          agent-ledger-dispatch helper
scripts/install.sh               One-shot local install
skills/dsh-dispatch/             Portable dispatch skill
skills/skill-advisor/            High-cost skill suggestion layer
skills/zcode-delegate-to-dsh/    ZCode → local sockets
skills/codex-house-loop/         House Codex loop (Grok + dsh + herdr sol@high)
gateway/                         Thin CLI over dsh / agent-run / cursor-acp
server/dsh-mcp.mjs               MCP stdio (dsh_delegate / dsh_health)
plugins/dsh-dispatch-pack/       Cordis bundle for `dsh plugin add`
plugins/dsh-llm-cursor-acp/      Cursor ACP adapter for DSH (MIT, no node_modules)
templates/zcode/                 MCP config snippet
examples/litellm/                Optional LiteLLM example
docs/model-dispatch-matrix.md    Prose matrix
VENDOR.md                        Where vendored files came from
```

Provenance: [VENDOR.md](VENDOR.md). ZCode / Cloud boundary: [docs/zcode-cloud-gateway.md](docs/zcode-cloud-gateway.md). DSH-facing loop: [docs/dsh.md](docs/dsh.md).

## Why (skill advisor)

Large skill libraries often fail in two ways:

- **Too passive**: useful skills are installed but never suggested unless the
  user remembers their exact names.
- **Too eager**: broad agents load or run too many skills, wasting context and
  creating side effects.

This repo still provides that middle layer:

1. Detect strong signals for high-cost skills.
2. Suggest exactly one relevant workflow.
3. Wait for explicit approval before execution.
4. Stay silent when the task is small, urgent, or unrelated.

Portable workflow standard for teams that want Codex, Claude Code, and other
agents to share the same rules:

- [Skill-first workflow standard](docs/development-workflow-standard.md)
- [Task routing](docs/task-routing.md)
- [Gate policy](docs/gate-policy.md)
- [Intent statement schema](schemas/intent.md)
- [Solution note schema](schemas/solution.md)
- [OSS reference policy](docs/oss-reference-policy.md) — research and prototype work judges technical value first; this shared policy is the sole full text.

## Repository ownership

This public repository is the single governance canon **and** the installable
integration kit: routing policy, provider bindings, schemas, gates, health
inspection, the thin orchestrator adapter, the local gateway, MCP server, and
DSH plugins. The executable DAG scheduler and its package/CI live in a
separate private `agent-run-orchestrator` repository; `orchestrator.lock.json`
pins the exact reviewed commit used by the adapter.

Run evidence stays local. Journals, checkpoint ledgers, provider sessions,
credentials, temporary worktrees, prompts, responses, and review bundles must
not be committed. This split keeps policy reviewable without publishing
provider/runtime internals or creating a second routing canon.

To update the private runtime, review and test its new commit first, then update
only `orchestrator.lock.json` here and run the public adapter and governance
regression suite. Never copy routing policy into the private package.

## Default Routing Targets

The bundled advisor covers these high-cost workflows by default:

| Skill | Suggest when | Default action |
| --- | --- | --- |
| `huashu-agent-swarm` | Large multi-module work that can be parallelized across backend, frontend, tests, docs, and QA | Suggest only |
| `gstack-pair-agent` | Another agent needs shared browser, page, or live QA context | Suggest only |
| `gstack-retro` | End of a week, sprint, deploy, or large repair sequence | Suggest only |
| `gstack-setup-gbrain` | Persistent project brain, gbrain, or MCP-backed memory setup | Suggest only |
| `no-mistakes` | Safe push, release gate, PR/CI validation, or no-mistakes validation | Suggest only |
| `lfg` | Hands-off plan-to-PR implementation pipeline | Suggest only |
| `ship` / `overnight-execution` | Production-facing or long-running autonomous execution | Suggest only |

You can edit `skills/skill-advisor/SKILL.md` if your local skill names differ.

## Skill files for Codex / Claude

For Codex:

```bash
mkdir -p ~/.codex/skills/skill-advisor ~/.codex/skills/dsh-dispatch
cp skills/skill-advisor/SKILL.md ~/.codex/skills/skill-advisor/SKILL.md
cp skills/dsh-dispatch/SKILL.md ~/.codex/skills/dsh-dispatch/SKILL.md
cp -R skills/codex-house-loop ~/.codex/skills/codex-house-loop
```

For Claude Code:

```bash
mkdir -p ~/.claude/skills/skill-advisor ~/.claude/skills/dsh-dispatch
cp skills/skill-advisor/SKILL.md ~/.claude/skills/skill-advisor/SKILL.md
cp skills/dsh-dispatch/SKILL.md ~/.claude/skills/dsh-dispatch/SKILL.md
cp -R skills/codex-house-loop ~/.claude/skills/codex-house-loop
```

Then add the routing snippet from
`examples/AGENTS.codex.snippet.md` to your global or project `AGENTS.md`.

For Claude projects, use
`examples/CLAUDE.snippet.md` and
`examples/CLAUDE.settings.local.example.json` as starting points for project
instructions and project-local `skillOverrides`.

## Usage Pattern

When a strong signal appears, the agent should say:

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

The agent should **not** run the target workflow until the user explicitly says
to run, start, enable, pair, set up, or launch that specific workflow.

## Local Audit

Run:

```bash
python3 scripts/skill_audit.py --write-manifest --report --syntax-check --dry-run-sync
```

The audit script validates skill metadata, classifies call policies, checks
lightweight script syntax, and reports update safety. It is conservative by
design:

- copied skills can sync only when the previous manifest proves there were no
  local edits;
- git-backed or locally modified skills are reported as merge-only;
- high-cost skills are classified as `suggest-confirm`, not auto-run.

## Privacy And Safety

- The audit runs locally.
- The script may inspect local skill folders and local agent session files to
  estimate usage.
- The script does not upload local files, prompts, reports, or session content.
- Generated manifests and reports may contain local paths; do not publish them
  unless you have reviewed them.
- `.gitignore` excludes generated manifests and report JSON files by default.

## QA

```bash
python3 -m py_compile scripts/skill_audit.py
python3 -m pytest tests
node --test gateway/local-gateway.test.mjs
```

See `docs/qa-matrix.md` for black-box prompt cases.

## License

MIT. Vendored files keep their original copyright notices; see [VENDOR.md](VENDOR.md).
