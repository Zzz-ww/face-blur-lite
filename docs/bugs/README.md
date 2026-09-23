# 执行期 Bug 记录

SDD（subagent-driven development）执行期发现并修复的 bug，一 bug 一文件。规矩：**先根因证明，后授权修复**；测试断言尽量保持计划原文；计划母本同步修正。

| # | 任务 | 一句话 | 状态 |
|---|---|---|---|
| [001](001-task1-fixture-math.md) | Task 1 | fixture 帧值数学与断言矛盾（首帧=0 vs 要求≈40） | 已修复 |
| [002](002-tests-root-parents.md) | Task 3/5/12 | tests/ 下 `parents[2]` 少一层，skipif 误触发 | 已修复 |
| [003](003-tracker-birth-iou.md) | Task 4 | tracker 出生不计 hit + iou_thr 排除出生首跳 | 已修复 |
| [004](004-testmedia-face-crop.md) | Task 5 | 检测框只盖贴图 1/3，std 断言数学不可满足 | 已修复 |
| [005](005-blur-kernel-ratio.md) | Task 5 | 0.15 核只抹高频，低频对比存活 | 已修复 |
| [006](006-orchestrator-mkdir.md) | Task 10 | 切片快路径不建目录，Worker ENOENT 耗尽重试 | 已修复 |

环境类问题（非代码 bug）见 [env-notes.md](env-notes.md)。

审查发现但暂不修的 Minor 项不进本目录——在执行台账累积，终审统一 triage 后决定修或记入开放细化项。
