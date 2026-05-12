---
name: skill-advisor
description: "Suggest high-cost or permissioned skills without executing them. Use this advisor for proactive suggestions: huashu-agent-swarm for large multi-module parallel work; gstack-pair-agent for sharing browser/page context with Claude, OpenClaw, Hermes, Cursor, or another agent; gstack-retro after a sprint/week/large repair/deploy; gstack-setup-gbrain for persistent project brain, gbrain, or MCP-backed memory. Never execute these from a mere outcome request; ask for explicit approval first."
---

# Skill Advisor

Use this skill as a small routing layer for valuable skills that should be
suggested proactively but not executed without approval.

## Rule

Do not run the target skill automatically. Suggest at most one target skill,
explain why in one sentence, then continue the main work unless the user
approves.

A user describing a large desired outcome is not approval to start the target
skill. Approval must explicitly say to run, start, enable, pair, set up, or
launch that specific workflow after the suggestion is made.

If asked whether to execute one of the target skills directly, answer no unless
the user has already explicitly approved that target workflow.

Use this format:

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

## Suggestion Matrix

| Target skill | Suggest when | Do not suggest when |
| --- | --- | --- |
| `huashu-agent-swarm` | Task is large, multi-module, parallelizable, or explicitly asks for multi-agent/swarm execution. | Single-file fix, urgent live incident, unclear requirements, or dirty repo risk dominates. |
| `gstack-pair-agent` | Another agent needs shared browser/page/session context, or browser QA should be handed to Claude/OpenClaw/Hermes/Cursor. | No live browser/page context is involved. |
| `gstack-retro` | End of a sprint/week/large repair sequence, after deploy/QA, or when repeated patterns should be captured. | Work is not done yet, or user needs immediate execution rather than reflection. |
| `gstack-setup-gbrain` | User wants persistent project brain, gbrain, MCP memory, or recurring cross-session context loss is blocking work. | One-off task, existing memory is enough, or setup/install would distract from urgent work. |

## Common Routing Pitfalls

- Large multi-module delivery can still use `autoplan`, but the high-cost
  suggestion is `huashu-agent-swarm`; do not substitute `autoplan` for the
  swarm suggestion.
- "Summarize what we did" can be answered directly, but end-of-week or
  multi-fix engineering reflection should suggest `gstack-retro`, not execute
  it silently.
- `claude-mem:knowledge-agent` helps query memory; it is not the setup workflow
  for persistent project brain. For gbrain/MCP brain setup, suggest
  `gstack-setup-gbrain`.

## Operating Notes

- Suggestion is not invocation.
- Ask for approval before executing any high-cost skill.
- If multiple target skills match, choose the one that removes the biggest
  current bottleneck.
- If no target skill strongly matches, stay silent and proceed normally.
