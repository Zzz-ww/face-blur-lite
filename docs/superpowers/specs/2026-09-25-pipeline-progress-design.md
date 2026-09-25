# pipeline 运行进度行 设计

- **日期**：2026-09-25
- **状态**：待用户评审
- **背景问题**：pipeline 模式全程静默——orchestrator.py 零 `print`，
  `faceblur-lite pipeline` 从启动到结束无输出，结束才打一个 JSON 结果数组。
  与 single 修 BUG-015 之前同款"假死"观感，批量大/片长时无法判断在跑还是在挂。

## 目标

1. 运行期 stderr 人话进度：提交汇总、聚合 chunks 进度（每视频分解）、
   重试即时行、qc 行、逐视频终态行、收尾汇总。
2. stdout 最终 JSON 不变——机器消费零影响。
3. 双控制面（sqlite / gocp）通用；**ControlPlane 契约零变更**（12 场景测试不动）。

## 非目标

- 不做 `\r` 单行刷新（日志留痕优先）
- 不做 `--quiet`（YAGNI；需要安静用重定向）
- 不动 single 心跳（bash 侧，BUG-015）
- 不做 per-chunk 逐条行（聚合 + 每视频分解足够）
- 全部碰撞短路路径不加行（已有 JSON 结果 + 退出码 1）

## 设计

### 三层事件源（全部父进程侧）

| 层 | 事件 | 来源 |
|---|---|---|
| L1 自有知识 | 提交汇总 / 提交失败 / qc 前+结果 / 逐视频终态 / 收尾汇总 | run_batch/_orchestrate 既有调用点，精确即时 |
| L2 巡检轮询 | 聚合进度行（节流）+ ↻ 重试行（即时） | 现有 0.5s while 巡检循环挂快照 diff |
| L3 只读旁路 | `_progress_snapshot(db)` | videos JOIN chunks GROUP BY，复制 stats.py `mode=ro` 先例 |

Worker 是 `multiprocessing.Process`（spawn），complete/fail 事件发生在子进程——
父进程不旁听（pickle 代理 + 多进程 stderr 交错的复杂度不值），用巡检轮询 diff 代替；
0.5s tick 对人眼进度绰绰有余。

### 行格式（前缀 `[pipeline]`，全走 stderr）

```
[pipeline] 提交 3 视频 → 17 chunks（workers ×2，segment 30s）
[pipeline] 6/17 · a_5min 2/10 · b_30s ✔ · c_90s 1/2      ← 节流 ≥5s 且有变化
[pipeline] ↻ a_5min chunk#4 第 2 次尝试                    ← 即时
[pipeline] qc b_30s … ✅ 0 违规（抽 20 帧）                ← 即时
[pipeline] ✖ c_90s failed：chunk 3 次耗尽                  ← 即时
[pipeline] 收尾：2 done · 1 failed · 总耗时 1m38s
```

### 节流规则

- 聚合行：距上次输出 ≥5s **且** chunk done 计数有变化才打；首次快照即打
  （给"跑起来了"的确认）
- ↻ / qc / 终态行：不受节流（高价值信号）
- `ProgressReporter` 小类：`now` / `write` 依赖注入（可测节流），
  持上次快照做 diff（attempt 增加 → ↻ chunk#k 第 n 次尝试）

### 事件点明细（代码位置，orchestrator.py）

1. 提交循环 catch 处：`✖ 提交失败：<input>（原因）`
2. 提交循环后：提交汇总行（N 视频 → M chunks，workers，segment）
3. while 巡检循环：`reclaim_expired()` 返回 n>0 → 即时 ↻ 行；
   `_progress_snapshot` diff → 节流聚合行 / attempt-diff ↻ 行
4. drain 后 qc 循环：qc 前 `qc <stem> …`，结果 `✅ n 违规（抽 m 帧）`；
   failed 分支 `✖ <stem> failed：原因`
5. 收尾：`收尾：x done · y qc_failed · z failed · 总耗时`

### db 选择与容错

- workdir 下 `gocp.db` 存在且 `--cp-url` 模式 → 优先；否则 `pipeline.db`
  （与 blur.sh report_pipeline 同规则；gocp.db 由 Go 进程写，标准 SQLite WAL，ro 直读成立）
- **快照任何异常 → 静默跳过该 tick**：进度是尽力而为，绝不反噬主流程
  （try/except 全包，巡检循环本身不受影响）

## 测试策略

- 单测：reporter 节流（注入时钟/写入捕获）；`_progress_snapshot`
  （SqliteControlPlane 真 db 造态：pending/running/done/attempt>1）
- 集成：现有 pipeline 测试跑 2 小视频，父进程打印可被 capsys 捕获，
  断言 stderr 含提交行与收尾行
- 回归：87 测试零红；契约测试零变化

## 文档同步

- pipeline-guide §7.2 观测出口 bullet 旁加"运行进度行"+ 实操入口补样例输出
- demo 注记：pipeline 步骤只重定向 stdout，进度行露出 = **有意展示**（demo 是展示位）

## 开放项（先按默认走）

- 聚合间隔 5s / tick 0.5s 的手感，跑过真实批量后再调
- merge 单独行：不做（聚合行 ✔ 标记 + qc 行已覆盖）
