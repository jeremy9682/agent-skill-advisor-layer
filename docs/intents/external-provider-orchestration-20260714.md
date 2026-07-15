## Intent

Goal: 将 Grok CLI、Cursor Auto 与未来 CLI provider 接入同一套本地开发治理，同时保留真实 provider 身份，并让 session、agent、scale、checkpoint ledger 与 skills 使用都可追踪。

User-facing outcome: 以后新增 provider 只需注册 manifest 和 adapter；运行会自动留下 `seat x provider x model x session x skills` 证据。Cursor 会被现有 Agent Sessions 正确索引，Grok 获得原生 Agent Sessions provider 补丁；跨设备只同步可移植 canon，不同步凭据或 transcript。

In scope:

- 新增 provider manifest、统一 `agent-run` wrapper 和隐私最小化 append-only run journal。
- 首批适配 Grok CLI 与 Cursor Auto；对 Cursor 原始 SQLite/JSONL 与 Grok 原生 session 目录做事实核验。
- 把 skill 的 available、exposed、selected、read-or-invoked 与内容 digest 纳入运行证据，并复用现有 skill advisor / high-cost 护栏。
- 让 checkpoint ledger 与 scale 消费统一 run 指针，不把 provider 当成治理 seat。
- 基于真实成功 Grok 4.5 session，为 Agent Sessions 准备原生 Grok discovery/parser/indexer/search/UI/enablement 补丁、脱敏 fixture 与测试。
- 用同一未完成、低风险任务比较基础组合与加入 Grok/Cursor 后的覆盖、冲突、延迟和失败模式。

Out of scope:

- 不自动购买订阅、添加 API key、启用充值或自动续费。
- 不同步凭据、cookies、完整 transcript 或私有 session 数据到其他设备。
- 不把 Grok 日志伪装成 Cursor/OpenClaw 日志。
- 不在缺少完整 Xcode 时声称已构建或安装修改版 Agent Sessions。
- 不承诺任意未知 CLI 的私有 transcript 都能零代码解析；自动化边界是 manifest 注册、统一调用和规范化 run journal，原生日志 parser 仍需 provider adapter。

Deliberate tradeoffs:

- provider identity 与 governance seat 分离；Grok/Cursor 可服务判断或执行，但三席独立性仍由 seat 约束。
- run journal 只存元数据、哈希、状态和指针；可观察性优先于完整回放，隐私优先于 transcript 集中化。
- skill 使用只接受 wrapper/日志可观察证据；模型自报仅作 claim，不作 verified evidence。
- Agent Sessions 原生 Grok 支持沿用其现有 provider 模式，先做小而真实的 browse/search 支持，不提前宣称 cockpit、usage、analytics 或 subagent parity。
- Cursor `--print` 的 DB-only session 以原始 SQLite 为事实源；Agent Sessions 索引是派生视图。

Constraints:

- 当前 advisor repo 有大量用户未提交改动；只新增独立文件，除非逐处确认，不覆盖现有 dirty 文件。
- Agent Sessions 当前无运行时 provider plugin；原生 Grok 支持需要源码补丁和重新编译。
- 本机只有 Command Line Tools，没有完整 Xcode；可准备与审查补丁，但本地 app build/install 是显式环境 gate。
- 所有 provider 调用默认只使用当前已登录/免费/既有套餐路径，不改变计费设置。
- append-only journal 不保存 prompt/response 正文、token、email、account id 或 auth material。

Verification expected:

- 单元测试覆盖 manifest 校验、命令生成、privacy redaction、append-only journal、skill digest/evidence 和 provider/seat 分离。
- Grok 真实 fixture 覆盖 user、assistant、tool_call、tool_result、model、cwd、session id；Cursor 覆盖 DB-only `--print` session。
- live smoke：Grok 4.5 与 Cursor Auto 在只读任务上成功，记录退出码、耗时、session 路径与 skill 证据；目标 repo fingerprint 不变。
- Agent Sessions：Cursor 新 DB-only session 经刷新可见；Grok patch 通过 focused parser/discovery tests。完整 app build 仅在 Xcode 可用后执行。
- 对照实验保存基础组与增强组的共同发现、新增正确发现、错误/冲突、延迟及失败收敛情况。

Task shape: judgment + multi-module feature

Risk zone: standard；跨设备隐私与外部 CLI 运行按高审慎处理，但不触碰 money/permissions/migration/PII/irreversible write。

Model seats: direction=Claude/Opus；landing=Codex + 外部 provider adapter；当前跨家族 final_review=Grok 4.5 high（Fable 无额度且不再调用）；GPT-5.6 Sol xhigh 作为独立第二终审/仲裁参与讨论，但在 Codex 产出 diff 时不替代跨家族终审。provider 身份与 seat 身份分离。

Effort budget: high；禁止额外付费 judge，禁止自动订阅；若 Xcode gate 过大，保留可审查补丁与明确 blocker，不伪报安装完成。

Scale gates: plan gate；provider/seat separation；privacy audit；skill evidence audit；Cursor/Grok live smoke；Agent Sessions focused tests；independent final diff review；ship gate only after local app build becomes available。
