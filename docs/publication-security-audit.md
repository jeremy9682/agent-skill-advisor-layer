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

## Git History

Check all commits, not just the working tree:

```bash
git log --all --format='%H %an <%ae> %cn <%ce> %s'
git grep -nE '(/Users/|gho_|sk-|PRIVATE KEY|password|secret|token)' $(git rev-list --all)
```

If a private email address appears in commit metadata, rewrite the commit before
publishing and push with `--force-with-lease` while the repository is still
private.

## Remote

Confirm the remote visibility only after the checks pass:

```bash
gh repo view OWNER/REPO --json visibility,url
```

