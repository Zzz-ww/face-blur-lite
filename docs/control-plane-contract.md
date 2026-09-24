# 控制面契约规范（control-plane-contract）

控制面 = 任务状态机 + 调度决策的唯一拥有者。数据面（Worker/算子）只通过
本契约定义的操作与控制面交互，不得直接读写存储。任何语言的实现
（当前 Python `SqliteControlPlane`，规划中 Go）必须满足本规范，
并通过 `tests/test_contract.py` 的同等场景测试。

## 1. 存储 schema

（时戳为 Unix epoch 秒，浮点；与 `state.py` 逐字一致；Go 实现建同构表）

```sql
CREATE TABLE IF NOT EXISTS videos(
    id TEXT PRIMARY KEY,
    input_path TEXT NOT NULL,
    output_path TEXT NOT NULL,
    merged_path TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL)
```

```sql
CREATE TABLE IF NOT EXISTS chunks(
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(id),
    idx INTEGER NOT NULL,
    input_path TEXT NOT NULL,
    output_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    lease_until REAL,
    metrics TEXT,
    enqueued_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    error TEXT,
    UNIQUE(video_id, idx))
```

## 2. 状态机
videos: processing → merging → qc_wait → done | qc_failed | failed
chunks: pending → running → done | failed
chunks 终态（done/failed）不可逆——迟到信号不得回退。
合法迁移仅由第 3 节操作触发；状态只能经控制面操作变更（单写入口）。

## 3. 操作语义（每个操作 = 一个事务）
| 操作 | 事务内容 | 保证 |
|---|---|---|
| submit_video | 建 video(processing) + N 个 chunk(pending) | 原子出现 |
| claim_chunk | 取最早 pending（video 须 processing，按 created_at,idx 序）→ running + lease_until | 并发互斥；失败视频自动出局 |
| complete_chunk | 置 done+metrics（**仅 running 态受理**，迟到 ack 返回 False 不计 join）；若该 video 全 done → video merging | **恰好一个调用者拿到 merge 认领（原子 Join）** |
| complete_merge | video → qc_wait（记 merged_path） | — |
| fail_chunk | 终态（done/failed）不受理；受理则 attempt+1（含耗尽分支）：未耗尽 → pending 重派（记 error）；耗尽 → chunk failed（记 error/finished_at）+ video failed（记 error/finished_at，仅自 processing 翻转） | attempt 单调递增；终态不可逆 |
| reclaim_expired | running 且 lease_until 已过期且 **video=processing**：未耗尽 → pending 重派（attempt+1）；耗尽 → chunk failed（attempt+1，**不写 chunk error/finished_at**）+ video failed（error='lease 过期且重试耗尽'） | 僵尸回收（Worker 暴死）；终态视频错误原因不被改写；耗尽分支与 fail_chunk 在 chunk 字段写入上**有意不同**（失败原因记在 video 级） |
| finish_video | video → done / qc_failed | 仅编排器调用 |
| get_chunk | 读单 chunk 全字段（只读，不存在抛 KeyError） | 契约测试/观测断言 |

## 4. 不变量
- 每 chunk 至多一次成功执行（幂等输出由数据面保证，attempt 上限兜底）
- merge 至多被认领一次
- lease_until 到期前他人不可重派该 chunk
- 所有状态变更只经控制面操作
- 终态不可逆：done/failed 不因迟到信号回退（complete_chunk/fail_chunk 均带状态守卫）

## 5. 并发实现要点（Go 实现指引）
- SQLite：WAL + busy_timeout=10000；写事务用 BEGIN IMMEDIATE 取代行锁
  （Go：database/sql + modernc.org/sqlite 或 mattn/go-sqlite3，
  `conn.BeginTx` 起手先执行 `BEGIN IMMEDIATE`）
- 事务内"读-判-写"（如 claim/complete_chunk）必须整体在 BEGIN IMMEDIATE
  内完成，不得拆两个事务
- 演进：存储换 Redis 时，原子 Join 须用 Lua 脚本等价实现
