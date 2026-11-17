---
name: codex-house-loop
description: >-
  Runs this machine's Codex orchestration loop: Cursor Grok orchestrates,
  off-peak local dsh implements, independent herdr Codex gpt-5.6-sol@high
  stage-gates (review-only). Use when the user says 复审, Mode 3, 派 Codex,
  herdr 审, 按我们 Codex 流程, or asks to review/ship on the house Codex path.
---

# codex-house-loop — 本机近期 Codex 编排习惯

操作层。**不改** `routing-policy.yaml`，**不重写** `dev-workflow` Mode 0–4。

命令 / 模板 / 验收见下。herdr 与 ledger 示例见 [reference.md](reference.md)。短例见 [examples.md](examples.md)。

## 分层（禁止第三套 canon）

| 层 | 信谁 | 本 skill 只补什么 |
|---|---|---|
| `~/Projects/agent-skill-advisor-layer/routing-policy.yaml` | `task_shapes` / 席位 / `final_review.stage_gate` / `agent-run-dispatch` 校验 | 不复述整张路由表 |
| 仓内 `docs/task-routing.md`、`docs/development-workflow-standard.md` | 可移植三席与门禁 | 不另写 portable 教义 |
| `~/.claude/skills/dev-workflow` | Mode 0–4 仪式（探雷 / 调研 / 派活 / 审查 / ship） | 不复制 Mode 正文 |
| 本 skill | **这台机器最近怎么开席**：Grok 编排、dsh 落地、herdr Codex 阶段门 | 开席命令、prompt、验收、禁止项 |

冲突时：

- 形状 / 谁可以写：YAML。
- Mode 0–4 先做什么：`dev-workflow`。
- **人怎么把 Codex 阶段门跑起来**（herdr / 峰谷 / 402 / 聊天≠ApprovalGrant）：本 skill。
- YAML overlay 把禁区终审抬到 `xhigh`、或 Codex 出品改派 `claude-fable-5`：**人操 herdr 阶段门仍 `gpt-5.6-sol` @ `high`**；对抗审查可加严。不要为对齐 prose 去改 YAML。
- `dsh-orchestration-loop` 若写「Cursor 直接派 Sol subagent」：以本 skill 为准——**禁止** Cursor 套餐 `gpt-*` / `*codex*`。

本仓无 `docs/runbook/multi-agent-orchestration.md`。不要发明一份。

## 触发

用户说：复审 / Mode 3 / 派 Codex / herdr 审 / 按我们 Codex 流程 / review 当前 diff / 阶段门。

不要用：一行本地改、只读问答、用户明确禁止派 Codex。

## 席位

| 席 | 谁 | 做 | 不做 |
|---|---|---|---|
| 编排 | Cursor 主会话 **Grok** | 拆范围、派 dsh、开 herdr 审、收 FINDING、开 PR | 不烧 Cursor 套餐 Claude/GPT/Codex；不自审自己刚写的大 diff |
| 实现 | 空闲时段本机 dsh `127.0.0.1:3080` `goal.create` | 改允许文件集、开 PR、停 | 不终审、不自报 GO、不合入 |
| 对抗 | 独立 Codex challenge（方法论，**不是**终审席） | 大 diff 或禁区先找破点 | 不替代 sol 阶段门 |
| 阶段门 | 独立 herdr `--kind codex` · `gpt-5.6-sol` @ **high** | 只审不写；双轴 Standards + Spec | producer 自审；overlay 升 xhigh；fable 当终审 |

额度 **402**（dsh 不可用）或北京高峰：实现改由**本编排席**落地。仍禁止 Cursor 套餐 Claude/GPT。

## 0. 预检

```bash
TZ=Asia/Shanghai date +%H:%M
git rev-parse --show-toplevel
git branch --show-current
git diff --stat origin/main...HEAD
```

- 无 diff → 停。
- 在 `main`/`master` 上要写功能 → 先开 feature 分支 + worktree。功能**不直推** `main`。
- **一工作树 = 一分支 = 一 session = 一 PR**（YunChou `AGENTS.md`）。
- 规模：`≤5` 文件 **且** `≤200` 行 → 小；否则大。
- 禁区（`permissions` / `doctype` / money / migration / PII / 不可逆写）：只加严**对抗**，阶段门仍 sol@high。
- 聊天里的 OK / lgtm / 继续 **≠** ApprovalGrant。禁区落地或放行 P1 必须用户点名文件或触发器。

## 1. 实现（需要写码时）

高峰（北京 **09:00–12:00、14:00–18:00**）：**不要** `pnpm dsh` / `dsh web` / 向 `127.0.0.1:3080` 新建 session、`session.prompt`、`goal.create`。已在跑的不要再 queue。

空闲：按 `dsh-local-dispatch` 开席。实现 prompt 必须写清 SCOPE / 禁止路径 / 开 PR 即停 / **不要自报 GO**。

HTTP 信封不要抄进本文件，读 `~/.claude/skills/dsh-local-dispatch/reference.md`。

## 2. 登记（账本 ≠ herdr）

YunChou / 跨席派发：**`agent-run-dispatch` + ledger**。

- **禁止** PATH `agent-run`（Beads / Grok 4.5 陷阱）。
- herdr **不是**账本。pane 状态不能代替 `agent-ledger-dispatch`。
- 命令见 [reference.md](reference.md)。

## 3. 大 diff：先对抗，再阶段门

`>5` 文件 **或** `>200` 行：先 challenge，再 sol 阶段门。

禁区即使小 diff：对抗加严（effort 可 xhigh），阶段门仍 sol@high。

对抗与终审是**两条轴上的方法论**，不是第二终审席。双轴定义（内容层）：Standards（仓内标准 + Fowler 气味）+ Spec（对照 intent/issue 原文）。不要为此再派一个「Standards 席」。

## 4. 开 Codex 阶段门（只审不写）

1. **新 pane**。不要占 `w1:p1`–`w1:p4`（本机 `coding-agents` TUI）。
2. `herdr agent start <name> --kind codex --pane <new-pane> -- -m gpt-5.6-sol -c 'model_reasoning_effort="high"'`
3. `herdr agent prompt <name> "…"`（模板见下）。
4. **不要信** `idle` / `done`。必须 `herdr agent read <name> --source recent-unwrapped`。
5. Codex upgrade / hooks：**不要 Trust all**。
6. herdr-house：Cursor / Codex 可在 `HERDR_ENV` 未设时调 CLI；假完成（未 login / 未 Trust）仍可能 exit 0。

详细命令：[reference.md](reference.md)。

### 阶段门 prompt

```
[TASK] 只审不写。审查 git diff origin/main...HEAD（无则 origin/master）。
[SEAT] 独立阶段门。你不是 producer。禁止改文件、禁止 commit、禁止 push。
[MODEL] gpt-5.6-sol @ high。不要自己升 xhigh。
[AXES] 两轴分开写，不要合并重排：
  1) Standards — 仓内 AGENTS.md / 文档化标准；气味是判断项不是硬违规。
  2) Spec — 对照 intent/issue 原文：缺需求 / 范围蔓延 / 实现错误。
[FINDINGS]
  - P1 闸门：正确性 / 安全 / 会坏行为的必须先修。
  - pin/CI 清单（pin、workflow 徽章、供应链）单独一节，不要和闸门逻辑搅在一起。
  - P2 建议。
[OUT] GATE: BLOCK | PASS_WITH_P1 | PASS
每条 FINDING 带文件:行号。不要自报产品 GO。
```

### 对抗 prompt（仅大 diff / 禁区）

```
[TASK] 对抗审查，不是终审。只找破点。
[DIFF] git diff origin/main...HEAD
[FOCUS] 运行时失败、权限/数据写错、静默坏数据、范围蔓延。
[OUT] 问题清单。不要修。不要 GATE PASS。
之后仍须独立 sol@high 阶段门。
```

## 5. FINDING 处理

1. **P1 先修闸门**。未清 P1 不 ship。
2. pin/CI 条目另列，不塞进闸门判断。
3. 修完若范围明显超出原 diff：再对抗 + 再 sol，producer 仍不得自审。
4. ~3 轮 sol 不收敛 → 停，交人。可稀疏问顾问；**不得**把 fable 升成终审席。

## 验收

- [ ] 阶段门是**另一席** `gpt-5.6-sol` @ high，且 `agent read` 到全文（不是 idle/done）。
- [ ] 大 diff 有对抗记录，再有阶段门记录。
- [ ] P1 已修或用户对**点名文件**做了 ApprovalGrant。
- [ ] pin/CI 未混进 GATE 逻辑。
- [ ] 功能分支 PR，未直推 `main`。
- [ ] YunChou 跨席：ledger 有 open/claim/close；没用 PATH `agent-run`。
- [ ] 未占 `w1:p1`–`w1:p4`；未 Trust all。

## 禁止

- Cursor 模型选择器或 Cursor Task/subagent 使用 `claude-*` / `gpt-*` / `*codex*` / `cursor-claude-*` / `cursor-gpt-*`
- 高峰新建 dsh；402 时改烧 Cursor Claude/GPT
- producer 自审；fable 当终审；overlay 把阶段门升 xhigh
- 把聊天 OK 当 ApprovalGrant
- PATH `agent-run`；把 herdr 当账本
- 功能直推 `main`；一树多 PR / 一 PR 多 session 抢同一文件集
- 把本 skill 写进 `~/.cursor/skills-cursor/`、YunChouAI、carDealer、yunchou-compiler
- 抄 gstack `/codex` 八股（telemetry、GSTACK REVIEW REPORT、AskUserQuestion）进业务 prompt
