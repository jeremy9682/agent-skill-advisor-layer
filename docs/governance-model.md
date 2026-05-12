# Governance Model

This repo treats skills as managed capabilities, not loose prompt files.

## Inventory

Run:

```bash
python3 scripts/skill_audit.py --write-manifest --report --syntax-check --dry-run-sync
```

The manifest records:

- runtime root;
- skill name and path;
- frontmatter validity;
- description length;
- source group;
- update policy;
- call policy;
- script syntax checks;
- dependency checks.

## Call Policies

- `auto-eligible`: low-cost skill that may be used when relevant.
- `manual-confirm`: externally visible or operational skill; be careful.
- `suggest-confirm`: high-cost skill; suggest first, execute only after
  approval.
- `router`: routing or advisory skill.
- `explicit-only`: disabled for model invocation by metadata.

## Update Policies

- `auto-sync-if-clean`: copied skills may sync only when the previous manifest
  proves there were no local edits.
- `merge-only`: git-backed or locally modified skills must be reviewed and
  merged manually.
- `source-managed`: symlinked skills are controlled by their source tree.
- `manual-only`: local/manual skills are never overwritten automatically.

## Claude Code

Use project-local `.claude/settings.local.json` `skillOverrides` to reduce
visibility of broad or costly skills:

- `on`
- `name-only`
- `user-invocable-only`
- `off`

Prefer project-local reduction over global `off`.

## Codex

Codex does not currently expose an equivalent per-skill lifecycle switch in the
same way. Use project/global `AGENTS.md` routing rules plus the audit manifest.

