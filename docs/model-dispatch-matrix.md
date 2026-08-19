# Model dispatch matrix

Single **prose** fact source for coding agents (ZCode / Claude Code / Cursor /
Codex / DSH). Machine canon remains [`routing-policy.yaml`](../routing-policy.yaml);
this page does not invent a second policy YAML.

Verified model ids (2026-08-19, this host):

| Surface | Default Grok 4.6 id | Fast / second shuttle | 4.5 (explicit downgrade only) |
| --- | --- | --- | --- |
| Cursor ACP / chat (`~/.cursor/cli-config.json`) | `grok-4.6` (`effort=high`, `fast=true`) | same id + `fast=true` | `grok-4.5` |
| `cursor-agent models` (agent-run) | `cursor-grok-4.6-high` | `cursor-grok-4.6-high-fast` | `cursor-grok-4.5-high`, `cursor-grok-4.5-high-fast` |
| Grok CLI (`grok models`) | `grok-4.6` (CLI default) | n/a | not listed as a native id; do not assume `grok-4.5` still launches |

Do not invent slugs. If a catalog changes, re-read `cursor-agent models` and
`grok models` before editing the canon.

## Task type → seats

Columns: **exec** (who writes) → **review** → **billing layer** → **degrade** → **forbid**.

| Task type | Exec seat / model / effort | Review | Billing layer | Degrade | Forbid |
| --- | --- | --- | --- | --- | --- |
| `mechanical` / scan / tiny edit | Cursor **Composer 2.5 Fast** (`composer-2.5-fast`, effort `low`, mode `fast`) | Dual-seal when L3/L4 or gated docs; else focused verify | Cursor subscription | Second shuttle: `cursor-grok-4.6-high-fast` / ACP `grok-4.6` fast. Explicit 4.5 only if 4.6 catalog-missing | Flagship (Opus, Sol, Fable, Terra) for the write |
| `ordinary_bug_fix` | **Codex Terra** (`gpt-5.6-terra`, medium/fast) **or** Claude Sonnet | Same-family independent Codex pass allowed (D3); Sol must not both write and review | Codex or Claude login | Sonnet ↔ Terra | Sol as writer **and** reviewer of the same diff |
| `standard_feature` | Direction: Claude **Opus** (medium/careful). Land: Cursor **Grok 4.6** or **DSH Kimi K3** | Sol (stage default `high`; overlay still ratchets to `xhigh`) | Claude login for direction; Cursor or DSH/Command Code for land | Land → Grok 4.6 if DSH peak-hours; direction does not drop below Opus without user waiver | Composer as sole author of behavior |
| `judgment` | Claude **Opus** high/careful | Cross-family blind; disputes → Fable Max | Claude login | Opus stays; do not downshift effort below `high` | Composer; DSH self-GO; Sol both directing and finally reviewing |
| `restricted_zone` | Claude **Opus** high/careful — **no downshift** | Sol **xhigh**. If Codex produced the diff → **Fable** reviews (cross-family). Overlay still applies | Claude + Codex/Fable logins | Fallback reviewer is **provisional only** (`degradation.cross_family_mandatory_items`) | DSH self-reported GO/NO-GO; Composer as final review; PTC/`code` preset; effort downshift |
| `arbitration` | `claude-fable-5` **max**/careful | This seat **is** the tie-break; not a writer | Claude login | None without user waiver | Using the producer family as the arbitrator |
| Parallel subtasks | Composer Fast ∥ Grok 4.6 Fast | Independent review after land | Cursor family | Serial if file-set collides | \>2 concurrent Cursor-family jobs; new bulk DSH in Beijing **09:00–12:00** and **14:00–18:00** |
| Pure retrieval | **Luna** (`gpt-5.6-luna`) / **DeepSeek Flash** / **Gemini Flash** | None (read-only) | Codex / Command Code / Cursor | Flash → Luna if the question needs more context | Flagship writers; spawning DSH sessions |
| Docs / prose | Grok 4.6 **or** Sonnet **or** GLM | Sol only if the doc is a gated/L3+ deliverable | Cursor / Claude / ZCode-GLM | GLM → Grok 4.6 → Sonnet | Composer changing meaning (format-only is OK) |

House rules that still win over this table:

- Landing seat ≠ final-review seat. Direction seat ≠ final-review seat.
- Cursor is a **broker**: review independence follows the **concrete model family** (Grok = xAI, Composer = Cursor, Claude = Anthropic, GPT = OpenAI).
- Claude/Codex cross-seat work goes through `agent-run` (local subscriptions), not Cursor-billed Claude/GPT.
- DSH implementation seats **must not** self-report GO/NO-GO.

## Stage-gate enforcement

`final_review.stage_gate` is consumed by `routing_runtime.validate_stage_gate`
(wired through `agent-run`). Same-family producer + stage final-reviewer is
blocked except D3 (`secondary_final_review` of `ordinary_bug_fix` with no
overlay). Overlay / `cross_family_mandatory` still force the reciprocal family
(Codex producer → Fable). Spawn inherit without `--spawn-explicit-override` is
rejected for final-review routes. Successful review runs close their checkpoint.

`runtime_routes.codex_final_review` effort stays `xhigh` — the stage-gate `high`
floor is not a silent downgrade of that binding.

`runtime_routes.final_review` is the **Grok CLI** second-opinion/review binding
(`grok-4.6` / high). It is not the Sol stage gate. Do not cite one as the other.

## `task_shape` → DSH preset / lane

Preset names are the four built-ins in `dsh-skill-pack` / `dsh-mode-routing`
(`standard`, `code`, `minimal`, `cordis`). Do not invent extra preset ids.
Preset is chosen at **session create**; a live session with output is
`agent-preset-locked` — switch = new session.

| `task_shape` / work | DSH preset (id) | Lane / run mode | Notes |
| --- | --- | --- | --- |
| Default coding, research, review-brain, orchestration | `standard` | `dsh web` session or `dsh --profile headless` for one-shot | 90% of DSH work |
| Mechanical batch across **>5** files, rule-first rewrite | `code` (PTC) | same; write a checkable N-step list | **Forbidden** in restricted_zone |
| Tiny template jobs, script+file, prompt A/B, many short sessions | `minimal` | headless preferred | No compaction — not for overnight |
| Editing DSH itself (custom preset / plugin / runtime) | `cordis` | new session only | Trust level = shell; not business code |
| `ordinary_bug_fix` | `standard` | headless if bounded | Not PTC while still bisecting |
| `standard_feature` land (Kimi K3 / GLM via pi-ai) | `standard` | web session + PR then stop | Peak-hours: do not **create** sessions |
| `judgment` / `arbitration` | — | do **not** use DSH as the seat | Claude Opus / Fable |
| `restricted_zone` | `standard` only | step-by-step, human-visible | Never `code` / `minimal` / `cordis` |
| Pure retrieval | `standard` or skip DSH | read-only tools | Prefer Luna / Flash on Cursor or Command Code |
| Docs format-only | `minimal` or skip DSH | Composer Fast on Cursor is cheaper | |

Peak window (Beijing, from 2026-08-17): **09:00–12:00** and **14:00–18:00** — do not `session.create` / `session.prompt` / `goal.create` for new bulk DSH. Idle including **12:00–14:00** is OK. Already-running peak sessions: do not queue more.

## How agents should use this

1. Classify the task (`mechanical` / `ordinary_bug_fix` / `standard_feature` / `judgment` / `restricted_zone` / `arbitration` / retrieval / docs).
2. Pick exec + review from the table. If a risk overlay trigger fires, ratchet review to `xhigh` and cross-family.
3. If landing on DSH, pick preset from the second table, then follow `dsh-dispatch` / `dsh-local-dispatch` HTTP rules.
4. Quota / endpoint failover is **LiteLLM** (see [`litellm-proxy.md`](litellm-proxy.md)); grading / seats stay in this repo. Do not silently retarget `~/.dsh/settings.yaml`.
