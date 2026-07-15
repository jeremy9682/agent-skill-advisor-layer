## Intent

Goal:
让 `agent-run doctor` 成为可信的**任务导向健康面**：能区分「能跑 / 身份已验证 / 策略禁用 / 超时阶段」，并修复 Codex 经 wrapper 的可观测性与超时根因；不再把「12 路由仪式性全绿」绑在 Cursor docs 接线上。

User-facing outcome:
- 终审 / ship 时能看清**本任务所需 route**是否 ready。
- Codex 超时能区分 startup / first-event / idle / total，而不是一律 exit 124 + 空 stdout。
- Claude / Codex / Cursor journal 能写入可核验的 `model_observed`（无法取证则保持 unknown，禁止用 requested 冒充）。

In scope:
1. Session adapter：从原生 transcript / `--json` 流事件提取 `model_observed`（claude、codex、cursor）。
2. Codex adapter：消费 `codex exec --json`；记录 `process_started_at` / `first_provider_event_at` / `last_progress_at` / `turn_completed_at`；超时分类。
3. Doctor：`ready | degraded | blocked | disabled`；按 `--task-shape` / route 展示 required vs optional；`reviewer_graph` 对缺边给出可行动原因。
4. Fixture 测试覆盖：exit0+已识别模型、exit0+unknown、首事件前超时、idle 超时、策略禁用、requested≠observed。
5. 定向串行 canary：sonnet / opus / terra / sol（及启用的 grok）；Fable 在 disabled 期间不要求刷绿。

Out of scope:
- 新建 Cursor seat / 第二协调平面 / 状态 dashboard / transcript 仓库。
- 强迫每个 IDE 原生聊天回合走 `agent-run`。
- 放松 `fail_closed` 或用 `model_requested` 伪造 `model_observed`。
- 用 `.mdc` 修 Cursor Voice/STT。
- YunChouAI 产品代码 / 生产环境。
- 全局 ledger 历史坏事件 bulk migration（可另立）。

Deliberate tradeoffs:
- Cursor 接线（YunChouAI PR #841）按静态 rules + receipt 验收已收口；本 intent 只治 **agent-run 证据层**。
- 先修 adapter 取证与超时遥测，再谈「全路由绿」；绿是结果不是目标。

Constraints:
- 不得购买/升级 Grok/Codex credit 来刷绿。
- 业务派发默认仍走 `agent-run`；原生 `codex exec` 仅诊断用。
- 与 `routing-policy.yaml` fail-closed / 独立性规则保持一致。

Verification expected:
- A/B：原生 `codex exec --json "只回复 OK"` 与 `agent-run run codex …` 同 prompt 对比有书面结论。
- `agent-run doctor --repo <slug> --task-shape judgment`（或等价）对 required route 给出非「全 blocked 无解释」的输出。
- 至少一条 Claude + 一条 Codex journal：`exit_code=0` 且 `model_observed` ≠ unknown（或明确记录取证失败原因）。
- 相关 pytest 绿；不放宽 fail-closed 语义。

Task shape: standard_feature

Risk zone: ordinary (governance tooling; no flip-list product paths)

Model seats:
- direction: claude-direction
- landing: claude-landing 或 codex-landing（adapter 实现）
- final-review: 跨家族（codex-final-review 或 grok）；不得同席自审

Effort budget: high

Scale gates: plan gate (本 intent) → final diff review → focused verification (doctor + canary)
