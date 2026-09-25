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
| finish_video | video → done / qc_failed（可选改写 output_path：qc_failed 隔离后指隔离层，缺省不动） | 仅编排器调用 |
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

## 6. HTTP API 映射（gocp 实现）

| 协议方法 | HTTP | 请求体 | 响应 |
|---|---|---|---|
| init_db | POST /init | — | {"ok": true}（schema 服务启动时已建） |
| submit_video | POST /videos | {input_path, final_output, chunk_inputs:[[idx,in,out],…]} | {video_id} |
| claim_chunk | POST /chunks/claim | {worker_id, ttl_s} | chunk 对象 或 null |
| complete_chunk | POST /chunks/{id}/complete | {metrics} | {merge_claimed: bool} |
| fail_chunk | POST /chunks/{id}/fail | {error, max_attempts} | {ok: true} |
| complete_merge | POST /videos/{id}/merge_complete | {merged_path} | {ok: true} |
| finish_video | POST /videos/{id}/finish | {status, output_path?} | {ok: true} |
| reclaim_expired | POST /maintenance/reclaim | {max_attempts} | {reclaimed: int} |
| get_video | GET /videos/{id} | — | video 对象（404 + {"error"} 不存在时） |
| get_chunk | GET /chunks/{id} | — | chunk 对象（404 不存在时；客户端转 KeyError） |
| chunk_outputs | GET /videos/{id}/chunk_outputs | — | {outputs: [path,…]} |
| video_overview | GET /videos | — | [video 对象,…] |

JSON 语义：键 = snake_case 列名；可空列显式 null（对应 Python
dict[row] 的 None 语义，不缺键）；错误 = 非 2xx + {"error": msg}；
404 → 客户端 `HttpControlPlane` 转 `KeyError`（与 SqliteControlPlane 的
get_video/get_chunk 不存在语义对齐，契约 v1.1）。
complete/fail 对不存在的 chunk id：与参考实现一致返回
merge_claimed:false / ok（不抛错）；get_video/get_chunk 不存在 →
404 → 客户端 KeyError。
写操作守卫语义（仅 running 可 complete、终态不可 fail、reclaim 过滤终态
video）见 §3——gocp 与 state.py 逐字对应。
