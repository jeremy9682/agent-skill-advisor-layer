# 日常怎么用 · 一页速查卡

> v3 开发流程 + skills 自动推荐。规矩已焊进入口文件，每个 session 自动生效——你只报任务。
> 底层规范：[task-routing](task-routing.md) · [gate-policy](gate-policy.md) · [routing-evals](routing-evals.md)

---

## 一句话原理

**以前**：每次念一长串咒语告诉 AI 怎么分工。
**现在**：分工规矩常驻系统，你**直接说要干的活**就行；工具管理员在旁边自动递工具。

---

## 🖥️ 贴哪（换机器 / 新同事接入，一次性）

```bash
git clone git@github.com:jeremy9682/agent-skill-advisor-layer.git ~/Projects/agent-skill-advisor-layer
```

1. `examples/CLAUDE.snippet.md` 内容 → 贴进 `~/.claude/CLAUDE.md`
2. `examples/AGENTS.codex.snippet.md` 内容 → 贴进 `~/.codex/AGENTS.md`
3. 注册工具管理员（可选）：把 `scripts/skill_router_hook.py` 加进 `~/.claude/settings.json` 的 `UserPromptSubmit`
   （改 settings 前先 `cp ~/.claude/settings.json ~/.claude/settings.json.bak`）
4. 自检：`python3 scripts/routing_eval.py --check` → 应 exit 0

> 机器专属配置（carDealer 点名、effort 档位表、hook 注册）不在仓库里——各机器自管。

---

## ✅ 说什么（日常触发，什么流程词都不用带）

| 你想干的事 | 直接说 | 系统自动做 |
|---|---|---|
| 改个小东西 | 「修一下 X」 | 小活直修 + 聚焦验证，免流程 |
| 做新功能 | 「做个 Y 功能，目标是……」 | 出方案 → 你批 → 落地 → Codex 终审 |
| 碰钱/权限/迁移/数据 | 正常说，带一句意图 | **自动升级**：Codex 盲审方案 + xhigh 终审 |
| 技术选型/方向纠结 | 「调研一下 A vs B」 | Codex+Claude 双模型并行辩论 |
| 审代码 | 「review 一下改动」 | 双 agent 审 + Fix-First |
| 提交 | 「ship」（明确说才跑） | 测试→审→push→PR |

**报任务时加一句「intent：我的真实目标是……」**——终审会拿它逐条对照，是性价比最高的一句话。

---

## 🚫 别说什么（这三句已废，再说会跟新规矩打架）

| ❌ 旧咒语 | 为什么废 | 现在是 |
|---|---|---|
| 「Codex 定方向」 | 判断型任务方向已归 Claude 侧 | 只有修 bug 类才 Codex 定方向 |
| 「Codex 监控 workflow」 | 实时盯着又费钱又没用 | 3 个检查点（批方案 / 看成品 / 发布） |
| 「按难度分配 effort / 不省额度」 | 已是常驻自动规矩 | 不说也执行；执行侧自由降档，终审兜底 |

---

## 🎛️ 手动开关（想覆盖默认时才说）

| 说这句 | 效果 |
|---|---|
| 「这个上 4 段」 | 强制 plan → Codex 盲审 → 落地 → 终审 |
| 「走小修通道」 | 跳过流程，直修 + 验证 |
| 「流程预算 20 分钟」 | 超了自动降级，只留最小验证 + 终审 |
| 「开判断席」/ `/model claude-fable-5` | 模糊开局/架构分叉，出 decision.md |
| 「run ship」/「启动 no-mistakes」 | 高成本管线**必须**这类明确批准词才点火 |

---

## 🔧 自检 & 维护命令

```bash
cd ~/Projects/agent-skill-advisor-layer
python3 scripts/routing_eval.py --check      # 路由回归门（recall@3 应 100%）
python3 scripts/routing_eval.py --doctor     # 从真实日志出「误报/漏报」候选，人工筛选后补进 cases.yaml
python3 scripts/skill_audit.py --report      # skill 供应链审计（provenance/hash/license）
```

**工具管理员会自动记账**：每次推荐对不对都写进 `~/.codex/skill-governance/routing-log.jsonl`，
跑几天后用 `--doctor` 看它瞎报/漏报了啥，回去修 `routing-evals/hints.yaml`。它从真实失败里长进。

---

## 三个位子（v3 铁律，记住这张图就够）

```text
判断席  ── 定方向、拍取舍       Fable → Opus → Claude（+Codex 盲审方案兜底）
落地席  ── 写代码               Claude（按难度调 effort）
终审席  ── 看最终 diff、验收     Codex（永不低于 high；碰禁区 xhigh）

铁律：谁坐落地席/判断席，就不能自己坐终审席。谁干的活不能自己验收。
```
