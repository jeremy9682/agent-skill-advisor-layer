# O11 编码代理接口层：落点与实施方案（2026-09-23）

状态：方案 + 最小原型（draft）。定位：**内部运维工程师自用，不对客户开放**（founder Q46）。
上游正本：YunChouAI-carDealer 笔记 `notes/research/coding-agent-automation-20260923.md` 第五节（四层）、决策 Q44 / Q46；约束沿用 07-21 延期模式卡 `DR-20260721-agent-harness`（agent 循环是商品；ACP 管接入、可替换；接进来能做什么由我们在 agent 之外管；三层授权不混称）。

## 一、一句话

四层里，**第 1–3 层（登记、运行、登录引导）落在本仓 agent-skill-advisor-layer**，因为本仓已经持有「各家命令行能力清单」`agent-providers.yaml` 和「驱动官方命令行」的 `scripts/agent_provider_run.py`；**编排（worktree、DAG、审查衔接）继续归 agent-run-orchestrator，不动**；**第 4 层（工单、分支前缀、PR 模板、停止点）落在运维线自己的配置**，账本复用本仓现有的 `agent-ledger`。不新开仓，不并入桌面版。

## 二、落点裁定（为什么放这里，不放那里）

| 层 | 内容 | 落点 | 理由 |
|---|---|---|---|
| 1 登记与能力声明 | 装了哪些 agent、登录状态、能力（ACP、无头模式、设备码登录） | 本仓：新增 `agent-connectors.yaml` + `scripts/agent_connect.py` | 本仓 `agent-providers.yaml` 已是「provider capability manifest」；接入登记用 `provider_ref` 复用它的可执行文件、要剥掉的环境变量、计费策略，不重复声明 |
| 2 运行 | 优先 ACP，退路是启动官方命令行；每个 agent / 账号独立运行目录；钩子回传进度 | 本仓 `agent_provider_run.py`（退路已在用）；ACP 作为它的新传输方式（后续） | 这个 wrapper 已经是「只驱动官方命令行、用用户自己的登录、剥掉 API key 环境变量」的唯一入口，再造一个会分叉 |
| 3 登录引导 | 没装→给官方安装命令；没登录→给官方登录命令；没账号→注册页；登录后测试对话 | 本仓 `scripts/agent_connect.py`（本 PR 原型） | 与第 1 层同一份登记，同一处跑探测 |
| 4 我方管理层 | 工单、分支前缀、PR 模板（含 Helpdesk 号）、只读诊断、审批停止点、账本 | 运维线配置（运维 bot 那台电脑的配置清单）+ 本仓 `agent-ledger` | ACP 不是授权系统，这层必须在 agent 之外；和接入哪家 agent 无关 |

不放 agent-run-orchestrator：它的 README 明确「does not own model routing or provider configuration」，只管调度、worktree 隔离、交接、审查、恢复。
不新开仓：本仓已有能力清单、wrapper、安装脚本和账本，新仓只会多一份要同步的清单。
不并入桌面版：Q46 定位内部工具；原研究结论「出现第二个真实使用者再产品化」照旧。

**边界**：`agent-connectors.yaml` **不是路由源**，登记了也不代表能被 `agent-run-dispatch` 派发。路由正本仍是 `routing-policy.yaml`，派发能力清单仍是 `agent-providers.yaml`。例如 kimi 登记在接入层，但不在 `agent-providers.yaml`，所以仍不可经 dispatch 派发——这是刻意的。

## 三、本 PR 的最小原型（P0）

- `agent-connectors.yaml`：登记 codex、kimi 两家。
  - codex 用 `provider_ref: codex` 复用派发清单；kimi 不在派发清单里，自带可执行文件与环境变量策略。
  - 每家有：官方安装命令、登录状态探测（命令 + 判定规则 + 证据强度）、登录引导（只打印本家命令行的登录命令、注册页、步骤）、能力声明。
- `scripts/agent_connect.py`：
  - `validate`：清单不合规一律退出码 2（写错即拒）。
  - `status [--connector X] [--json]`：每项检查三态（过 / 不过 / 没检查）；分两层报告：
    - `login_verdict`（登录层）：可执行文件 + 登录 + 计费策略；
    - `verdict`（整体就绪）：登录层 → 每个声明为 true 的能力 → 真实对话，只有 `ready` 算成功。任何「不过 / 没检查」都非 0 退出（不许「没检查当通过」）。
    - 原型不跑真实对话，所以完整 `status` **恒非 0**（`live_turn_not_checked`）——它现在不是就绪闸门，这是刻意的（终审 P1-1）。
  - `status --login-only`：只回答「登录是否已配置且符合计费策略」，不跑能力探测（能力行标「没检查」），全部 `login_verdict=login_configured` 才退出 0。JSON 顶层带 `scope: full | login-only`，防止把登录层结论当整体就绪。
  - 登录层没配置好的（未登录、未安装、无法判断、登录方式不符），自动附登录引导。引导最后一步让工程师运行 `python3 scripts/agent_connect.py status --login-only --connector <名>` 确认——登录已配置且符合计费策略即退出 0；不让跑完整 `status`（它按设计恒非 0，复审 P2）。有测试直接执行引导里写的这条命令，登录后必须退出 0、登录前必须退出 1。
  - `guide <connector>`：只打印引导，不执行命令行。

### 判定规则（本机 2026-09-23 实测样本）

| 家 | 探测命令 | 已登录 | 未登录 | 证据强度 |
|---|---|---|---|---|
| codex 0.155.1 | `codex login status` | 退出 0 且「Logged in using ChatGPT」→ 订阅；「Logged in using an API key」→ API key | 退出 1 且「Not logged in」 | 官方状态命令 |
| kimi 2.0.0 | `kimi provider list` | 每行一条 provider 记录，**整行**必须是 `<id>  type=<类型>  models=<数量>  source=<oauth\|inline\|apiJson(url)>`（字段间恰好两个空格，前后不许有别的字，区分大小写），逐条分类后聚合：只有 `managed:kimi-code` + `type=kimi` + `models≥1` + `source=oauth` 这一种记录算订阅，**全部**记录都是它才判订阅；`inline` / `apiJson(...)` → API key / 自定义；**混合 → `mixed`，任何计费策略都不认**；不完整的行、或完整但不是登记订阅 provider 的 OAuth 记录（别的 id、别的类型、0 个模型）→ 无法判断 | 退出 0 且整段输出只有「No providers configured.」 | 配置推断（kimi 没有「是否已登录」子命令，只能证明配置了 OAuth provider，不能证明令牌仍有效） |

kimi 聚合的理由（终审 P1-2）：kimi 允许同时配多个 provider，文本输出不说默认模型走哪个 provider（只有 `--json` 说，但它含凭据，不读）。所以「存在一个 OAuth」不能证明用的是 OAuth，只有「全是 OAuth」才无歧义。源码核对：kimi-code 2.0.0 `handleProviderList` 每行 `<id>  type=..  models=N  source=<oauth|inline|apiJson(url)>`，末尾可选空行 + `Default model: ...`。

逐行识别要按**完整记录**做（复审 P1）：上一版只要行尾是 `source=oauth` 就当 provider 行，`not-a-provider source=oauth` 这种行会被判成订阅。现在清单用带命名字段的整行模式（`id` / `type` / `models` / `source`）识别记录，规则按字段逐个整值匹配；`validate` 还强制「判订阅的规则必须把每个字段都钉死」，防止以后有人把订阅规则放宽回只看 `source`。登记的订阅 provider 名来自源码：`kimi login` 不论 `--region mainland-cn` 还是 `global`，写的都是 `managed:kimi-code`（`KIMI_CODE_PROVIDER_NAME`，type `kimi`，带 OAuth 引用，至少 1 个模型）。格式另用本机安装的真实 kimi 2.0.0 可执行文件对着临时目录里的**合成配置**（无真实登录、无令牌）重新抓过：纯 OAuth、OAuth + inline 混合、inline + apiJson、未登记 id 的 OAuth、空配置五种，经 `agent_connect.py status --login-only` 分别判为 订阅 / `mixed` / API key / 无法判断 / 未登录。

样本来源：已登录样本来自本机真实状态；未登录样本来自指向空目录的 `CODEX_HOME` / `HOME`；codex API key 样本用假占位串写进临时 `CODEX_HOME` 取得，取完即删，未碰真实登录。

### 安全性质（每条都有测试）

1. 只执行本家命令行：探测命令第一个参数必须是 `{binary}`；登录引导里的命令第一个词必须是本家命令行名，且不许带 `| ; & $ > <` 等拼接符。
2. 原始输出只在内存里分类，**永不打印、不进 JSON**。原因：`codex login status` 在 API key 模式下会打出部分 key。
3. 探测时剥掉该家的 API key 环境变量（codex 复用派发清单的 `strip_environment`），避免「环境里有 key 就算已登录」。
4. 标准输入关闭、超时封顶 60 秒；超时或输出不认识 → 「没检查」→ 结论「无法判断」，退出码非 0。能力探测超时 / 不过同样让整体 `status` 非 0。
5. 能力声明为 true 必须带探测，报告的是探测结果（能力不许虚报）；声明 false 不许带探测。
6. 不执行登录、不读凭据文件、不存任何秘密；登录引导只提示。
7. 「登录已配置」≠「能用」：`live_turn`（真实对话）在原型里恒为「没检查」，因此完整 `status` 恒非 0；`mixed` 登录方式不进任何计费策略（清单里也不许声明）。

### 验证

- 新测试 `tests/test_agent_connect.py` 71 条（终审后 +14，复审后 +31，其中改写 1 条：原「多个 OAuth provider 都判订阅」用的 `managed:kimi-code-global` 是虚构 id，真实 kimi 不会写出，改为判「无法判断」），全部用写在临时目录里的假命令行真实起子进程。
- 复审修复后补 13 个变异（记录识别退回子串匹配、字段匹配退回子串匹配、订阅规则只看 `source`（清单被 `validate` 拒绝）、同上且去掉校验门、完全退回旧的行尾 `source=oauth` 判定、去掉「订阅规则须钉死全部字段」门、分隔符放宽为任意空白、大小写不敏感、允许 0 个模型、引导改回完整 `status`、允许自由文本规则、不校验字段名、识别出记录但没规则认领时忽略），全部变红；未变异的副本同负载下全绿。
- 历史那次「39 过 / 1 失败」已复现并定因：把提交 `914cb9c` 的四个文件拷到临时目录，8 路并发各跑 6 遍整文件，48/48 都是 `test_capability_probe_timeout_blocks_overall_status` 失败（`assert 'unknown' == 'capability_not_checked'`）；同一份不加负载单跑 3/3 过。原因是那版测试把全局探测上限压到 1 秒，负载下 kimi 的**登录**探测也超时，登录层变成「无法判断」。`cc23541` 已改为只缩短 ACP 探测自己的超时；当前版本在同样 8 路负载下 24/24 遍整文件全过。
- 终审修复后补 13 个变异（能力不过被忽略、能力整体不计入、能力没检查不阻断、真实对话没检查当过、完整模式按登录层退出、`--login-only` 仍跑探测、混合塌成首个、不认识的行被忽略、`mixed` 进策略、不走聚合、OAuth 模式不锚定、条目规则不校验、引导条件错），全部变红。
- 变异 11 个（忽略退出码、未知当已登录、不剥环境变量、回显原始输出、去掉「声明需探测」、计费恒过、能力忽略匹配、真实对话恒过、引导命令不限本家、引导不拦拼接符、探测不限 `{binary}`），全部变红。
- 本机真跑：`status --login-only` 两家均「登录已配置」、退出 0；完整 `status` 两家 `live_turn_not_checked`、退出 1，kimi ACP 能力探测过、codex ACP 不适用；把 `CODEX_HOME` / `HOME` 指向空目录再跑，两家都判「未登录」并附引导（真实命令行上的反向对照）。
- 全量测试：本分支 334 过 / 4 失败；4 个失败是 `tests/test_router_hook.py` 的子进程超时，origin/main 基线同样 4 个失败（基线 308 过 / 4 失败），与本改动无关。

## 四、后续分期（每期单独 PR、单独终审）

| 期 | 内容 | 开工门 |
|---|---|---|
| P1 | 补 claude / cursor / grok 连接器（全部 `provider_ref` 复用）；`--smoke` 显式开关跑一轮真实对话（花额度，所以默认关），把 `live_turn` 从「没检查」变成过 / 不过；显示额度（仅在该家有官方查询命令时） | 主席定 |
| P2 | 运行层：`agent_provider_run.py` 加 ACP 传输（kimi 原生 `kimi acp` 先行；codex 需评估社区 `codex-acp` 适配器，本机未装）；每 agent / 账号独立运行目录（参照 Orca 给每个 Codex 窗格单独 `$HOME`）；进度走各家自带钩子 | 需先过方案盲审（改公共 wrapper） |
| P3 | 管理层对接：运维 bot 配置清单写明每一步停止点谁放行；派发前 `agent-ledger` 记录用了哪家、哪个登录方式 | 随运维线配置清单 |
| P4 | 安装引导「用户确认后执行官方安装命令」 | P1 之后 |

## 五、刻意取舍

- **不做登录代执行**：只打印官方命令，由工程师自己在终端跑。原型不进入凭据链路，符合 Q44「订阅只许驱动官方命令行、用用户自己的登录」。
- **kimi 只做配置推断**：它没有状态子命令；与其伪造「已验证」，不如明写证据强度，P1 的 `--smoke` 再补真实对话证据。混合 provider 失败关闭（终审 CHALLENGE 后修）。
- **完整 `status` 恒非 0**：原型不花额度跑真实对话，就不许报告整体成功；只想问登录的用 `--login-only`（终审 CHALLENGE 后修）。
- **codex ACP 标「不适用」**：官方命令行没有 ACP 服务端，社区适配器未安装，不虚报。
- **不改 `agent-providers.yaml`**：它被 agent-run-orchestrator 的 `governance.lock.json` 按哈希钉住；新开一个文件不影响派发链路。
- **新文件，不塞进 wrapper**：`agent_provider_run.py` 近 4000 行且被钉住，原型阶段独立脚本更易审、易撤。

## 六、未覆盖 / 风险

- 各家命令行改输出措辞会让判定落到「无法判断」（失败关闭，不会误判已登录），需要随版本更新规则。
- 只在本机 macOS 上实测；其他机器的安装路径靠 `binary_candidates` + `PATH`。
- 未做额度显示、测试对话、ACP 运行、独立运行目录（见分期）。
- 排序：Q46 把 O11 排在宏力问题与 10-10 出口第 ② 条之后；Q50 要求无依赖模块并行。本 PR 纯本机只读、不连任何站点、不占验收站，与这两条线没有资源冲突；P1 之后是否继续由主席定。
