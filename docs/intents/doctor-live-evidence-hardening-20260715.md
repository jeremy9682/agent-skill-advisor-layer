## Intent

**Status: Phase 2 / serial canary — CANCELLED (2026-07-15)**

User goal (updated):
日常只需 **可恢复的 sessions / journal / ledger** 即可；**不要求** model-identity
取证仪式，也 **不继续** live-evidence hardening / doctor 全绿刷绿。

Cancelled explicitly:
- Phase 2 定向串行 canary（sonnet / opus / terra / sol / grok 等）
- 「doctor 必须全绿」作为日常或 ship 前置条件
- 任何以刷 `model_observed` / live-evidence TTL 为目的的后续 hardening

Do **not** continue this intent as backlog. Phase 1 landing (merged via
`59cfb16` / PR #16) remains historical record only.

---

## Original intent (historical)

Goal:
让 `agent-run doctor` 成为可信的**任务导向健康面**：能区分「能跑 / 身份已验证 / 策略禁用 / 超时阶段」，并修复 Codex 经 wrapper 的可观测性与超时根因；不再把「12 路由仪式性全绿」绑在 Cursor docs 接线上。

User-facing outcome:
- 终审 / ship 时能看清**本任务所需 route**是否 ready。
- Codex 超时能区分 startup / first-event / idle / total，而不是一律 exit 124 + 空 stdout。
- Claude / Codex / Cursor journal 能写入可核验的 `model_observed`（无法取证则保持 unknown，禁止用 requested 冒充）。

In scope (Phase 1 — done):
1. Session adapter：从原生 transcript / `--json` 流事件提取 `model_observed`（claude、codex、cursor）。
2. Codex adapter：消费 `codex exec --json`；记录 `process_started_at` / `first_provider_event_at` / `last_progress_at` / `turn_completed_at`；超时分类。
3. Doctor：`ready | degraded | blocked | disabled`；按 `--task-shape` / route 展示 required vs optional；`reviewer_graph` 对缺边给出可行动原因。
4. Fixture 测试覆盖：exit0+已识别模型、exit0+unknown、首事件前超时、idle 超时、策略禁用、requested≠observed。

Out of scope (unchanged):
- 新建 Cursor seat / 第二协调平面 / 状态 dashboard / transcript 仓库。
- 强迫每个 IDE 原生聊天回合走 `agent-run`。
- 放松 `fail_closed` 或用 `model_requested` 伪造 `model_observed`。
- 用 `.mdc` 修 Cursor Voice/STT。
- YunChouAI 产品代码 / 生产环境。
- 全局 ledger 历史坏事件 bulk migration（可另立）。

Deliberate tradeoffs:
- Cursor 接线（YunChouAI PR #841）按静态 rules + receipt 验收已收口；本 intent 只治 **agent-run 证据层**。
- 先修 adapter 取证与超时遥测；「全路由绿」不是目标（且已取消继续刷绿）。

Constraints:
- 不得购买/升级 Grok/Codex credit 来刷绿。
- 业务派发默认仍走 `agent-run`；原生 `codex exec` 仅诊断用。
- 与 `routing-policy.yaml` fail-closed / 独立性规则保持一致。

Task shape: standard_feature

Risk zone: ordinary (governance tooling; no flip-list product paths)

Model seats (historical Phase 1):
- direction: claude-direction
- landing: claude-landing 或 codex-landing（adapter 实现）
- final-review: 跨家族（codex-final-review 或 grok）；不得同席自审

Effort budget: high (Phase 1 only)


## Phase 1 landing (2026-07-15) — MERGED

Implemented and merged (`59cfb16`):
- `model_observed` parsers for Claude / Codex session JSONL (+ Cursor reason field)
- Codex `exec --json` streaming with `stage_telemetry` and `timeout_first_event|idle|total|startup`
- Doctor statuses: `ready|degraded|blocked|disabled` + `task_focus` + `reviewer_graph_gaps`
- Fixture tests green (`tests/test_agent_provider_run.py`)
- Live Codex canary (historical): exit 0, `model_observed=gpt-5.6-terra`, stage telemetry populated (~20s)

## Phase 2 — CANCELLED

~~Still deferred: serial multi-model canary sweep / dual-seat final review of this landing PR.~~

**Cancelled by user decision 2026-07-15:** recoverable sessions/ledger is enough;
do not continue live-evidence hardening or doctor-green ritual. No open doctor
ledger slug; no further canary sweep backlog.
