# Security Policy

## Reporting A Vulnerability

Please open a private security advisory on GitHub or contact the maintainer
privately before publishing exploit details.

## Local Data Handling

`scripts/skill_audit.py` is a local audit tool. It may inspect:

- local skill directories;
- local agent session files for coarse usage estimates;
- local environment variable presence for dependency checks.

It does not upload files, prompts, generated reports, or session content.

Generated reports and manifests can contain local paths. Review them before
sharing publicly.

## Publishing Checklist

Before making a fork or derivative public:

- scan for secrets, tokens, and private keys;
- check Git commit author email addresses;
- avoid committing generated manifests or reports;
- review examples for private project names or local absolute paths;
- run the tests in `tests/`.

