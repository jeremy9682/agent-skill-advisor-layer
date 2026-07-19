# Skill Advisor Layer

**Proactive routing and governance for high-cost agent skills.**

Skill Advisor Layer helps Codex, Claude Code, and other skill-based agents
notice valuable workflows without silently launching expensive, disruptive, or
permissioned ones.

[中文说明](README.zh-CN.md)

## Why

Large skill libraries often fail in two ways:

- **Too passive**: useful skills are installed but never suggested unless the
  user remembers their exact names.
- **Too eager**: broad agents load or run too many skills, wasting context and
  creating side effects.

This repo provides a small middle layer:

1. Detect strong signals for high-cost skills.
2. Suggest exactly one relevant workflow.
3. Wait for explicit approval before execution.
4. Stay silent when the task is small, urgent, or unrelated.

It also now carries a portable skill-first development standard for teams that
want Codex, Claude Code, and other agents to share the same workflow rules:

- [Skill-first workflow standard](docs/development-workflow-standard.md)
- [Task routing](docs/task-routing.md)
- [Gate policy](docs/gate-policy.md)
- [Intent statement schema](schemas/intent.md)
- [Solution note schema](schemas/solution.md)

## What Is Included

```text
skills/skill-advisor/       Routing skill
scripts/skill_audit.py      Local skill inventory and governance audit
examples/                   Codex and Claude configuration snippets
docs/                       Routing, governance, and QA docs
schemas/                    Intent and solution note schemas
tests/                      Lightweight pytest coverage
```

## Repository ownership

This public repository is the single governance canon: routing policy, provider
bindings, schemas, gates, health inspection, and the thin orchestrator adapter
live here. The executable DAG scheduler and its package/CI live in the separate
private `agent-run-orchestrator` repository; `orchestrator.lock.json` pins the
exact reviewed commit used by the adapter.

Run evidence stays local. Journals, checkpoint ledgers, provider sessions,
credentials, temporary worktrees, prompts, responses, and review bundles must
not be committed to either repository. This split keeps policy reviewable
without publishing provider/runtime internals or creating a second routing
canon.

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

## Install

For Codex:

```bash
mkdir -p ~/.codex/skills/skill-advisor
cp skills/skill-advisor/SKILL.md ~/.codex/skills/skill-advisor/SKILL.md
```

For Claude Code:

```bash
mkdir -p ~/.claude/skills/skill-advisor
cp skills/skill-advisor/SKILL.md ~/.claude/skills/skill-advisor/SKILL.md
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
```

See `docs/qa-matrix.md` for black-box prompt cases.

## License

MIT.
