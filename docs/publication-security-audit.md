# Publication Security Audit

Use this checklist before making the repository public.

## Files

- No generated manifest committed.
- No generated report JSON committed.
- No local session transcripts committed.
- No `.env` or shell history files committed.
- No private project source code committed.

## Content

Scan for:

- API keys and access tokens;
- private keys and certificates;
- passwords and connection strings;
- local absolute paths;
- personal email addresses;
- private customer or project names.

## (a) Current-tree scan

Run these against the working tree before every publication or scrub pass.
Each command should return **no output** on a scrubbed tree; any hit must be
fixed or explicitly accepted with documented rationale before going public.
Patterns use character-class splits (e.g. `[z]ihan`) so this document's own
examples do not match themselves. Test fixtures and this file are excluded
where noted.

```bash
# Personal usernames and private project codenames
git grep -nIiE '[z]ihan|car[d]ealer|yun[ch]ou' -- ':!*.git' ':!docs/publication-security-audit.md'

# Absolute home paths (allow the documented /Users/example test fixtures)
git grep -nIE '/[U]sers/[a-z]+' -- ':!*.git' ':!docs/publication-security-audit.md' \
  | grep -Ev '/Users/example' || true

# Personal email inboxes (not GitHub noreply or git@github.com SSH clone URLs)
git grep -nIiE 'jeremy9682@[g]mail|[a-z0-9._%+-]+@[g]mail\.com' \
  -- ':!*.git' ':!docs/publication-security-audit.md' \
  | grep -Ev 'users\.noreply\.github\.com|git@github\.com' || true

# Exact billing forensics figures (comma-separated millions pattern)
git grep -nIiE '[0-9]{1,3},[0-9]{3},[0-9]{3}\s*/\s*[0-9,]+' \
  -- ':!*.git' ':!docs/publication-security-audit.md'

# Secrets and credentials (exclude test fixtures and this doc)
git grep -nE '(gho_|sk-[a-zA-Z0-9]{20,}|PRIVATE KEY|password\s*=|secret\s*=|token\s*=)' \
  -- ':!*.git' ':!docs/publication-security-audit.md' ':!tests/'
```

Narrow or extend patterns as the fleet grows. Prefer fixing forward in tracked
files over documenting exceptions.

## (b) Git-history scan

Check all commits, not just the working tree:

```bash
git log --all --format='%H %an <%ae> %cn <%ce> %s'

git grep -nE '(/Users/|gho_|sk-|PRIVATE KEY|password|secret|token)' $(git rev-list --all)

# History-specific: personal identifiers that may predate the current tree scrub
git log --all -p | grep -nIiE '[z]ihan|car[d]ealer|yun[ch]ou|jeremy9682@[g]mail|/Users/[a-z]+/'
```

History hits are costlier than tree hits — see section (d).

## (c) Commit-metadata policy

- Use a publication-safe author/committer identity. GitHub's noreply address
  (`<id>+username@users.noreply.github.[c]om`) avoids exposing a personal inbox.
- Set author email **before** the first public push if the repo was ever private
  with a personal email:

```bash
git config user.email "<id>+username@users.noreply.github.com"
git config user.name "Your Display Name"
```

- Never rewrite metadata on commits already shared to a public default branch
  unless you accept the force-push and collaborator disruption cost.
- Pre-commit hooks and CI should not embed machine-local paths into generated
  artifacts that get committed.

## (d) Post-publication remediation

When something already leaked (tree or history):

1. **Scrub forward immediately** — remove or anonymize the sensitive content in
   the current branch; re-run section (a) until clean.
2. **Assess history-rewrite cost** — count affected commits and downstream
   forks/clones. `git filter-repo` or BFG may be warranted for secrets; for
   names/paths the cost/benefit is a product decision.
3. **Document acceptance** — if history rewrite is declined, record what leaked,
   when, why rewrite was skipped, and what compensating controls apply (e.g.
   rotated credentials, no longer valid paths).
4. **Rotate real secrets** — if any credential-class material appeared, assume
   compromise and rotate regardless of scrub success.
5. **Force-push only while private** — if metadata or history must be rewritten,
   do it before widening visibility, then notify collaborators.

## Remote

Confirm the remote visibility only after the checks pass:

```bash
gh repo view OWNER/REPO --json visibility,url
```
