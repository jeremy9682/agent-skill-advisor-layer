# ZCode / Cloud 薄网关

定级仍只认本仓 [`routing-policy.yaml`](../routing-policy.yaml)（经 `agent-run`）。
网关是**现成协议的插座**，不是第二套路由。

不要新写 routing yaml。不要引入 CCR / RouteLLM / Bifrost。额度改道走
[`docs/litellm-proxy.md`](litellm-proxy.md)，与本文正交。

可运行适配在 sibling 仓
[dsh-cursor-codex](https://github.com/jeremy9682/dsh-cursor-codex)
的 `gateway/`、`templates/zcode/`、`skills/zcode-delegate-to-dsh/`。

## 三层不要混

| 层 | 做什么 | 入口 | 不做什么 |
| --- | --- | --- | --- |
| 定级 | task_shape → 席位 / 模型 / effort | `agent-run run auto --task-shape …`，canon = `routing-policy.yaml` | 不在网关里分类任务 |
| 额度 | 同模型多提供方按剩余额度改道 | LiteLLM（可选） | 不替代席位规则 |
| 插座 | 把 ZCode / 编辑器接到已有 worker | MCP / ACP stdio / 本机 CLI | 不新造 HTTP 路由器 |

## 谁打谁

```text
ZCode（本机 MCP 客户端 / shell）
  ├─ MCP stdio ──────────────► dsh-cursor-codex/server/dsh-mcp.mjs
  │                              └─ dsh --profile headless
  ├─ shell ──────────────────► agent-run run auto --task-shape <shape>
  │                              └─ routing-policy.yaml
  └─ shell ──────────────────► cursor-agent acp   （Cursor 当 worker）

ACP 客户端（Zed / JetBrains / 自写 client）
  ├─ dsh --profile acp  /  dsh-acp
  ├─ cursor-agent acp    （官方 Cursor ACP）
  └─ claude-agent-acp    （Claude Agent SDK → ACP）

可选本机 HTTP（仅 loopback）
  └─ coder/agentapi 包一层已有 CLI（PTY→HTTP），不是新路由
```

Cursor 与 Codex **不是** ACP 客户端：它们当 agent（被调用方），不能加载第三方
ACP agent。ZCode 走 MCP 或 shell。方向事实与
[dsh-cursor-codex integration guide](https://github.com/jeremy9682/dsh-cursor-codex/blob/main/docs/integration-guide.md)
一致。

## 如何调用

先 `doctor`，再按通道派活。路径按本机 checkout 替换。

```bash
# 插座自检（不打 Cloud、不读凭据）
node /path/to/dsh-cursor-codex/gateway/local-gateway.mjs doctor

# 1) ZCode / 任意 MCP 客户端 → DSH
#    把 templates/zcode/config.snippet.json 的 mcp.servers.dsh
#    合并进 ~/.zcode/cli/config.json（User）或 <repo>/.zcode/config.json（Workspace）
#    然后在 ZCode 里调 MCP 工具 dsh_delegate / dsh_health

# 2) 无 MCP 时，同一条 headless 命令
dsh --profile headless "<完整自包含任务：仓库路径、目标、约束、验证命令>"

# 3) 要过席位/effort canon：agent-run（不要在网关里猜 task_shape）
agent-run run auto --task-shape ordinary_bug_fix \
  --cwd /path/to/repo \
  "Diagnose this failing test without editing."

# 4) 要 Cursor 当 worker：官方 ACP stdio，不是 HTTP
cursor-agent acp
# 一句话封装（本机已登录的 cursor-agent）：
node /path/to/dsh-cursor-codex/gateway/local-gateway.mjs run --via cursor-acp \
  --cwd /path/to/repo \
  "Say hello in one sentence."

# 5) 可选：用已收藏的 coder/agentapi 把 CLI 包成本机 HTTP（默认 :3284）
#    仅 --allowed-hosts localhost；不要对公网暴露
agentapi server --type=cursor --allowed-hosts localhost -- cursor-agent
```

ZCode skill：把
`dsh-cursor-codex/skills/zcode-delegate-to-dsh` 拷进 ZCode skills 目录
（或用 `$` 点名），agent 会按 MCP → gateway CLI → `dsh`/`agent-run` 的顺序选通道。

`agent-run` 的安装与 journal 边界见
[`docs/provider-orchestration.md`](provider-orchestration.md)。
DSH 侧资产见 [`docs/dsh.md`](dsh.md)。

## Cloud 边界（硬拒绝）

**Cursor Cloud 不是本机网关，也没有可打进 127.0.0.1 的公开 ACP/HTTP 插座。**

| 表面 | 事实 | 本网关 |
| --- | --- | --- |
| `cursor-agent acp` | 本机 stdio JSON-RPC，官方入口 | ✓ 直接复用 |
| Cursor 桌面 MCP | 本机 `~/.cursor/mcp.json` | ✓ ZCode 可 import / 自配同款 dsh MCP |
| Cursor Cloud Agents REST（`https://api.cursor.com/v1/agents`） | 在**远端沙箱**拉起 Cloud agent，面向 GitHub repo | ✗ 打不到本机 `agent-run` / `dsh web :3080` / MCP stdio |
| Cloud ACP | 无。ACP 是进程 stdin/stdout，不是公网 URL | ✗ |
| Dashboard team MCP | 官方 ACP 模式**不**加载 | ✗ |
| 把 Cloud 当 `--via` | 无本地 socket | `local-gateway.mjs` **exit 2**，错误码 `CLOUD_NO_LOCAL_HTTP` |

因此：ZCode 只在**本机**接到 DSH / `agent-run` / `cursor-agent acp`。
不要把 `api.cursor.com` 配成 LiteLLM upstream，也不要写「Cloud HTTP 网关」。
远端 Cloud agent 是另一个产品；要用它，走 Cursor 自己的远程 API，且接受它看不到本机 loopback。

## 直接复用（协议），不搬 runtime

- [Agent Client Protocol](https://github.com/agentclientprotocol/agent-client-protocol) — editor ↔ agent 的 JSON-RPC。
- [`claude-agent-acp`](https://github.com/agentclientprotocol/claude-agent-acp) — Claude Agent SDK 暴露成 ACP。
- Cursor 官方 [`agent acp` / `cursor-agent acp`](https://cursor.com/docs/cli/acp) — 本机 Cursor worker。
- 本地已收藏 [`coder/agentapi`](https://github.com/coder/agentapi)（`/Users/zihan/Projects/agent-orchestration-reference-repos/agentapi`）— 有 CLI 时的 PTY→HTTP；含实验性 ACP IO（`x/acpio`）。
- 本机 [dsh-cursor-codex](https://github.com/jeremy9682/dsh-cursor-codex) MCP（`dsh_delegate`）与 ACP profile（`dsh --profile acp`）。

[`langgenius/mosoo-agent-driver`](https://github.com/langgenius/mosoo-agent-driver)
把 Claude Agent SDK / Codex app-server / ACP 收成一套 Driver 事件。
**只参考协议矩阵，不搬 Durable Object / 沙箱 runtime。**

## 明确不做

- 新 `routing-*.yaml` 或改 `routing-policy.yaml` 的 `task_shapes`
- CCR / RouteLLM / Bifrost / cli-agent-gateway 当产品
- 改 LiteLLM example / 生产 `~/.dsh/settings.yaml`
- 改 deepseek-harness `subagent-command-code`
- 把 MCP/ACP 绑到非 loopback 端口
- 在任务文本里放 API key

## 验证

```bash
node /path/to/dsh-cursor-codex/gateway/local-gateway.mjs doctor
agent-run doctor
dsh --version
cursor-agent acp   # 应占用 stdio；Ctrl-C 退出。不要对 Cloud 做同样的事
```

`doctor` 只报告二进制路径、版本、Cloud 不可达。不打印账号、token、环境变量值。
