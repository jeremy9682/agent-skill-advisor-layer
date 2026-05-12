# Routing Model

The advisor layer separates three actions:

1. **Use** - load and follow a normal skill because the task directly matches it.
2. **Suggest** - mention a costly or permissioned skill because it may help.
3. **Execute** - run the costly workflow only after explicit user approval.

## Suggestion Is Not Invocation

For high-cost skills, the default behavior is:

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

Then continue the user's main task when possible.

## Explicit Approval

The user must explicitly say to run, start, enable, pair, set up, or launch the
specific workflow. A broad outcome request is not enough.

Examples:

- "Build the whole module across backend/frontend/tests/docs" means suggest
  `huashu-agent-swarm`.
- "Start huashu-agent-swarm for this module" means execute it.
- "Let Claude see this page too" means suggest `gstack-pair-agent`.
- "Pair Claude with this browser session" means execute it.

## Negative Signals

Do not suggest high-cost skills for:

- single-file fixes;
- urgent live incidents where routing text would slow down repair;
- pure product thinking with no execution;
- tasks where the relevant high-cost workflow would increase coordination risk.

