# reference — herdr 开席与 agent-run-dispatch

官方命令表：`~/.claude/skills/herdr/SKILL.md`。本机例外：`~/.claude/skills/herdr-house/SKILL.md`。
派发 CLI：本 clone 的 `agent-run-dispatch` / `agent-ledger-dispatch`（或 `python3 scripts/agent_ledger.py`）。

## herdr：开独立 Codex 阶段门

不要对已占 TUI 的 `w1:p1`–`w1:p4` 做 `pane run`（会变成用户提示词）。

```bash
# 看谁占用了 coding-agents
herdr workspace list
herdr pane list --workspace w1
herdr agent list

# 新 pane：从当前调用席拆，cwd 必须是目标 worktree
herdr pane split --current --direction right --cwd "$PWD" --no-focus
# 记下 .result.pane.pane_id → 下称 $PANE

# 唯一名，[a-z][a-z0-9_-]{0,31}
herdr agent start sg-sol --kind codex --pane "$PANE" -- \
  -m gpt-5.6-sol -c 'model_reasoning_effort="high"'

# 等 ready；若 startup 报 agent_not_ready / blocked：agent read，不要 Blind Trust all
herdr agent prompt sg-sol "$(cat <<'EOF'
[TASK] 只审不写。审查 git diff origin/main...HEAD。
[SEAT] 独立阶段门。禁止改文件 / commit / push。
[AXES] Standards 与 Spec 分开写。
[FINDINGS] P1 闸门 vs pin/CI 分节。结尾 GATE: BLOCK|PASS_WITH_P1|PASS
EOF
)" --wait --timeout 300000

# 验收：必须读文本。idle/done 不算完。
herdr agent read sg-sol --source recent-unwrapped --lines 200
herdr agent get sg-sol
```

对抗席换名，例如 `sg-chal`，prompt 用 SKILL.md 对抗模板。不要复用阶段门同一 agent 名自审。

`agent prompt` 若 `agent_blocked`：先 `agent read`，把升级/hooks 问题交给用户；**不要**代点 Trust all。

读不到全文（alternate screen）：让 Codex 把报告写到 `/tmp/sg-sol-review.md` 只回路径，再 Read 该文件。

## 禁止的 PATH

```bash
command -v agent-run
# 若指向 Beads / 硬失败 wrapper：不要用来派发。日常用：
command -v agent-run-dispatch
command -v agent-ledger-dispatch
```

## ledger 开一条阶段门

`worktree` 必须是 `绝对路径 @ 分支 @ 40字SHA`。跨席应指向**干净** worktree（从 `origin/<branch>` 新建），不要指向脏主 checkout。

```bash
SHA=$(git -C "$WT" rev-parse HEAD)
BR=$(git -C "$WT" branch --show-current)

python3 ~/Projects/agent-skill-advisor-layer/scripts/agent_ledger.py open yunchouai \
  --intent-ref docs/intents/example.md#intent \
  --from-seat cursor-grok-orchestrate \
  --to-seat codex-final-review \
  --worktree "$WT @ $BR @ $SHA" \
  --own src/foo.py tests/test_foo.py \
  --do-not-touch src/permissions.py \
  --verification "git diff --stat origin/main...HEAD" \
  --next-action "Independent herdr Codex gpt-5.6-sol@high stage gate; review-only"

# 派发前 claim；审完 close
python3 ~/Projects/agent-skill-advisor-layer/scripts/agent_ledger.py claim yunchouai "$EVT" \
  --seat codex-final-review --note "herdr sg-sol"

python3 ~/Projects/agent-skill-advisor-layer/scripts/agent_ledger.py close yunchouai "$EVT" \
  --seat codex-final-review --outcome "GATE PASS; P1 none"

python3 ~/Projects/agent-skill-advisor-layer/scripts/agent_ledger.py fold yunchouai
```

项目 slug 按仓改（不要把 YunChou 事件写进别的 jsonl）。

## agent-run-dispatch 阶段门（可选，替代手开时）

仍禁止 PATH `agent-run`。需要 checkpoint。模型/effort 显式写出，不要 inherit 成编排席 Grok。

```bash
agent-run-dispatch doctor --task-shape standard_feature --cwd "$PWD"

agent-run-dispatch run auto \
  --task-shape standard_feature \
  --seat codex-final-review \
  --model gpt-5.6-sol \
  --effort high \
  --checkpoint-event "$EVT" \
  --cwd "$PWD" \
  --timeout-seconds 600 \
  'Review-only. Dual-axis Standards + Spec on origin/main...HEAD. Do not edit. Do not self-GO.'
```

禁区形状用 `--task-shape restricted_zone` 做**登记与对抗加严**；人操 herdr 终审仍 `-m gpt-5.6-sol` @ high。若 runtime 因 overlay 拒绝同家族终审：不要改派 fable 当终审；记下 YAML 校验结果，阶段门继续 herdr sol@high，交人看是否改 YAML。

## dsh 实现（空闲）

```bash
curl -sS -X POST 'http://127.0.0.1:3080/api/session.list' \
  -H 'content-type: application/json' \
  -d '{"type":"client-request","rpcId":"r-list","method":"session.list","payload":{}}'
```

不通 → 空闲时段在 deepseek-harness 仓 `pnpm dsh web`（端口 3080）。高峰不要启。

`session.create` / `goal.create` / `/permission danger-full-access` 全文：`~/.claude/skills/dsh-local-dispatch/reference.md`。
