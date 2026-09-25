# pipeline 运行进度行与运行日志 设计

- **日期**：2026-09-25
- **状态**：待用户评审（**v2**：按反馈修订为双出口——控制台粗粒度 + 文件全量日志）
- **背景问题**：pipeline 模式全程静默——orchestrator.py 零 `print`，
  `faceblur-lite pipeline` 从启动到结束无输出，结束才打一个 JSON 结果数组。
  与 single 修 BUG-015 之前同款"假死"观感，批量大/片长时无法判断在跑还是在挂。
- **v2 反馈**：详细过程输出为**日志文件**；控制台只大概输出时间、进度、
  过程关键事件、结果。

## 目标

1. **控制台（stderr）粗粒度**：耗时、聚合进度（每视频分解）、关键事件
   （重试 / 提交失败 / qc / 终态）、收尾结果——扫一眼就知道"活着、到哪了、结果如何"。
2. **文件日志细粒度全量**：`workdir/pipeline.log`——per-chunk 事件、
   视频状态机转变、run 参数与头尾，事后可 grep 取证。
3. stdout 最终 JSON 不变——机器消费零影响。
4. 双控制面（sqlite / gocp）通用；**ControlPlane 契约零变更**（12 场景测试不动）。

## 非目标

- 不做 `\r` 单行刷新（日志留痕优先）
- 不做 `--quiet` / 日志级别过滤（YAGNI；要安静用重定向）
- 日志不做轮转（单轮运行短、量小；追加模式即可）
- 不做 JSON 日志行（半结构化 key=value 够 grep）
- 不动 single 心跳（bash 侧，BUG-015）
- 全部碰撞短路路径不加行（已有 JSON 结果 + 退出码 1）

## 设计

### 三层事件源（全部父进程侧）

| 层 | 事件 | 来源 |
|---|---|---|
| L1 自有知识 | 提交汇总 / 提交失败 / qc 前+结果 / 逐视频终态 / 收尾汇总 | run_batch/_orchestrate 既有调用点，精确即时 |
| L2 巡检轮询 | per-chunk 状态转变（供日志）+ 聚合行（供控制台，节流）+ ↻ 重试 | 现有 0.5s while 巡检循环挂快照 diff |
| L3 只读旁路 | `_progress_snapshot(db)` | videos JOIN chunks GROUP BY，复制 stats.py `mode=ro` 先例 |

Worker 是 `multiprocessing.Process`（spawn），complete/fail 事件发生在子进程——
父进程不旁听（pickle 代理 + 多进程 stderr 交错的复杂度不值），用巡检轮询 diff
代替。0.5s tick 内 chunk 状态若跨级跳变（pending→done），diff 按**行级终态**补发
对应事件（proc_s 取自 started_at/finished_at 列，不依赖 tick 恰好采样到中间态）。

### 控制台行格式（粗、节流；前缀 `[pipeline +Ns]` = 距 run 开始秒数）

```
[pipeline +0s] 提交 3 视频 → 17 chunks（workers ×2，segment 30s）
[pipeline +12s] 6/17 · a_5min 2/10 · b_30s ✔ · c_90s 1/2      ← 节流 ≥5s 且有变化
[pipeline +31s] ↻ a_5min chunk#4 第 2 次尝试                    ← 即时
[pipeline +48s] qc b_30s ✅ 0 违规（抽 20 帧）                  ← 即时
[pipeline +52s] ✖ c_90s failed：chunk 3 次耗尽                  ← 即时
[pipeline +98s] 收尾：2 done · 1 failed · 总耗时 1m38s
```

### 文件日志（细、全量、无节流）

- **路径**：`workdir/pipeline.log`（自动开启；gocp 模式同 workdir）——
  与 pipeline.db 同级，run 工件归一处
- **格式**：`ISO 时间 <event> k=v …` 半结构化，grep 友好：

```
2026-09-25T14:32:01.234 run_start inputs=3 workers=2 segment=30 det_every=3 cp=sqlite
2026-09-25T14:32:01.981 video_submit video=10b5dd2f input=real_video.MP4 chunks=7
2026-09-25T14:32:04.112 chunk_start video=10b5dd2f idx=3 attempt=1
2026-09-25T14:32:29.870 chunk_done video=10b5dd2f idx=3 proc_s=28.3 attempt=1
2026-09-25T14:33:02.105 lease_reclaim chunks=4,7
2026-09-25T14:33:40.512 video_status video=10b5dd2f from=merging to=qc_wait
2026-09-25T14:34:10.003 qc_result video=10b5dd2f sampled=71 violations=16
2026-09-25T14:34:10.480 run_end done=0 qc_failed=1 failed=0 wall_s=129.2
```

- **事件清单**：run_start（含参数）/ video_submit / chunk_start / chunk_done /
  lease_reclaim / video_status（状态机转变）/ qc_result / run_end
- **追加模式** + `=== run start / end ===` 头尾分隔——跨轮追加是日志叙事惯例
  （区别于已诊断的 DB 跨轮污染问题，那个另行立项）
- **worker 字段暂缺**：chunks 表无 worker_id 列（fencing，backlog #2），
  列加上后 chunk_start/done 补 worker=wN
- **写失败静默**：日志绝不反噬主流程（try/except 全包）

### ProgressReporter（双 sink）

- `console_write`：节流聚合 + 即时关键事件（规则同 v1：聚合 ≥5s 且变化才打、
  首次快照即打；↻/qc/终态即时）
- `log_write`：全量无节流
- `now` / 两个 write 均依赖注入，节流与格式可单测

### db 选择与容错

- workdir 下 `gocp.db` 存在且 `--cp-url` 模式 → 优先；否则 `pipeline.db`
  （与 blur.sh report_pipeline 同规则）
- **快照任何异常 → 静默跳过该 tick**：进度是尽力而为，绝不反噬主流程

## 测试策略

- 单测：reporter 双 sink 节流（注入时钟/写入捕获）；`_progress_snapshot`
  （SqliteControlPlane 真 db 造态：pending/running/done/attempt>1）
- 集成：现有 pipeline 测试跑 2 小视频——控制台（capsys）断言提交/收尾行；
  **日志文件断言** run_start/run_end 存在、chunk_done 行数 = chunk 总数、
  追加模式两轮 run 后头尾计数 = 2
- 回归：87 测试零红；契约测试零变化

## 文档同步

- pipeline-guide §7.2 观测出口 bullet 旁加"运行进度行 + pipeline.log"+ 实操入口样例
- blur.sh：批量收尾 echo 一行 `详细日志：<workdir>/pipeline.log`（report_pipeline 处）
- demo 注记：pipeline 步骤只重定向 stdout，进度行露出 = **有意展示**

## 开放项（先按默认走）

- 聚合间隔 5s / tick 0.5s 的手感，跑过真实批量后再调
- merge 单独行：控制台不做（✔ + qc 行覆盖）；日志侧由 video_status 转变覆盖
- 日志行是否需要 run_id（目前靠 run_start 头分隔，够用）
