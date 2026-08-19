# Skill Advisor Layer

**模型分派 canon + 本机对接套件。** 一个公开仓库：clone 之后跑 `./scripts/install.sh`，定级策略、`agent-run-dispatch`、DSH 插件、ZCode/MCP 网关都在这棵树里。不必再 clone 我们其它仓。

[English README](README.md)

## Quick Start

**必装：** 已有 Cursor **或** Claude **或** Codex 订阅（以及对应 CLI）。可选：官方 DeepSeek Harness。**LiteLLM 不是必装项**，不在这条路径上。

```bash
git clone https://github.com/jeremy9682/agent-skill-advisor-layer.git
cd agent-skill-advisor-layer
./scripts/install.sh
```

`install.sh` 会：

1. 安装**本 clone** 的启动器与 ledger。若 `~/.local/bin/agent-run` 已被占用（硬失败 wrapper，或历史上的 Beads `agent_run_beads_bridge.py`），**原文件不动**，改为安装 `~/.local/bin/agent-run-dispatch`。ledger 同理：`agent-ledger` 被占用时装 `agent-ledger-dispatch`；只有空闲时才链 `agent-ledger`。
2. 若 `PATH` 上有 `dsh`，对 **web** 和 **headless** 执行 `dsh plugin add`：**本 clone** 的 `plugins/dsh-dispatch-pack` 与 `plugins/dsh-llm-cursor-acp`，再从已装 DSH 的 `node_modules` 把 `@deepseek-ai/schemastery`、`@deepseek-ai/dsh-skill-filesystem` 链到本仓 **gitignore 的** `node_modules`。
3. 跑 `node gateway/local-gateway.mjs doctor`。

### 日常命令（本 clone）

**PATH 上的 `agent-run` 是硬失败陷阱（exit 2），不要拿它做分派。** 旧 Beads 编排用 `agent-run-beads`。日常分派用 `-dispatch` 名字：

```bash
cd <clone>
./scripts/install.sh
agent-run-dispatch routes
agent-run-dispatch doctor --task-shape mechanical
python3 scripts/agent_ledger.py open …  # 或 agent-ledger-dispatch
python3 scripts/agent_ledger.py claim …
agent-run-dispatch run auto --task-shape mechanical --checkpoint-event "$EVT" --cwd "$PWD" --timeout-seconds 480 '…read-only…'
```

`node gateway/local-gateway.mjs run --via agent-run` **不是**日常入口：它不带 checkpoint，而且 PATH `agent-run` 会硬失败。需要网关打到本 canon 时，设 `AGENT_RUN_BIN` 指向 `agent-run-dispatch`（或本 clone 的 `scripts/agent_provider_run.py`）。旧 Beads 编排用 `agent-run-beads`。

| 命令 | 典型目标 | Canon |
| --- | --- | --- |
| `agent-run` | 硬失败 wrapper（stderr 警告后 exit 2） | 防止误走 Beads / Grok 4.5 |
| `agent-run-beads` | Beads 桥 → `~/.agent-skill-advisor-layer-governance-clean` | Grok 4.5；旧 Beads 编排 |
| `agent-run-dispatch` | 本 clone `scripts/agent_provider_run.py` | Grok 4.6 + `stage_gate` |
| `agent-ledger` | 常常也是 governance-clean | 被占用就别动 |
| `agent-ledger-dispatch` | 本 clone `scripts/agent_ledger.py` | 本 clone 的 ledger |

不要去改 governance-clean 那棵 git 树。

### Headless 插件 peer

若 `dsh --profile headless` 启动失败（`Cannot find module '@deepseek-ai/schemastery'` 或 `@deepseek-ai/dsh-skill-filesystem`）：

1. 安装官方 DSH：`npm i -g @deepseek-ai/dsh`
2. 再跑 `./scripts/install.sh`（从 DSH 的 `node_modules` 做 gitignored 链接）
3. 或在本 clone：`npm install --no-save @deepseek-ai/schemastery @deepseek-ai/dsh-skill-filesystem`

不要把 `node_modules` 提交进 git。家目录手工 symlink 不是受支持的安装路径。

```bash
node gateway/local-gateway.mjs doctor
```

官方 DSH：`npm i -g @deepseek-ai/dsh`，见 [docs/harness-opt-in.md](docs/harness-opt-in.md)。**不必**再 clone `dsh-skill-pack`、`dsh-cursor-codex` 或 harness fork。可选额度代理（非必装）：[docs/litellm-proxy.md](docs/litellm-proxy.md) / `examples/litellm/`（配置拷到 git 外；不要提交密钥）。本机 VPN/DNS 可能把 `api.commandcode.ai` 解析到 `198.18.0.123`。`GET /v1/models` 必须带 LiteLLM master key。

本 clone 提供：

```text
routing-policy.yaml              机器 canon（task_shape → 席位/模型）
scripts/agent_provider_run.py    agent-run-dispatch 启动器
scripts/agent_ledger.py          agent-ledger-dispatch 助手
scripts/install.sh               一次安装
skills/dsh-dispatch/             可移植分派 skill
skills/skill-advisor/            高成本 skill 建议层
skills/zcode-delegate-to-dsh/    ZCode → 本机插座
gateway/                         dsh / agent-run / cursor-acp 薄 CLI
server/dsh-mcp.mjs               MCP stdio（dsh_delegate / dsh_health）
plugins/dsh-dispatch-pack/       给 `dsh plugin add` 的 Cordis 包
plugins/dsh-llm-cursor-acp/      DSH 的 Cursor ACP adapter（MIT，无 node_modules）
templates/zcode/                 MCP 配置片段
examples/litellm/                可选 LiteLLM 示例
docs/model-dispatch-matrix.md    散文矩阵
VENDOR.md                        薄层来源
```

来源说明：[VENDOR.md](VENDOR.md)。ZCode / Cloud 边界：[docs/zcode-cloud-gateway.md](docs/zcode-cloud-gateway.md)。DSH 入口：[docs/dsh.md](docs/dsh.md)。

## 为什么需要 skill advisor

装了很多 skill 后，系统通常会走向两个极端：

- **太被动**：有用的 skill 已经安装，但用户不点名就从来不会被建议。
- **太激进**：Agent 一次加载或运行太多 skill，浪费上下文，也可能造成副作用。

这个 repo 仍然提供中间层：

1. 识别高成本 skill 的强触发信号。
2. 只主动建议一个最相关的 workflow。
3. 等用户明确批准后才执行。
4. 小任务、紧急任务、无关任务时保持安静。

可跨 Codex、Claude Code 和其他 agent 共享的 skill-first 标准：

- [Skill-first workflow standard](docs/development-workflow-standard.md)
- [Task routing](docs/task-routing.md)
- [Gate policy](docs/gate-policy.md)
- [Intent statement schema](schemas/intent.md)
- [Solution note schema](schemas/solution.md)
- [OSS 参考政策](docs/oss-reference-policy.md) — 调研与原型阶段按技术价值判断；共享正文只保留在这一处。

## 仓库所有权

本公共仓库是唯一的治理 canon **也是**可安装的对接套件：路由策略、provider 绑定、schema、gate、健康检查、orchestrator 薄适配器、本机网关、MCP、以及 DSH 插件。可执行 DAG 调度器及其 package/CI 放在独立的私有 `agent-run-orchestrator` 仓库；本仓库通过 `orchestrator.lock.json` 精确固定已审核的私有 commit。

运行证据只留本机。journal、checkpoint ledger、provider session、凭据、临时 worktree、prompt、response 和 review bundle 都不得提交。这样既能公开审查治理规则，又不会暴露 provider/运行时内部信息，也不会产生第二套路由真相源。

升级私有运行时时，应先审核并测试新的私有 commit，再只更新本仓库的 `orchestrator.lock.json`，并回归公共 adapter 与治理测试。不要把 routing policy 复制进私有 package。

## 默认覆盖的高成本 Skill

| Skill | 什么时候建议 | 默认动作 |
| --- | --- | --- |
| `huashu-agent-swarm` | 大型、多模块、可并行的任务，例如后端、前端、测试、文档、QA 一起推进 | 只建议 |
| `gstack-pair-agent` | 需要另一个 Agent 共享浏览器、页面或真实 QA 上下文 | 只建议 |
| `gstack-retro` | 一周、一个 sprint、一次部署或大修复序列结束后复盘 | 只建议 |
| `gstack-setup-gbrain` | 配置长期项目脑、gbrain 或 MCP-backed memory | 只建议 |
| `no-mistakes` | safe push、release gate、PR/CI validation 或 no-mistakes validation | 只建议 |
| `lfg` | 从 plan 到 PR 的 hands-off 自主管线 | 只建议 |
| `ship` / `overnight-execution` | 面向生产或长时间自主执行 | 只建议 |

如果你的本地 skill 名称不同，可以改 `skills/skill-advisor/SKILL.md`。

## 给 Codex / Claude 的 skill 文件

Codex：

```bash
mkdir -p ~/.codex/skills/skill-advisor ~/.codex/skills/dsh-dispatch
cp skills/skill-advisor/SKILL.md ~/.codex/skills/skill-advisor/SKILL.md
cp skills/dsh-dispatch/SKILL.md ~/.codex/skills/dsh-dispatch/SKILL.md
```

Claude Code：

```bash
mkdir -p ~/.claude/skills/skill-advisor ~/.claude/skills/dsh-dispatch
cp skills/skill-advisor/SKILL.md ~/.claude/skills/skill-advisor/SKILL.md
cp skills/dsh-dispatch/SKILL.md ~/.claude/skills/dsh-dispatch/SKILL.md
```

然后把 `examples/AGENTS.codex.snippet.md` 加到全局或项目 `AGENTS.md`。Claude 项目可用 `examples/CLAUDE.snippet.md` 和 `examples/CLAUDE.settings.local.example.json`。

## 使用方式

强信号出现时，Agent 应说：

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

在用户明确说 run / start / enable / pair / set up / launch 该 workflow 之前，**不要**执行。

## 本地审计

```bash
python3 scripts/skill_audit.py --write-manifest --report --syntax-check --dry-run-sync
```

审计在本机运行，不上传文件。生成的 manifest / report 可能含本地路径，发布前请审阅。

## QA

```bash
python3 -m py_compile scripts/skill_audit.py
python3 -m pytest tests
node --test gateway/local-gateway.test.mjs
```

黑盒用例见 `docs/qa-matrix.md`。

## License

MIT。vendored 文件保留原版权声明，见 [VENDOR.md](VENDOR.md)。
