# Project Overrides

Use project-local overrides instead of globally disabling broad skills.

Recommended pattern:

- Keep the `skill-advisor` routing skill visible.
- Put costly execution skills in `name-only` or `user-invocable-only`.
- Keep project-critical repair, QA, and design skills fully visible.
- Avoid global `off` unless the skill is truly irrelevant everywhere.

