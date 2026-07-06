# Syncing The Standard

The canonical standard lives in this repository. Local agent files should be
thin entrypoints that link back here.

## Install Snippets

Codex:

```bash
mkdir -p ~/.codex/skills/skill-advisor
cp skills/skill-advisor/SKILL.md ~/.codex/skills/skill-advisor/SKILL.md
```

Then add [`examples/AGENTS.codex.snippet.md`](../examples/AGENTS.codex.snippet.md)
to a global or project `AGENTS.md`.

Claude Code:

```bash
mkdir -p ~/.claude/skills/skill-advisor
cp skills/skill-advisor/SKILL.md ~/.claude/skills/skill-advisor/SKILL.md
```

Then add [`examples/CLAUDE.snippet.md`](../examples/CLAUDE.snippet.md) to a
global or project `CLAUDE.md`.

## Drift Check

At the start of a new machine or team setup:

```bash
git -C ~/Projects/agent-skill-advisor-layer pull --ff-only
python3 ~/Projects/agent-skill-advisor-layer/scripts/skill_audit.py \
  --write-manifest --report --syntax-check --dry-run-sync
```

If local `AGENTS.md` or `CLAUDE.md` contains long copied versions of these
rules, replace them with the snippets so the standard has one canonical source.
