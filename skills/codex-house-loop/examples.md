# examples — 短路径

## A. 小 diff 复审（≤5 文件且 ≤200 行）

编排席（Grok）未写过该 diff：

1. `git diff --stat origin/main...HEAD` 确认规模。
2. 开 herdr 新 pane → `sg-sol` `--kind codex` · `gpt-5.6-sol` @ high。
3. 贴阶段门 prompt。`agent read`。
4. P1 先修。pin/CI 另记。

编排席就是 producer：跳过自己「主脑直审」，直接步骤 2。

## B. 大 diff Mode 3

1. 对抗：`sg-chal` + 对抗 prompt → `agent read`。
2. 阶段门：`sg-sol` + 阶段门 prompt → `agent read`。
3. 两份报告都在再改 P1。不要让 chal 宣布 PASS。

## C. 禁区（permissions / doctype / money）

1. 用户必须点名路径或触发器。聊天「OK」不够。
2. 对抗可 xhigh。
3. 阶段门仍 `sg-sol` @ high。不派 fable。不因 overlay 升终审 xhigh。

## D. 北京高峰或 dsh 402

1. 不新建 dsh。
2. Grok 编排席自己改允许文件集。
3. 仍开独立 herdr Codex 阶段门。
4. 仍禁止 Cursor 套餐 `claude-*` / `gpt-*`。

## E. YunChou 一树一 PR

```text
worktree /Users/zihan/Projects/YunChouAI/.worktrees/feat-foo
branch   feat/foo
session  一个 dsh session 或一个 herdr 实现席
PR       只从该 branch 开，不推 main
ledger   agent-ledger-dispatch open/claim/close
herdr    只用来盯 Codex/TUI，不当账本
```
