# Skill Advisor Layer

**模型分派 canon + 本机对接套件。** 一个公开仓库：clone 之后跑 `./scripts/install.sh`，定级策略、`agent-run`、DSH 插件、ZCode/MCP 网关都在这棵树里。不必再 clone 我们其它仓。

[English README](README.md)

## Quick Start

**你需要已经有** Cursor **或** Claude **或** Codex 订阅（以及对应 CLI）。可选：官方 DeepSeek Harness、LiteLLM。它们是第三方运行时，像 Node 一样自己装。

```bash
git clone https://github.com/jeremy9682/agent-skill-advisor-layer.git
cd agent-skill-advisor-layer
./scripts/install.sh
```

`install.sh` 会：

1. 把 `agent-run` 链到**本 clone** 的 `scripts/agent_provider_run.py`。若 `~/.local/bin/agent-run` 已存在且不是这份启动器（例如 Beads 包装），则原文件不动，改为安装 `~/.local/bin/agent-run-dispatch`。
2. 若 `PATH` 上有 `dsh`，对 **web** 和 **headless** 执行 `dsh plugin add`：**本 clone** 的 `plugins/dsh-dispatch-pack` 与 `plugins/dsh-llm-cursor-acp`。
3. 跑 `node gateway/local-gateway.mjs doctor`。

然后：

```bash
agent-run routes                          # 或 agent-run-dispatch routes
agent-run doctor --task-shape mechanical
node gateway/local-gateway.mjs doctor
```

可选额度代理：`examples/litellm/`（把示例配置拷到 git 外；不要提交密钥）。官方 DSH：`npm i -g @deepseek-ai/dsh`，见 [docs/harness-opt-in.md](docs/harness-opt-in.md)。**不必**再 clone `dsh-skill-pack`、`dsh-cursor-codex` 或 harness fork。

本 clone 提供：

```text
routing-policy.yaml              机器 canon（task_shape → 席位/模型）
scripts/agent_provider_run.py    agent-run 启动器
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
