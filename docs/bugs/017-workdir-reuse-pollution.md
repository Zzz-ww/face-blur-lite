# BUG-017：workdir 跨轮复用污染控制面库

- **发现**：2026-09-25 用户实跑 `outputs/20260925-test1/` 诊断 + 进度波对同行为的显式依赖
- **任务**：修复波 fix/bug-016-017
- **状态**：已修复（sqlite 模式非空库显式拒绝）

## 现象

同一 `--outdir`（连带同一 workdir）连跑两轮批量后：

1. **stats 变垃圾**：`videos` 表累积两轮的行——实测 2 视频 qc_failed=2
   （上一轮遗留）、吞吐 1.1 chunks/min 的分母是**跨轮墙钟**（14 chunks
   ÷ 763s，两轮相隔 714.9s）；
2. **虚报 qc_failed**：上一轮的终态行被 stats/收尾报告原样捞出，
   看起来像本轮失败；
3. **潜伏危险**：上一轮若被中断，遗留 `pending` chunk 会被下一轮新起的
   Worker 池认领——但 splitter 只清 chunk 文件不清 DB 行，残留行指向的
   文件路径可能已变（换 --segment 重切后更必然），Worker 会对着不存在的
   路径重试耗尽 → 莫名 failed。

进度波（feat/pipeline-progress）在日志侧**有意**选择了"跨轮追加是
叙事惯例"（`=== run start/end ===` 分隔、快照按本轮 vid_set 过滤），
把 DB 污染问题顶到了台面——同一目录两种工件两种语义，DB 那半边必须
表态。

## 根因

`init_db()` 只做 `CREATE TABLE IF NOT EXISTS`；`_orchestrate` 假设
workdir 是"本轮一次性工件目录"（splitter 清旧 chunk、progress 追加日志
都按"单轮所有权"设计），但 DB 无任何轮次边界检查——**两套假设从未对齐**：
日志=追加叙事，DB=每次从零。谁复用目录谁踩雷。

## 修复

**sqlite 本地模式：非空库显式拒绝**（[_orchestrate](../../src/faceblur_lite/orchestrator.py)
在 `init_db()` 后检查 `video_overview()` 非空 → 抛 `WorkdirReuseError`；
CLI 捕获后 stderr 一行指引 + 退出码 2——"环境/输入/参数错误"类）。
指引内容：换新 workdir，或删除旧 `pipeline.db` 重来；断点续跑
（resume）= 演进 backlog #6，不在本修复。

**边界裁定**：`--cp-url`（gocp）模式**豁免**——远端控制面是共享队列
（R11/R12：submit 廉价增量入库 + 共享池 drain + 多客户端），"非空即拒绝"
会摧毁库即队列语义；观测侧隔离已由进度波的 vid_set 过滤 + stats 按
查询时刻快照承担。

## 教训

1. **一次性工件目录 vs 共享队列是两种语义**，同一存储位置只能选一种；
   本仓的边界=本地 workdir（sqlite）一次性、远端控制面（http）共享。
   写入库的组件必须声明自己属于哪种语义。
2. 跨轮复用的危害第一次露头不是崩，是**统计悄悄变假**——观测数据的
   正确性也是正确性，且更晚被发现。
3. "日志追加 + DB 从零"的混合语义可以共存，但必须在显式契约里
   （本修复后：日志=多轮叙事，DB=单轮所有权，拒绝时日志不留痕）。
