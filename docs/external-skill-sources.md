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
| mattpocock-skills | `391a2701dd948f94f56a39f7533f8eea9a859c87` |
| emilkowalski-skills | `f76beceb7d3fc8c43309cefad5a095a206103a4e` |
| huashu-skills | `35e7cf31328f6de07e5d125bfd094791f84b2352` |
| huashu-design | `0e7ec8aca0058184c1a9e06e57697e84f68a3f0f` |

**Enforcement points** (all local — GitHub CI is NOT one: the runner's home
has no skills, so `--enforce-pins` there would be vacuously green):
- weekly `router_selftune.py` report + notification now includes the pin gate
  (fail-closed on errors);
- a launchd/cron weekly fallback can guarantee the run when the primary scheduler
  misses (ISO-week dedupe makes double-runs harmless);
- the documented audit command in CLAUDE.md carries `--enforce-pins`.

Trust tiers and 30/60/90-day re-review stay deferred (maintenance cost before
benefit).

## mattpocock/skills

- Source: `https://github.com/mattpocock/skills`
- Branch: `main`
- Checked main commit: `391a2701dd948f94f56a39f7533f8eea9a859c87`
- Published set: the 21 paths declared in `.claude-plugin/plugin.json` at the
  pinned commit. Directories under `deprecated/`, `in-progress/`, `misc/`, and
  `personal/` are repository content, not members of the published plugin.
- Installed skills:

  - User-invoked upstream: `ask-matt`, `grill-with-docs`, `triage`,
    `improve-codebase-architecture`, `setup-matt-pocock-skills`, `to-spec`,
    `to-tickets`, `implement`, `wayfinder`, `grill-me`, `handoff`, `teach`,
    `writing-great-skills`.
  - Model-invoked upstream: `diagnosing-bugs`, `prototype`, `research`, `tdd`,
    `domain-modeling`, `codebase-design`, `code-review`, `grilling`.
- Install shape: reviewed copy pinned to the commit above. The two previously
  installed productivity skills were byte-identical and retained; the other
  19 were copied from the audited checkout into each runtime without
  overwriting existing directories.
- Reproduction outline (run against an empty destination or filter existing
  names; the installer refuses overwrite):

```bash
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo mattpocock/skills \
  --ref 391a2701dd948f94f56a39f7533f8eea9a859c87 \
  --path <paths-from-.claude-plugin/plugin.json> \
  --dest ~/.codex/skills

python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo mattpocock/skills \
  --ref 391a2701dd948f94f56a39f7533f8eea9a859c87 \
  --path <paths-from-.claude-plugin/plugin.json> \
  --dest ~/.claude/skills
```

### Runtime and routing integration

- Claude honors `disable-model-invocation: true` for the 13 user-invoked
  wrappers. Codex only consumes `name` and `description`; on Codex,
  `explicit-only` is therefore a governance convention reinforced by
  `AGENTS.md` and routing hints, not a runtime-enforced bit.
- The machine routing canon (`routing-policy.yaml`) wins over upstream flow
  prose. In particular, upstream `/implement` or `/code-review` never waives
  the local judgment/landing/final-review seat split, checkpoint ledger, risk
  overlays, or ship gate.
- `research` describes a background-agent workflow. It may be selected only
  when the active runtime permits delegation; otherwise run the same research
  discipline in the current session.
- `ask-matt` is an explicit router for this upstream bundle. The local
  `skill-advisor` remains authoritative for high-cost approval policy.
- `grill-with-docs` is the codebase/documented variant: it composes `grilling`
  with `domain-modeling` and may update `CONTEXT.md`/ADRs. Plain non-code
  pressure tests use the model directly unless the user explicitly enters
  `/grill-me`, `/grilling`, or the grilling workflow.
- Source group is `mattpocock-skills`; update policy is `merge-only`. Updates
  require reading changed skill files, advancing the registered SHA, rebuilding
  the manifest, and rerunning routing and pin checks.

### grill-me / grilling

`grill-me` is an explicit wrapper with `disable-model-invocation: true`; it only
delegates to `/grilling`. `grilling` contains the actual interview workflow:
relentlessly interview the user about a plan or design, one question at a time,
look up codebase facts instead of asking for them, and do not enact the plan
until the user confirms shared understanding.

Daily routing must not surface `grilling` for generic adversarial requests such
as "push back", "别顺着我", "找漏洞", or "反方审一下"; answer those directly.
Enter the interview loop only when the user names `/grill-me`, `/grilling`, or
explicitly asks to run the grilling workflow. Skill-to-skill calls from the
pinned Matt workflows remain allowed; this policy only constrains top-level
entry routing.

Review notes:

- Good fit: pre-implementation planning, product/architecture/design pressure
  tests, unclear plans that need decision-tree questioning.
- Not a substitute for: code review, QA, security audit, or ship/release gates.
- Operational risk for the original pair is low. The complete bundle includes
  one inert HITL shell template and workflows capable of writing repo docs,
  issues, PR labels, code, tests, or commits when explicitly invoked; normal
  task authorization and local gates still apply.
- Policy: `grill-me` and `grilling` are `explicit-only` at the top-level entry;
  source group is `mattpocock-skills`; update policy is `merge-only`.
- Codex does not mechanically enforce upstream `disable-model-invocation`.
  This is a local policy-enforced overlay (global instructions and manifest
  classification; the router-hints refinement is a separate follow-up), not
  file-level isolation.

## emilkowalski/skills

- Source: `https://github.com/emilkowalski/skills`
- Branch: `main`
- Checked main commit: `f76beceb7d3fc8c43309cefad5a095a206103a4e`
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
