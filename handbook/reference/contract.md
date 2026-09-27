# 控制面契约参考（v1.2）

控制面是**协议不是实现**：`control.py` 定义 `ControlPlane` 协议（12 方法），数据面 Worker 只准 import 这个协议；实现有两个——`state.py` SqliteControlPlane（SQLite WAL〔Write-Ahead Logging，预写日志〕内嵌，默认）与 `remote.py` HttpControlPlane（适配 `gocp` Go 服务，`--cp-url` 切换）。两实现过**同一份契约测试**（`tests/test_contract.py` 14 场景 ×2 实现 = 28 用例），行为漂移立刻红灯。

## 12 方法与关键不变量

| # | 方法（签名省略类型） | 语义 | 关键不变量 |
|---|---|---|---|
| 1 | `init_db()` | 建表 | 幂等（CREATE IF NOT EXISTS） |
| 2 | `submit_video(input, final_output, chunk_inputs) → video_id` | 原子写入 video + 全部 chunks | 单事务；video 出生即 `processing` |
| 3 | `claim_chunk(worker_id, ttl_s) → chunk \| None` | 互斥认领 | `BEGIN IMMEDIATE` 单写者串行化；置 `running` + lease；按 (video 创建序, chunk idx) 出队 |
| 4 | `complete_chunk(id, metrics, *, worker_id, attempt) → bool` | 置 done + 计数 | **围栏**：WHERE 匹配 `(worker_id, attempt)` 才受理；**原子 Join**：全部 done 时同一事务置 `merging`，恰好一个调用者拿 true |
| 5 | `complete_merge(video_id, merged_path)` | 合并完成 | `merging → qc_wait` |
| 6 | `fail_chunk(id, error, *, worker_id, attempt, max_attempts=3)` | 失败回写 | 围栏同上；**终态不可逆**（done/failed 迟到 fail 不受理）；未耗尽回 `pending` 且 attempt+1 |
| 7 | `reclaim_expired(max_attempts=3) → int` | lease 过期回收 | 只回收 video 仍 `processing` 的 chunk；耗尽 → chunk+video 双 failed |
| 8 | `get_video(id) → dict` | 读 | 不存在 raise KeyError |
| 9 | `get_chunk(id) → dict` | 读 | 同上 |
| 10 | `chunk_outputs(video_id) → list[str]` | 按 idx 排序的 chunk 输出路径 | 合并方使用 |
| 11 | `video_overview() → list[dict]` | 全部 videos（按 created_at） | 只读 |
| 12 | `finish_video(id, status, output_path=None) → None` | qc 后终态回写 | `output_path=COALESCE(?, output_path)`（隔离路径不覆盖空值） |

围栏等价业界 **SQS receipt handle**：attempt 每次重派 +1，是任务持有纪元的唯一标识——lease 过期重派后，僵尸 Worker 的迟到 ack/fail 因 attempt 不匹配被拒。

## 两级状态机

```
videos:  processing ──(最后 chunk done 者认领)──▶ merging ──▶ qc_wait ──▶ done | qc_failed
              │                                        │
              └──(chunk 耗尽 / lease 耗尽)──▶ failed ◀──┘（终态三者不可逆）

chunks:  pending ◀──(fail 未耗尽，attempt+1)── running ──▶ done
            │                                    │
            └──(claim，置 lease)────────────────▶ failed（耗尽，attempt 记最终计数）
```

## 存储契约（双实现逐字一致）

- **DDL（Data Definition Language，建表语句）**：`videos(id, input_path, output_path, merged_path, status, error, created_at, finished_at)` + `chunks(id, video_id, idx, input_path, output_path, status, attempt, worker_id, lease_until, metrics, enqueued_at, started_at, finished_at, error, UNIQUE(video_id, idx))`——Python 与 Go 版本逐字相同
- **并发**：SQLite WAL + `busy_timeout=10000`；写事务 `BEGIN IMMEDIATE`（Python 显式执行；Go 经连接串 `_txlock=immediate` 起手即同语义）
- **序列化边界**：Go 端可空列显式置 `null`（不缺键，对齐 Python dict 的 None 语义）；HTTP `JSON null ↔ Python None`

## gocp HTTP 形态（12 端点 ↔ 12 方法）

```console
$ gocp [db_path] [listen_addr]      # 默认 pipeline.db 127.0.0.1:8080，纯 Go 无 CGO
```

| 端点 | 方法 | 备注 |
|---|---|---|
| `POST /init` | init_db | 启动时已建表，no-op ok |
| `POST /videos` | submit_video | body: input_path/final_output/chunk_inputs |
| `POST /chunks/claim` | claim_chunk | 空闲时返回 JSON null → Python None |
| `POST /chunks/{id}/complete` | complete_chunk | **缺 worker_id/attempt → 400**（防新旧二进制混跑时围栏静默失效） |
| `POST /chunks/{id}/fail` | fail_chunk | 同 400 规则 |
| `POST /videos/{id}/merge_complete` | complete_merge | |
| `POST /videos/{id}/finish` | finish_video | |
| `POST /maintenance/reclaim` | reclaim_expired | 编排器巡检期调用 |
| `GET /videos/{id}` | get_video | 不存在 404 → 客户端转 KeyError |
| `GET /chunks/{id}` | get_chunk | 同上 |
| `GET /videos/{id}/chunk_outputs` | chunk_outputs | |
| `GET /videos` | video_overview | |

---

**事实源锚点**：协议 12 方法 [control.py:12-29](../../src/faceblur_lite/control.py#L12-L29)｜互斥认领 [state.py:83-100](../../src/faceblur_lite/state.py#L83-L100)｜围栏+原子 Join [state.py:102-131](../../src/faceblur_lite/state.py#L102-L131)｜终态不可逆+attempt 语义 [state.py:139-169](../../src/faceblur_lite/state.py#L139-L169)｜lease 回收 [state.py:171-196](../../src/faceblur_lite/state.py#L171-L196)｜DDL [state.py:36-60](../../src/faceblur_lite/state.py#L36-L60) ≡ [main.go:111-136](../../gocp/main.go#L111-L136)｜端点注册 [main.go:600-614](../../gocp/main.go#L600-L614)｜400 围栏规则 [main.go:239-243](../../gocp/main.go#L239-L243)｜契约测试 `tests/test_contract.py`（14 场景 ×2）
