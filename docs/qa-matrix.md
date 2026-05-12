# QA Matrix

Use black-box prompts to verify that the advisor layer suggests useful skills
without over-triggering.

## High-Cost Routing

Expected behavior:

| Case | Expected |
| --- | --- |
| Build a large module across backend, frontend, tests, docs, and QA | suggest `huashu-agent-swarm` |
| Let Claude or another agent inspect the same browser/page context | suggest `gstack-pair-agent` |
| End-of-week, sprint, deploy, or multi-fix retrospective | suggest `gstack-retro` |
| Set up persistent project brain, gbrain, or MCP-backed memory | suggest `gstack-setup-gbrain` |
| Small button text change | none |
| Live bug investigation | none for high-cost layer |
| Product thinking only, no development | none |
| Explicitly confirmed swarm launch | execute `huashu-agent-swarm` |

## Design Routing

If you also use a design-first layer such as `huashu-design`, verify:

| Case | Expected |
| --- | --- |
| Mobile/high-fidelity HTML prototype | use `huashu-design` first |
| Pure backend or script bug | none |
| Dashboard information architecture, data visualization, color redesign | use `huashu-design` first |
| Existing design, only bind click handler | none |

## Safety Checks

After QA prompts, verify that no high-cost workflows actually started:

```bash
tmux ls 2>/dev/null | rg -i 'swarm|pair|gbrain|agent' || true
find . -maxdepth 3 \( -name 'current_tasks' -o -name 'agent_logs' -o -name '*swarm*' -o -name '*gbrain*' \)
```

