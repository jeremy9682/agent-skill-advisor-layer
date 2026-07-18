# Intent: agent-run stability hardening retroactive seal

## 用户目标

对已合并的 agent-run 稳定性加固（893684e..a877e45，含 PR #20/#21/#22）补做
governed cross-family final review（codex_final_review），journal 中此前无 execute
producer；先前 seal（evt-20260717T104210）因 kill_process_tree P1 BLOCKED，P1 已在 a877e45 修复。

## 范围

- serial lock passthrough、stream-json session attribution、timeout canon、QA harness
- kill_process_tree：leader 先退出时仍按 PGID 清理（a877e45 / PR #22）
- 验证：`git diff 893684e..a877e45`；pytest 210 pass；functional QA 47 pass；PR #20+#21+#22 merged

## 验收

- 一条 successful execute producer（claude-family）+ codex_final_review seal PASS
- checkpoint open/claim/close 与 journal run_id 可追溯
