# Apple-inspired + CJK visual contract

This prototype is a routing and visual-composition test. It is **not** an
official Apple interface, an Apple product clone, or a claim of Apple
endorsement.

## Purpose

Compare the same Chinese operations-console content under two deliberately
different contracts:

- **A — Carbon × CJK legacy baseline:** a serious ERP structure built from an
  8px grid, gray layers, square controls, dense rows, and bottom-border inputs.
- **B — Apple-inspired visual contract:** a calm, spatial workspace with one
  continuous canvas, translucent functional chrome, restrained depth,
  continuous-feeling corners, direct labels, and an occasional anchored sheet.

The memorable idea on side B is **“a quiet glass shelf above one sheet of
paper.”** Navigation and temporary controls float; the business content remains
one continuous white surface instead of becoming a mosaic of cards.

## Skill ownership

| Layer | Skill | Active responsibility |
| --- | --- | --- |
| Author | `frontend-design` | Page layout, component composition, HTML/CSS/JS |
| Baseline | `design-systems` | CJK typography and spacing only |
| Overlay | `apple-design` | Transient material, spatial continuity, press feedback, sheet behavior |

`design-systems` does **not** contribute `erp-structure` to side B. That facet
belongs only to the labeled legacy comparison on side A. `apple-design` is an
overlay and is never credited as the page author.

## CJK contract

- Font stack: `PingFang SC`, `MiSans`, `Source Han Sans SC`,
  `Microsoft YaHei`, then platform fallbacks.
- No negative `letter-spacing`, including headings.
- Chinese body copy and primary control labels stay at 13px or larger;
  timestamps, captions, and compact metadata may use 12px.
- Money, dates, percentages, order IDs, and timestamps use tabular numerals.
- CJK weights map to 400 / 500 / 600; no synthetic 510 / 590 weights.
- No fake italic, decorative underline, or Latin-only OpenType features on
  Chinese text.

## Apple-inspired contract

- Use platform blue (`#0a84ff`) for non-text accent marks. Controls carrying
  white text use the darker platform-adjacent action blue (`#0071e3`) so the
  default state clears WCAG AA contrast; pressed state is darker again.
- Use translucent material only for functional floating layers: navigation,
  toolbar, and segmented control. The sheet uses a near-opaque elevated
  material so headless Chromium and GPU-constrained browsers do not leak a
  backdrop-filter compositing layer outside its containing comparison panel.
- Larger transient surfaces receive stronger blur and shadow; materials do not
  stack on top of other translucent materials.
- Corners follow a restrained 12 / 18 / 24px hierarchy rather than applying
  pills everywhere.
- Motion is brief and causal: pointer press feedback, segment content
  replacement, and a source-anchored sheet. Animations affect only `transform`
  and `opacity`, last at most 240ms, and use a strong ease-out curve. Keyboard
  activation and Escape complete the sheet state change immediately.
- The interface remains usable with reduced motion, reduced transparency,
  reduced contrast, forced colors, and keyboard-only navigation.

## Explicit exclusions

- No purple gradient, mesh gradient, glowing orb, glass-card pile, oversized
  marketing headline, fake device frame, or dashboard-card collage.
- No Carbon structural rules on side B.
- No negative tracking borrowed from Latin Apple typography guidance.
- No logo copying, San Francisco font redistribution, or official Apple assets.
