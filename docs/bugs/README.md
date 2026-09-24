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
| [007](007-merger-relative-paths.md) | Task 11 | concat 相对条目按列表目录解析，合并 ENOENT 卡 merging | 已修复 |
| [008](008-bootstrap-python-floor.md) | blur.sh Task 1 | 默认 python3<3.10 双地板（PEP 660 需 pip≥21.3 + requires-python≥3.10）自举失败 | 已修复 |
| [009](009-out-of-frame-box-crash.md) | 真实视频首跑 | 预测框出画 → expand_box 上界缺口 → numpy 越界切片空数组 → GaussianBlur 崩溃 | 已修复 |
| [010](010-qc-fps-filter-phase-offset.md) | 真实视频 qc | qc 抽帧走 fps 滤镜：恒定 +14 帧相位偏移 + t_sec 标签失真 0.47s，且与检测节奏锁相 | 已修复 |
| [011](011-fast-motion-track-rebirth.md) | 真实视频 qc | 快运动 IoU 失配 → track 每轮重生 vel 永不收敛 → 预测帧系统性脱靶 | 已修复 |
| [012](012-small-face-margin-pixel-floor.md) | 真实视频 qc | 小脸比例 margin 仅 2-4px 盖不住检测框抖动 → 脸缘裸露（加 margin_px 像素下限） | 已修复 |
| [013](013-low-conf-no-birth-naked-run.md) | 真实视频 qc | 低分（0.3-0.5）不出生 → 持续低置信度人脸长段裸奔（检测框并入糊区） | 已修复 |

环境类问题（非代码 bug）见 [env-notes.md](env-notes.md)。

审查发现但暂不修的 Minor 项不进本目录——在执行台账累积，终审统一 triage 后决定修或记入开放细化项。
