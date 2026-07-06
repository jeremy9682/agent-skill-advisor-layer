# Intent Statement Schema

Use this schema before code review, release gates, or any workflow that judges a
diff.

```markdown
## Intent

Goal:

User-facing outcome:

In scope:

Out of scope:

Deliberate tradeoffs:

Constraints:

Verification expected:
```

## Guidance

- Write the user's objective, not a diff summary.
- Include constraints or approaches the user ruled in or out.
- Mention surprising implementation choices that are deliberate.
- Keep it short for small fixes and richer for multi-file work.

## Example

```markdown
## Intent

Goal: Add safe retry handling for failed payment webhook delivery.

User-facing outcome: Operators should see fewer duplicate invoice incidents and
failed webhooks should retry without creating extra charges.

In scope: Retry scheduling, idempotency checks, and regression tests around
duplicate webhook delivery.

Out of scope: Replacing the payment provider integration or redesigning the
billing dashboard.

Deliberate tradeoffs: Keep the existing queue backend; add targeted guards
instead of a larger job-system migration.

Constraints: Preserve current event payload format and database schema unless a
minimal migration is required.

Verification expected: Unit test for duplicate delivery, integration test for
retry scheduling, and existing billing tests green.
```
