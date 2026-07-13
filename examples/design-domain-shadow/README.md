# Design-domain shadow prototype

This folder contains a real, runtime-free A/B prototype for the design-skill
selection contract.

- `DESIGN.md` — the visual contract and explicit exclusions
- `apple-cjk-task.yaml` — the explicit task contract consumed by the selector
- `apple-cjk-selection-record.yaml` — the generated shadow-mode selection record
- `apple-cjk-ab.html` — the interactive comparison

Generate the auditable record, then open the HTML file directly in a modern
desktop browser:

```bash
python3 scripts/design_shadow_select.py \
  --input examples/design-domain-shadow/apple-cjk-task.yaml \
  --output examples/design-domain-shadow/apple-cjk-selection-record.yaml
```

The standalone HTML needs no network request, build step, framework, external
font, or runtime router. Regenerating the governance record requires Python 3
and PyYAML, matching the repository audit/test environment.

## What to test

1. Compare **A** (labeled legacy Carbon × CJK structure) with **B** (labeled
   Apple-inspired visual contract).
2. On side B, switch the “经营概览 / 待办事项” segmented control. The content
   updates immediately and the selected state is announced to assistive tech.
3. Open “查看今日摘要”. The anchored sheet traps focus, closes with its close
   button, the backdrop, or `Escape`, and returns focus to the trigger.
4. Use `Tab` to verify visible focus rings.
5. Emulate `prefers-reduced-motion`, `prefers-reduced-transparency`, and
   `prefers-contrast` to verify their fallbacks.

The phrase “Apple-inspired” is intentional: this is an independent web visual
contract, not an official Apple interface or endorsement.
