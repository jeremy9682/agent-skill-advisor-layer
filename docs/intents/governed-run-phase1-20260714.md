# Intent: GovernedRun Phase 1

## 用户目标

把 Cursor 从只支持 `auto-undisclosed` 的特殊入口升级为可发现模型能力的正式
provider；让 `routing-policy.yaml` 成为唯一运行时 routing canon；新增
`route-doctor`；并为每次运行生成隐私最小化的 Instruction BOM（I-BOM）。

## 公开 seam

1. `agent-run discover`：返回 provider 安装状态与分层模型能力事实。
2. `agent-run routes` / `agent-run doctor`：只从 routing canon 解析路线并解释
   capability、独立性、quota/cooldown 证据和阻塞原因。
3. `agent-run run` 的 journal receipt：包含 canon/binding/I-BOM digest，不包含
   prompt、response、credential 或完整 instruction 正文。

测试只跨以上 seam；provider 命令和 Cursor model parser 是内部 adapter。

## 约束

- `routing-policy.yaml` 是唯一 route source；`agent-providers.yaml` 只描述
  provider adapter、capability discovery、session、billing guard、skills 和
  journal。
- 动态模型目录是 `catalog-listed` 证据，不等于 live entitlement；只有真实
  canary/run receipt 才能证明 `live-run-verified`。
- Cursor broker 的 family 必须按具体 model 解析。`cursor-grok-*` 属于 xAI；
  `auto` 为 undisclosed，不得满足 cross-family blind review。
- 保留 provider-native transcripts；统一的 journal 只保存 pointer、hash 和
  redacted evidence。
- 不修改账号订阅、充值、auto top-up、credential 或 provider billing 设置。
- 不触碰当前 worktree 中与本任务无关的已有修改。

## 刻意取舍

- 保留 `agent-run run auto --task-shape ...` 的调用兼容性，但路线从 canon
  编译，不保留 manifest fallback；发现双源时 fail closed。
- `doctor` 默认不发模型请求；live canary 必须显式执行并作为有时效证据。
- I-BOM 记录可观测 instruction provenance；供应商内置 prompt 不可见时记为
  `opaque:<client-version>`，不声称能够完整重放。
- MCP 仅记录 capability digest；不保存 URL、token、env value 或完整配置。
- ledger 继续只管 ownership/checkpoint，不扩展其 10 字段 schema。

## 验收标准

- Cursor provider 能动态发现 `composer-2.5`、`cursor-grok-4.5-*` 等当前账号
  catalog，并能通过显式 `--model` 调用；未知/未列出的 model fail closed。
- manifest 不再含 `routes`；所有现有 route 兼容投影来自
  `routing-policy.yaml`。
- `agent-run doctor` 显示 provider 安装、catalog、recent live evidence、路线
  blockers 与 producer-family -> reviewer-family 图；离线 doctor 不调用模型。
- run receipt 带稳定 I-BOM digest；instruction、skills、canon、adapter 或 MCP
  capability 变化会改变 digest；`read-only` / `execute` 模式也属于 digest，
  正文与秘密不会进入 journal。
- focused tests、全量 tests、Ruff、`py_compile` 通过；最终 diff 由未产出该
  diff 的 Opus 4.8 和 GPT-5.6 Sol/xhigh 独立审核；不可验证模型身份的
  broker 输出不计作终审证据。

## 非目标

- 不引入 LangGraph、Temporal、统一 transcript 数据库或黑盒 LLM Auto Router。
- 不在本阶段实现跨设备同步、quota daemon、provider 自动购买或自动 fallback。
