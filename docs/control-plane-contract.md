# 控制面契约规范（control-plane-contract）

控制面 = 任务状态机 + 调度决策的唯一拥有者。数据面（Worker/算子）只通过
本契约定义的操作与控制面交互，不得直接读写存储。任何语言的实现
（当前 Python `SqliteControlPlane`，规划中 Go）必须满足本规范，
并通过 `tests/test_contract.py` 的同等场景测试。

## 1. 存储 schema
（`videos` / `chunks` 两表 DDL，与 `state.py` 逐字一致；Go 实现建同构表。
时戳为 Unix epoch 秒，浮点）

## 2. 状态机
videos: processing → merging → qc_wait → done | qc_failed | failed
chunks: pending → running → done | failed
合法迁移仅由第 3 节操作触发；状态只能经控制面操作变更（单写入口）。

## 3. 操作语义（每个操作 = 一个事务）
| 操作 | 事务内容 | 保证 |
|---|---|---|
| submit_video | 建 video(processing) + N 个 chunk(pending) | 原子出现 |
| claim_chunk | 取最早 pending（video 须 processing，按 created_at,idx 序）→ running + lease_until | 并发互斥；失败视频自动出局 |
| complete_chunk | 置 done+metrics；若该 video 全 done → video merging | **恰好一个调用者拿到 merge 认领（原子 Join）** |
| complete_merge | video → qc_wait（记 merged_path） | — |
| fail_chunk | attempt+1：未耗尽 → pending 重派；耗尽 → chunk+video failed | attempt 单调递增 |
| reclaim_expired | running 且 lease_until 已过期 → 同 fail_chunk 语义 | 僵尸回收（Worker 暴死） |
| finish_video | video → done / qc_failed | 仅编排器调用 |

## 4. 不变量
- 每 chunk 至多一次成功执行（幂等输出由数据面保证，attempt 上限兜底）
- merge 至多被认领一次
- lease_until 到期前他人不可重派该 chunk
- 所有状态变更只经控制面操作

## 5. 并发实现要点（Go 实现指引）
- SQLite：WAL + busy_timeout=10000；写事务用 BEGIN IMMEDIATE 取代行锁
  （Go：database/sql + modernc.org/sqlite 或 mattn/go-sqlite3，
  `conn.BeginTx` 起手先执行 `BEGIN IMMEDIATE`）
- 事务内"读-判-写"（如 claim/complete_chunk）必须整体在 BEGIN IMMEDIATE
  内完成，不得拆两个事务
- 演进：存储换 Redis 时，原子 Join 须用 Lua 脚本等价实现
