# External Skill Sources

This file tracks community skills that are allowed in the daily skill fleet but
must stay review-gated. Installers can fetch them, but updates should not be
auto-applied without reading the changed `SKILL.md`.

## Supply-chain pin requirement (Tier-2 item ⑤)

Every **external** skill must carry an immutable identifier so a specific
installed version is reproducible and drift is detectable:

- **Git-backed source** → a commit sha. The skill lives inside a git checkout
  (`git_head` is populated), or its source group is registered below with a
  "Checked main commit".
- **Non-git source** (a stray copy, a plugin/marketplace payload) → a version
  plus an integrity digest. A `tree_hash` alone is **not** accepted as the sole
  identifier — it proves integrity, not provenance.
- **First-party** (`local-manual`, authored here) → exempt.

`scripts/skill_audit.py` reports this unconditionally under `pin_checks`
(baseline). The **hard red gate** is opt-in via `--enforce-pins`, which exits
non-zero on any unpinned external skill. Per the lightweight-first plan, keep
the gate **off** in CI until the baseline reaches zero, then flip it on.

An external skill counts as pinned only via a **fixed commit SHA** — either a
local git checkout (`git_head`) or a registered pin below. A URL + mutable
branch is **not** a pin. `REGISTERED_PINS` in `skill_audit.py` must match this
list of group → SHA:

| Source group | Pinned commit SHA |
| --- | --- |
| mattpocock-skills | `d574778f94cf620fcc8ce741584093bc650a61d3` |
| emilkowalski-skills | `f76beceb7d3fc8c43309cefad5a095a206103a4e` |
| huashu-skills | `35e7cf31328f6de07e5d125bfd094791f84b2352` |
| huashu-design | `0e7ec8aca0058184c1a9e06e57697e84f68a3f0f` |

**Baseline 2026-07-12:** 82 external skills, 77 pinned (via local commit or a
registered SHA above), **5 unpinned** — all stray copies with no git checkout
and no registered pin: `frontend-design` (agents) + 4 in the `gstack` group
(`gstack`, `gstack-upgrade` ×2, `open-gstack-browser`). To clear them: register
the `gstack` and `frontend-design` groups with a SHA (or re-install those copies
from a pinned source), then `--enforce-pins` can go green and move to CI. Trust
tiers and 30/60/90-day re-review stay deferred (maintenance cost before benefit).

## mattpocock/skills

- Source: `https://github.com/mattpocock/skills`
- Branch: `main`
- Checked main commit: `d574778f94cf620fcc8ce741584093bc650a61d3`
- Installed on this host: Codex `~/.codex/skills`, Claude `~/.claude/skills`
- Install command:

```bash
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo mattpocock/skills \
  --path skills/productivity/grill-me skills/productivity/grilling \
  --dest ~/.codex/skills

python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo mattpocock/skills \
  --path skills/productivity/grill-me skills/productivity/grilling \
  --dest ~/.claude/skills
```

### grill-me / grilling

`grill-me` is an explicit wrapper with `disable-model-invocation: true`; it only
delegates to `/grilling`. `grilling` contains the actual model-invoked workflow:
relentlessly interview the user about a plan or design, one question at a time,
look up codebase facts instead of asking for them, and do not enact the plan
until the user confirms shared understanding.

Daily routing should therefore surface `grilling` for natural-language prompts
such as "grill me", "push back", "别顺着我", and "反方审一下". Keep `/grill-me`
available as the explicit user entrypoint.

Review notes:

- Good fit: pre-implementation planning, product/architecture/design pressure
  tests, unclear plans that need decision-tree questioning.
- Not a substitute for: code review, QA, security audit, or ship/release gates.
- Operational risk: low. It has no scripts, tools, or assets.
- Policy: `grill-me` is `explicit-only`; `grilling` is `auto-eligible`;
  source group is `mattpocock-skills`; update policy is `merge-only`.

## emilkowalski/skills

- Source: `https://github.com/emilkowalski/skills`
- Branch: `main`
- Checked main commit: `f76beceb7d3fc8c43309cefad5a095a206103a4e` (2026-07-09)
- Installed on this host: Codex `~/.codex/skills`, Claude `~/.claude/skills`
- Skills: `emil-design-eng`, `review-animations`, `animation-vocabulary`,
  `apple-design`
- Install shape: **symlink into a shared mutable worktree** — cloned to
  `~/Projects/external-skills/emilkowalski-skills`, then symlinked into both
  runtimes (same pattern as `gsap-skills`). This diverges from the
  `skill-installer` copy+pin flow used for `mattpocock/skills`.

```bash
git clone https://github.com/emilkowalski/skills.git \
  ~/Projects/external-skills/emilkowalski-skills
for n in emil-design-eng review-animations animation-vocabulary apple-design; do
  ln -s ~/Projects/external-skills/emilkowalski-skills/skills/$n ~/.claude/skills/$n
  ln -s ~/Projects/external-skills/emilkowalski-skills/skills/$n ~/.codex/skills/$n
done
```

### Update procedure (do NOT blind-pull)

Both runtimes read one worktree, so an unreviewed upstream commit changes agent
behaviour on both sides at once, with no diff review. Required flow:

```bash
R=~/Projects/external-skills/emilkowalski-skills
git -C $R fetch origin
git -C $R diff HEAD..origin/main -- skills/     # read every changed SKILL.md
git -C $R merge --ff-only origin/main           # only after review
# then update "Checked main commit" above
```

Review notes:

- Good fit: motion/animation craft review, gesture and spring design, naming a
  motion effect before prompting for it. Wired in as Stage 2 of the Web UI ship
  gate (`~/.claude/skills/design-systems/SKILL.md`).
- Not a substitute for: visual/UX QA (`design-review`), engineering rules
  (`web-interface-guidelines`), or CJK typography rules.
- Operational risk: low-to-moderate. No scripts, tools, or assets — prose only.
  The risk is prompt-surface, not code execution: four descriptions are injected
  into both runtimes every session.
- **Runtime asymmetry (verified 2026-07-10):** `review-animations` carries
  `disable-model-invocation: true`. Claude Code honors it (never auto-fires).
  **Codex ignores it** — Codex parses only `name` + `description`
  (`.system/skill-creator/SKILL.md:79`) and has no slash commands. Treat
  `explicit-only` as a Claude-side guarantee and a Codex-side convention.
- **Known content conflict:** `apple-design`'s typography section prescribes
  negative tracking on large text. That is a Latin-script rule; the mandatory
  CJK rules forbid it. `design-systems/CJK.md` wins on CJK surfaces.
- Policy: `review-animations` is `explicit-only` (Claude-enforced); the other
  three are `auto-eligible`; source group is `emilkowalski-skills`; update
  policy is `review-then-ff-only`.
