# Solution Note Schema

Project-local solution notes should live in the project repository under
`docs/solutions/`. This shared repository stores the schema, not private project
learnings.

Use a solution note only when the learning is reusable. Do not write one for
obvious edits or routine cleanup.

```markdown
---
title: "<short reusable lesson>"
date: "YYYY-MM-DD"
tags: ["bug", "feature", "review", "ops"]
source_task: "<issue, PR, branch, or short task name>"
applies_to: ["<paths, modules, workflows, or domains>"]
---

# <Title>

## Problem

What failed, slowed work down, or needed a non-obvious decision?

## Root Cause Or Decision

What was the underlying cause, constraint, or durable choice?

## Fix Or Pattern

What worked, and what should a future agent reuse?

## Verification

What evidence proved the fix or decision?

## Future Trigger

When should a future brainstorm, plan, or review read this note?
```

## Quality Bar

Write a solution note when at least one is true:

- a bug root cause was non-obvious;
- a decision reversed an earlier assumption;
- a reusable implementation pattern emerged;
- a gate or review found a class of issue likely to repeat;
- a project-specific command, setup, or workflow saved meaningful time.

Skip it when the only learning is "remember to run tests" or "fix the typo".
