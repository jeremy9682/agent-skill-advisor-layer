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

## What Is Included

```text
skills/skill-advisor/       Routing skill
scripts/skill_audit.py      Local skill inventory and governance audit
examples/                   Codex and Claude configuration snippets
docs/                       Routing, governance, and QA docs
tests/                      Lightweight pytest coverage
```

## Default Routing Targets

The bundled advisor covers these high-cost workflows by default:

| Skill | Suggest when | Default action |
| --- | --- | --- |
| `huashu-agent-swarm` | Large multi-module work that can be parallelized across backend, frontend, tests, docs, and QA | Suggest only |
| `gstack-pair-agent` | Another agent needs shared browser, page, or live QA context | Suggest only |
| `gstack-retro` | End of a week, sprint, deploy, or large repair sequence | Suggest only |
| `gstack-setup-gbrain` | Persistent project brain, gbrain, or MCP-backed memory setup | Suggest only |

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
`examples/CLAUDE.settings.local.example.json` as a starting point for
project-local `skillOverrides`.

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

