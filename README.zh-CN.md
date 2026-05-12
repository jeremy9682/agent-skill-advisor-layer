# Skill Advisor Layer

**面向高成本 Agent Skill 的主动建议层与治理层。**

Skill Advisor Layer 用来帮助 Codex、Claude Code 以及其他支持 skill 的
Agent 在合适的时候主动提醒有用 workflow，同时避免偷偷启动高成本、有副作用、
需要权限确认的 skill。

[English README](README.md)

## 为什么需要它

装了很多 skill 后，系统通常会走向两个极端：

- **太被动**：有用的 skill 已经安装，但用户不点名就从来不会被建议。
- **太激进**：Agent 一次加载或运行太多 skill，浪费上下文，也可能造成副作用。

这个 repo 提供一个中间层：

1. 识别高成本 skill 的强触发信号。
2. 只主动建议一个最相关的 workflow。
3. 等用户明确批准后才执行。
4. 小任务、紧急任务、无关任务时保持安静。

## 包含内容

```text
skills/skill-advisor/       路由 skill
scripts/skill_audit.py      本地 skill 盘点与治理审计脚本
examples/                   Codex / Claude 配置片段
docs/                       路由、治理和 QA 文档
tests/                      轻量 pytest 测试
```

## 默认覆盖的高成本 Skill

| Skill | 什么时候建议 | 默认动作 |
| --- | --- | --- |
| `huashu-agent-swarm` | 大型、多模块、可并行的任务，例如后端、前端、测试、文档、QA 一起推进 | 只建议 |
| `gstack-pair-agent` | 需要另一个 Agent 共享浏览器、页面或真实 QA 上下文 | 只建议 |
| `gstack-retro` | 一周、一个 sprint、一次部署或大修复序列结束后复盘 | 只建议 |
| `gstack-setup-gbrain` | 配置长期项目脑、gbrain 或 MCP-backed memory | 只建议 |

如果你的本地 skill 名称不同，可以改
`skills/skill-advisor/SKILL.md`。

## 安装

Codex：

```bash
mkdir -p ~/.codex/skills/skill-advisor
cp skills/skill-advisor/SKILL.md ~/.codex/skills/skill-advisor/SKILL.md
```

Claude Code：

```bash
mkdir -p ~/.claude/skills/skill-advisor
cp skills/skill-advisor/SKILL.md ~/.claude/skills/skill-advisor/SKILL.md
```

然后把 `examples/AGENTS.codex.snippet.md` 里的路由规则加入全局或项目级
`AGENTS.md`。

Claude 项目可以参考
`examples/CLAUDE.settings.local.example.json` 配置项目级 `skillOverrides`。

## 使用模式

当信号足够强时，Agent 应该这样说：

```text
This looks like a candidate for <skill> because <reason>. I can run it if you approve.
```

用户必须明确说“运行、启动、启用、配对、配置、launch 这个 workflow”后，
Agent 才能执行目标 high-cost skill。单纯描述一个大目标，不等于批准执行。

## 本地审计

```bash
python3 scripts/skill_audit.py --write-manifest --report --syntax-check --dry-run-sync
```

审计脚本会检查 skill 元数据、调用策略、脚本语法、依赖状态和更新安全性。默认
策略是保守的：

- 复制型 skill 只有在上次 manifest 能证明本地未修改时才允许安全同步；
- git 管理或本地修改过的 skill 只报告差异，不自动覆盖；
- 高成本 skill 会被标记为 `suggest-confirm`，不会自动运行。

## 隐私与安全

- 审计脚本只在本地运行。
- 脚本可能读取本地 skill 目录和本地 agent session 文件，用于估算使用情况。
- 脚本不会上传本地文件、prompt、报告或 session 内容。
- 生成的 manifest 和 report 可能包含本机路径；公开前请先审查。
- `.gitignore` 默认排除了生成的 manifest 和 report JSON 文件。

## QA

```bash
python3 -m py_compile scripts/skill_audit.py
python3 -m pytest tests
```

黑盒路由测试用例见 `docs/qa-matrix.md`。

## License

MIT.

