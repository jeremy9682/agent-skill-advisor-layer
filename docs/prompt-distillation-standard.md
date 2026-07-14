# 提示词蒸馏标准(Prompt Distillation Standard)

> 依据:2026-07-12 提示词使用全量分析(4,865 条人打提示词;Codex sol xhigh 五轮终审 PASS;完整报告为本地私有文档,不随本仓发布)。
> 本文档是蒸馏层的 canon 入口;分档/席位以 `routing-policy.yaml` 为准,本文不复制政策。

## 四层分层(重复的话放哪)

| 层 | 承接什么 | 判定 | 载体 |
|---|---|---|---|
| 微命令(显式入口) | 你主动喊的高频**元指令**(派发/升档/评审风格/归档) | 官方判定句:"反复粘贴同样指令就该固化";Boris 阈值:一天 >1 次;本地阈值:月频 ≥4 进候选 | Claude `~/.claude/commands/*.md`(`disable-model-invocation: true`);Codex `~/.agents/skills/*/`(`allow_implicit_invocation: false`,显式 `$名字`) |
| Skills | 流程性任务知识,模型按语义自动采用 | 是"怎么做某类任务"而非"这次怎么派" | 两侧 skills 目录 |
| CLAUDE.md / AGENTS.md | 永远生效的**事实**与协议 | >30 行的"流程"应外迁;协议(如 ledger 纪律)留守 | 全局/项目 memory 文件 |
| Hooks | 必须强制、不能靠自觉的规则 | "Every time X, always Y" → hook 不是散文 | settings.json / hooks.json |

**关键经验**:元指令必须走显式调用——skill 自动触发不可靠(Vercel 评测 56% 的 case 未调用 skill:<https://vercel.com/blog/agents-md-outperforms-skills-in-our-agent-evals>;被动 description 激活率第三方 200+ 次实测约 20%:<https://scottspence.com/posts/how-to-make-claude-code-skills-activate-reliably>)。

## 现役显式入口(2026-07-14 F1 落地)

| 入口 | 侧 | 替代的手打模板(历史频次) |
|---|---|---|
| `/dispatch [surprise\|cross\|max\|solo]` | Claude | "配置拉满…"(8)、"用 dynamic workflow 按难度分 effort…"(4)、"你让 fable5 也审核下你们俩讨论"(4)、"详读整个 chat…surprise me"(7+6) |
| `/next-steps` | Claude | "接下去还要做什么?参考计划和用户需求反馈"(11) |
| `/obsidian-archive` | Claude | Obsidian 归档族(~15) |
| `$full-throttle [fable]` | Codex | 同"拉满"/互审,Codex Desktop 侧 |

配套提醒:`~/.claude/hooks/distill-reminder.sh`(UserPromptSubmit)检测到手打旧模板时提示对应入口;这些模式被 `skill_router_hook.py` 吸收后应退役该 hook。

## Codex 档位 profiles(2026-07-14 F0 落地)

内嵌 `[profiles.x]` 语法 0.134 起废弃(会使 `-p` 报错),档位在独立文件:

| profile | 文件 | model/effort | 用途 |
|---|---|---|---|
| `-p exec` | `~/.codex/exec.config.toml` | terra / medium | 普通实现派发(执行席) |
| `-p review` | `~/.codex/review.config.toml` | sol / high / **read-only** | 终审地板(判断型默认也走这档) |
| `-p review-x` | `~/.codex/review-x.config.toml` | sol / xhigh / **read-only + 非fast** | **risk overlay 命中(flip 清单/禁区)强制**;判断型需要更深时显式升 |

注意 `-p` 是**叠加语义**(继承基础 config 未覆盖的键),终审两档因此在 profile 内显式覆盖 sandbox 为 read-only。机械活执行席归 Claude(canon),不建 Codex low 档;改档位须过判断案,不得静默漂移。三档已冒烟验证(模型/effort/sandbox/端到端回包)。

## 月度复跑规程

1. `python3 ~/.claude/scripts/prompt_usage_stats.py`(全源统计+剔除对账+跨源重叠)与 `family_counts.py`(模板家族)。
2. 对比上月产物(`~/.claude/scripts/out/`),**月频 ≥4 的新模板进蒸馏候选队列**,走 founder 拍板。
3. 同步跑 `scripts/discovery_budget_check.py`(本仓)检查两侧 skill 发现预算占用。
4. 采用率检查:新入口若连续两月使用为 0,评估改名/合并/退役。

## 语料数据治理(硬规则)

- 统计产物含提示词原文 → 只写 `~/.claude/scripts/out/`(目录 0700、文件 0600),**禁止进 git/云端/vault**。
- 输出示例一律清洗(凭据/PII/PEM 整块/裸 base64)+ 截断 160 字符 + sha1 留痕;保留期 90 天,脚本自动清理。
- 归档到 Obsidian 的文档只写结论与聚合数据,不贴批量原文语料。

## F0-F3 实施记录(2026-07-14){#f0-f3-impl-20260714}

founder 批准全做(2026-07-14):F0 profiles 迁移 ✅(终审两档强制只读);F1 3+1 显式入口 ✅(命名冲突预检通过;Codex 侧显式 `$` 调用验证 LOADED);F2 治理接线 ✅(本文档 + distill-reminder hook + 预算审计脚本 + 月度复查任务);F3 催进度分类 ✅(结论:主因是回合自主性不足,非通知缺失;详情在本地私有报告附录)。实施经 `-p review` 终审两轮收敛;过程记录在 `skill-governance` 账本。
