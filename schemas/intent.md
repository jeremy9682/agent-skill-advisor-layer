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

Optional routing fields for non-trivial review, release, or scale decisions:

```markdown
Task shape:

Risk zone:

Model seats:

Effort budget:

Scale gates:
```

## Guidance

- Write the user's objective, not a diff summary.
- Include constraints or approaches the user ruled in or out.
- Mention surprising implementation choices that are deliberate.
- Keep it short for small fixes and richer for multi-file work.
- Omit routing fields for obvious small fixes. Use them when a change touches
  multiple modules, a public interface, data shape, permissions, money, PII,
  deployment, or another restricted-zone trigger.
- `Model seats` should name direction, landing, and final-review ownership. Do
  not let one agent own both landing and final review for a judgment-shaped
  change.
- `Effort budget` is a review-depth signal (`medium-fast`, `high`, or `xhigh`),
  not a provider budget or automatic model dispatcher.
- `Scale gates` should list only gates that must happen: plan gate, blind plan
  review, final diff review, ship gate, or focused verification.

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

Task shape: feature

Risk zone: restricted

Model seats: direction=Claude plan; landing=implementation owner; final_review=Codex xhigh

Effort budget: xhigh

Scale gates: plan gate, blind plan review, final diff review
```
