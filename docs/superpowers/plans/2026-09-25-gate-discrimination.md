# 闸门恢复判别力（fencing + qc 分级 + 顺手五件）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 控制面 complete/fail 加 (worker_id, attempt) 复合持有者围栏（契约 v1.2，sqlite/gocp 双实现）；qc 违规按 conf 单轴分致命/边缘（退出码 0/1/3）；顺手五件（全密度 CLI、box 注释、mv 容错、隔离防覆盖、chunk_retry 事件）。

**Architecture:** 契约先行——协议签名三实现（control.py 协议 / state.py sqlite / gocp Go + remote.py HTTP 适配）同 commit 原子变更，契约测试参数化双跑锁一致；qc 分级只在数据面（qc.py + cli + blur.sh），severity 不进 ControlPlane 契约、隔离语义零变动（任何违规仍隔离）。spec：docs/superpowers/specs/2026-09-25-gate-discrimination-design.md。

**Tech Stack:** Python 3.10+（opencv-python/numpy/scipy/pytest 白名单）、Go stdlib + modernc.org/sqlite、bash 3.2、SQLite WAL。

## Global Constraints

- 依赖白名单：Python 仅 `opencv-python/numpy/scipy/pytest`；Go 仅 `modernc.org/sqlite`——零新依赖
- 模型资产不入库（models/*.onnx gitignore；FACEBLUR_MODEL env 覆盖）
- **outputs/ 与 inputs/ 整体 gitignore 永不入库（硬保证）；实验产物写 `mktemp -d` 临时目录，严禁写 outputs/ 或 inputs/**
- `docs/industry-benchmark.md` 有用户未暂存手笔——**本波任何任务不得修改/暂存/提交该文件**
- bash 3.2 兼容（无关联数组、无 ${var,,}）
- qc 隔离语义不变：任何违规仍隔离 qc_failed/；severity 只分流（stderr 分列 / 复核排序 / 退出码），不进 videos.status
- 退出码优先级 2 > 3 > 1（环境错误 > qc 致命 > 边缘/failed）
- 围栏语义边界（PR/文档不得超卖）：修状态机假 done/僵尸偷 merge/迟到 fail 弹回现任；**不修**数据面 at-least-once 双写
- commit 尾行 `Co-Authored-By: Claude Code <noreply@anthropic.com>`；分支 feat/gate-discrimination，收尾 --no-ff 合并；**push 仅在用户明确要求时**
- 测试命令一律 `./venv/bin/python -m pytest`（下文简写 pytest）

---

### Task 1: 契约 v1.2——complete/fail 持有者复合围栏

**Files:**
- Modify: `src/faceblur_lite/control.py:17-19`（协议签名）
- Modify: `src/faceblur_lite/state.py:102-160`（complete_chunk / fail_chunk）
- Modify: `src/faceblur_lite/remote.py:48-59`（HTTP body 透传）
- Modify: `gocp/main.go:232-266, 305-311`（handleComplete / handleFail）
- Modify: `src/faceblur_lite/worker.py:34-45`（调用点传参）
- Test: `tests/test_contract.py`（新场景 ×2 + 既有调用点补参）、`tests/test_go_cp.py`（补参 + 400 测试）、`tests/test_progress.py:105-106`（补参）

**Interfaces:**
- Produces（后续任务与所有调用方依赖的最终签名）:
  - `complete_chunk(chunk_id: str, metrics: dict, *, worker_id: str, attempt: int) -> bool`
  - `fail_chunk(chunk_id: str, error: str, *, worker_id: str, attempt: int, max_attempts: int = 3) -> None`
  - worker_id/attempt 取自 claim 应答（claim_chunk 返回 dict 已含 attempt 与 worker_id）；必填关键字，**无 None 过渡后门**
- gocp 请求体新增 `worker_id`/`attempt`；**缺失即 400**（防新旧二进制混跑围栏静默失效）
- SQL 围栏谓词统一 `worker_id IS ? AND attempt IS ?`（IS 对 NULL/非 NULL 均正确比较；sqlite 与 modernc 均支持）

- [ ] **Step 0: 建分支**

```bash
cd /Users/zhujiawei/Desktop/zte/backup/workbackup-main/face-blur-lite
git checkout -b feat/gate-discrimination
```

- [ ] **Step 1: 写失败测试——契约 +2 场景（test_contract.py 末尾追加）**

```python
def test_fencing_late_ack_after_reclaim(cp):
    """契约 v1.2 刀 4 时序：lease 过期重派后，原持有者迟到 ack/fail 拒收。"""
    _mk(cp, n=1)
    t1 = cp.claim_chunk("w1", ttl_s=-1)     # 上一纪元：拿到即过期
    cp.reclaim_expired()                    # 重派 → pending（attempt 1）
    t2 = cp.claim_chunk("w2", 60)           # 现任纪元
    assert cp.complete_chunk(t1["id"], {}, worker_id="w1",
                             attempt=t1["attempt"]) is False  # 僵尸 ack 拒
    cp.fail_chunk(t1["id"], "迟到 fail", worker_id="w1",
                  attempt=t1["attempt"])    # 僵尸 fail：不得弹回现任任务
    cur = cp.get_chunk(t2["id"])
    assert (cur["status"], cur["attempt"]) == ("running", 1)
    assert cp.complete_chunk(t2["id"], {}, worker_id="w2",
                             attempt=t2["attempt"]) is True    # 现任过


def test_fencing_same_name_reincarnation(cp):
    """同名转世：gocp 共享队列模式 Worker 同名复用——裸 worker_id 围栏
    挡不住，(worker_id, attempt) 复合围栏挡住（attempt=持有纪元唯一标识，
    SQS receipt handle 等价物）。"""
    _mk(cp, n=1)
    t1 = cp.claim_chunk("w0", ttl_s=-1)     # 上一轮僵尸 w0，attempt=0
    cp.reclaim_expired()
    t2 = cp.claim_chunk("w0", 60)           # 同名 w0 再认领，attempt=1
    assert t2["worker_id"] == "w0" and t2["attempt"] == 1
    assert cp.complete_chunk(t1["id"], {}, worker_id="w0",
                             attempt=t1["attempt"]) is False  # 旧纪元拒
    assert cp.complete_chunk(t1["id"], {}, worker_id="w0",
                             attempt=t2["attempt"]) is True    # 新纪元过
```

- [ ] **Step 2: 更新既有调用点（同文件，新签名下全部补参）**

`test_atomic_join_single_claim`（原 :36-37）：

```python
    assert cp.complete_chunk(t1["id"], {}, worker_id="w1",
                             attempt=t1["attempt"]) is False
    assert cp.complete_chunk(t2["id"], {}, worker_id="w2",
                             attempt=t2["attempt"]) is True   # 最后完成者认领 merge
```

`test_fail_exhausts_to_video_failed`（原 :51-57）——围栏语义下 fail 后 chunk 回 pending，同票重_fail 不再命中，须重派再认领（这也是真实时序）：

```python
def test_fail_exhausts_to_video_failed(cp):
    _mk(cp, n=1)
    t = cp.claim_chunk("w1", 60)
    while t is not None:                    # 现任失败 → 重派 → 再认领，直至耗尽
        cp.fail_chunk(t["id"], "boom", max_attempts=3,
                      worker_id="w1", attempt=t["attempt"])
        t = cp.claim_chunk("w1", 60)
    assert cp.video_overview()[0]["status"] == "failed"
    assert cp.claim_chunk("w2", 60) is None  # 失败视频不再派发
```

`test_complete_chunk_late_ack_rejected`（原 :72-74）：

```python
    assert cp.complete_chunk(t1["id"], {}, worker_id="w1",
                             attempt=t1["attempt"]) is False  # 迟到 ack：pending 不算数
    t2 = cp.claim_chunk("w2", 60)
    assert cp.complete_chunk(t2["id"], {}, worker_id="w2",
                             attempt=t2["attempt"]) is True    # 现持有者完成才认 join
```

`test_fail_chunk_terminal_irreversible`（原 :81-83）：

```python
    cp.complete_chunk(t1["id"], {}, worker_id="w1", attempt=t1["attempt"])
    cp.complete_chunk(t2["id"], {}, worker_id="w2", attempt=t2["attempt"])
    cp.fail_chunk(t1["id"], "merge 崩了", max_attempts=3,        # BUG-007 场景
                  worker_id="w1", attempt=t1["attempt"])
```

`test_reclaim_skips_terminal_video`（原 :92-94）：

```python
    t2 = cp.claim_chunk("w2", 60)
    while t2 is not None:
        cp.fail_chunk(t2["id"], "boom", max_attempts=3,
                      worker_id="w2", attempt=t2["attempt"])
        t2 = cp.claim_chunk("w2", 60)       # video → failed，耗尽
```

`test_fail_exhaustion_records_final_attempt`（原 :99-105）：

```python
def test_fail_exhaustion_records_final_attempt(cp):
    _mk(cp, n=1)
    t = cp.claim_chunk("w1", 60)
    cid = t["id"]
    while t is not None:
        cp.fail_chunk(t["id"], "boom", max_attempts=3,
                      worker_id="w1", attempt=t["attempt"])
        t = cp.claim_chunk("w1", 60)
    ch = cp.get_chunk(cid)
    assert ch["status"] == "failed" and ch["attempt"] == 3  # 耗尽分支也 +1
```

`test_missing_chunk_id_semantics`（原 :120-121）：

```python
    assert cp.complete_chunk("no-such-id", {}, worker_id="w1",
                             attempt=0) is False
    cp.fail_chunk("no-such-id", "x", max_attempts=3,
                  worker_id="w1", attempt=0)   # 静默 no-op，不抛
```

`tests/test_progress.py`（原 :105-106，test_snapshot_roundtrip 内）：

```python
    cp.complete_chunk(c0["id"], {"ms": 100},
                      worker_id="w0", attempt=c0["attempt"])
    cp.fail_chunk(c1["id"], "boom",
                  worker_id="w0", attempt=c1["attempt"])  # attempt 0→1 回 pending
```

`tests/test_go_cp.py`（原 :16）：

```python
    assert cp.complete_chunk(t["id"], {}) is True   # → 改为下行
    assert cp.complete_chunk(t["id"], {}, worker_id="w1",
                             attempt=t["attempt"]) is True
```

- [ ] **Step 3: gocp 400 测试（tests/test_go_cp.py 末尾追加）**

```python
def test_gocp_rejects_unfenced_write(go_cp_url):
    """契约 v1.2：缺 worker_id/attempt 的 complete/fail POST → 400——
    防新旧二进制混跑时围栏静默失效（旧客户端字段缺失必须显式炸）。"""
    import json as _json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    for path, body in [("/chunks/x/complete", {"metrics": {}}),
                       ("/chunks/x/fail", {"error": "e"})]:
        req = Request(go_cp_url + path, method="POST",
                      data=_json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
        try:
            urlopen(req)
            raise AssertionError(f"{path} 缺围栏字段应 400")
        except HTTPError as e:
            assert e.code == 400
```

- [ ] **Step 4: 跑测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_contract.py tests/test_go_cp.py tests/test_progress.py -x -q
```
预期：FAIL——新场景 TypeError（unexpected keyword）+ 400 测试得 200。

- [ ] **Step 5: 实现——control.py 协议签名**

```python
    def complete_chunk(self, chunk_id: str, metrics: dict, *,
                       worker_id: str, attempt: int) -> bool: ...
```
```python
    def fail_chunk(self, chunk_id: str, error: str, *,
                   worker_id: str, attempt: int,
                   max_attempts: int = 3) -> None: ...
```

- [ ] **Step 6: 实现——state.py**

complete_chunk（签名 + docstring + WHERE 追加围栏；其余逻辑不动）：

```python
    def complete_chunk(self, chunk_id: str, metrics: dict, *,
                       worker_id: str, attempt: int) -> bool:
        """置 done；若该 video 全部 done，同一事务内认领 merge。

        并发下恰好一个调用者拿到 True——原子 Join（scatter-gather 门控）。
        契约 v1.2 持有者围栏：WHERE 匹配 (worker_id, attempt) 复合键——
        lease 失效重派后的僵尸迟到 ack 返回 False，不污染现任 done。
        """
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM chunks WHERE id=?",
                            (chunk_id,)).fetchone()
            cur = c.execute(
                "UPDATE chunks SET status='done', metrics=?, finished_at=? "
                "WHERE id=? AND status='running' "
                "AND worker_id IS ? AND attempt IS ?",
                (json.dumps(metrics), time.time(), chunk_id,
                 worker_id, attempt))
```

fail_chunk（SELECT 即围栏——僵尸/缺 id 走 row None 静默 no-op，分支内 UPDATE 不变）：

```python
    def fail_chunk(self, chunk_id: str, error: str, *,
                   worker_id: str, attempt: int,
                   max_attempts: int = 3) -> None:
        """终态（done/failed）不可逆：迟到 fail 不受理（BUG-007 场景——
        merge 崩溃后对已完成 chunk 的 fail 不得把 done 翻回 pending）。
        契约 v1.2 持有者围栏：SELECT 匹配 (worker_id, attempt)——僵尸迟到
        fail 静默 no-op，不得弹回现任任务/耗尽现任 attempt。
        每次受理 attempt+1（含耗尽分支——终态记录最终计数，与 reclaim 对齐）。"""
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM chunks WHERE id=? "
                "AND worker_id IS ? AND attempt IS ?",
                (chunk_id, worker_id, attempt)).fetchone()
            if row is None or row["status"] in ("done", "failed"):
                return
```

- [ ] **Step 7: 实现——remote.py 透传**

```python
    def complete_chunk(self, chunk_id: str, metrics: dict, *,
                       worker_id: str, attempt: int) -> bool:
        return self._call("POST", f"/chunks/{chunk_id}/complete",
                          {"metrics": metrics, "worker_id": worker_id,
                           "attempt": attempt})["merge_claimed"]
```
```python
    def fail_chunk(self, chunk_id: str, error: str, *,
                   worker_id: str, attempt: int,
                   max_attempts: int = 3) -> None:
        self._call("POST", f"/chunks/{chunk_id}/fail",
                   {"error": error, "worker_id": worker_id,
                    "attempt": attempt, "max_attempts": max_attempts})
```

- [ ] **Step 8: 实现——gocp/main.go**

handleComplete req 结构体（Decode 后、db.Begin 前加 400 守卫）：

```go
	var req struct {
		Metrics  map[string]any `json:"metrics"`
		WorkerID string         `json:"worker_id"`
		Attempt  *int           `json:"attempt"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	// 契约 v1.2 持有者围栏：字段缺失即 400（Attempt 用指针区分缺省与 0）
	// ——防新旧二进制混跑时围栏静默失效
	if req.WorkerID == "" || req.Attempt == nil {
		writeErr(w, 400, "worker_id+attempt required（契约 v1.2 持有者围栏）")
		return
	}
```

handleComplete UPDATE（v1.1 注释行同步改 v1.2）：

```go
	// 契约 v1.2：仅当前持有者（worker_id+attempt 复合围栏）受理——迟到 ack
	// 不算数，不进入 join 计数（与 state.py complete_chunk 守卫逐字对应）
	res, err := tx.Exec(
		"UPDATE chunks SET status='done', metrics=?, finished_at=? "+
			"WHERE id=? AND status='running' AND worker_id IS ? AND attempt IS ?",
		string(metrics), nowF(), r.PathValue("id"), req.WorkerID, *req.Attempt)
```

handleFail req 结构体 + 400 守卫（同款）：

```go
	var req struct {
		Error       string `json:"error"`
		MaxAttempts int    `json:"max_attempts"`
		WorkerID    string `json:"worker_id"`
		Attempt     *int   `json:"attempt"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	// 契约 v1.2 持有者围栏：字段缺失即 400（与 handleComplete 同款）
	if req.WorkerID == "" || req.Attempt == nil {
		writeErr(w, 400, "worker_id+attempt required（契约 v1.2 持有者围栏）")
		return
	}
	if req.MaxAttempts <= 0 {
		req.MaxAttempts = 3
	}
```

handleFail SELECT（围栏进 SELECT——ErrNoRows 路径现在同时覆盖缺 id 与僵尸，注释同步）：

```go
	if err = tx.QueryRow("SELECT video_id, attempt, status FROM chunks "+
		"WHERE id=? AND worker_id IS ? AND attempt IS ?",
		r.PathValue("id"), req.WorkerID, *req.Attempt).Scan(
		&videoID, &attempt, &status); err != nil {
		// DB 错误 ≠ 不存在：仅 ErrNoRows 走守卫路径，其余 500
		if !errors.Is(err, sql.ErrNoRows) {
			writeErr(w, 500, err.Error())
			return
		}
		// 缺失 id / 僵尸迟到 fail（契约 v1.2 围栏不匹配）与参考实现对齐
		//（Python：row is None 早退静默 no-op）——commit 空事务 + ok=true
```
（该 if 块其余行不变；两个 UPDATE 分支 SQL 不动——事务内已持写锁，SELECT 即闸门。）

```bash
cd gocp && gofmt -l . && go vet ./... && go build ./... && cd ..
```
预期：无输出（gofmt 无待格式化文件）、vet/build 通过。

- [ ] **Step 9: 实现——worker.py 调用点**

```python
            merge_claimed = cp.complete_chunk(task["id"], {
                "frames": rep.frames, "detections": rep.detections,
                "predicted_frames": rep.predicted_frames,
                "segments": rep.segments, "wall_s": rep.wall_s},
                worker_id=worker_id, attempt=task["attempt"])
```
```python
        except Exception as e:      # noqa: BLE001 —— 单 chunk 失败不拖死 Worker
            cp.fail_chunk(task["id"], f"{type(e).__name__}: {e}",
                          worker_id=worker_id, attempt=task["attempt"])
```

- [ ] **Step 10: 全量绿**

```bash
./venv/bin/python -m pytest -q
```
预期：全绿（110 基线 + 新 3 场景 ×2 双跑 = 116 上下；以实际计数为准，零失败零 error）。

- [ ] **Step 11: Commit**

```bash
git add src/faceblur_lite/control.py src/faceblur_lite/state.py \
        src/faceblur_lite/remote.py src/faceblur_lite/worker.py \
        gocp/main.go tests/test_contract.py tests/test_go_cp.py \
        tests/test_progress.py
git commit -m "feat(contract): complete/fail 持有者复合围栏 (worker_id, attempt)——契约 v1.2

刀 4 收口：lease 重派后僵尸迟到 ack 不污染现任 done、不偷 merge 认领；
迟到 fail 不弹回现任任务。attempt=持有纪元唯一标识（SQS receipt handle
等价物），裸 worker_id 被 gocp 共享队列同名转世穿透。gocp 缺字段 400
拒收防混跑静默失防。不修数据面 at-least-once 双写（attempt 上限兜底）。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: qc 分级内核（qc.py）

**Files:**
- Modify: `src/faceblur_lite/qc.py:8-31, 52-54`
- Test: `tests/test_qc_severity.py`（新建——纯单元，无 assets 依赖）

**Interfaces:**
- Produces（Task 3/4 依赖）:
  - 常量 `T_FATAL_CONF = 0.7`（临时待校准）
  - `severity_for(conf: float) -> str`（"fatal"/"marginal"，`conf >= T_FATAL_CONF` 为 fatal）
  - `Violation.severity: str`（第 4 字段，显式传入）
  - `QCReport.fatal_count -> int` / `QCReport.marginal_count -> int`（property）
  - `passed` 语义不变（not violations）

- [ ] **Step 1: 写失败测试（tests/test_qc_severity.py 新建）**

```python
"""qc 严重度分桶单元测试（纯合成，无资产依赖）——spec 2026-09-25 §2。"""
from faceblur_lite.qc import (T_FATAL_CONF, QCReport, Violation,
                              severity_for)


def test_severity_boundary():
    """conf 单轴分桶：>=T_FATAL_CONF 为 fatal；边界值本身落 fatal。"""
    assert T_FATAL_CONF == 0.7          # 临时待校准，见校准文档
    assert severity_for(0.50) == "marginal"
    assert severity_for(0.69) == "marginal"
    assert severity_for(0.70 - 1e-9) == "marginal"
    assert severity_for(T_FATAL_CONF) == "fatal"
    assert severity_for(0.99) == "fatal"


def test_report_counts_and_passed_unchanged():
    rep = QCReport(violations=[
        Violation(0.0, 0.90, (0, 0, 9, 9), "fatal"),
        Violation(1.0, 0.55, (0, 0, 9, 9), "marginal"),
        Violation(2.0, 0.51, (0, 0, 9, 9), "marginal")])
    assert rep.fatal_count == 1 and rep.marginal_count == 2
    assert not rep.passed                      # 分级不改变 passed 二值语义
    assert QCReport().fatal_count == 0
    assert QCReport().passed
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_qc_severity.py -q
```
预期：FAIL——ImportError（severity_for 不存在）。

- [ ] **Step 3: 实现 qc.py**

常量（DEFAULT_PASS_CONF 之后）：

```python
DEFAULT_SAMPLE_FPS = 1.0
DEFAULT_PASS_CONF = 0.5
T_FATAL_CONF = 0.7   # 致命/边缘分界，临时待校准（2026-09-25 444 处分布
                     # 与依据见 docs/qc-severity-calibration.md；SCRFD 升级
                     # 或新增视觉标签 ≥20 处时须重校准；0.5-0.7 段零标签
                     # =已知局限，分界保守取 0.7，无标签段归边缘）


def severity_for(conf: float) -> str:
    """conf 单轴分桶：致命（高置信真信号）/边缘（低置信残块）。"""
    return "fatal" if conf >= T_FATAL_CONF else "marginal"
```

Violation（box 注释 = 顺手件 2）：

```python
@dataclass
class Violation:
    t_sec: float
    conf: float
    box: tuple[int, int, int, int]   # (x1, y1, x2, y2) 左上/右下像素坐标——非 xywh
    severity: str                    # "fatal"/"marginal"（severity_for 分桶）
```

QCReport（passed 之后追加两个 property）：

```python
    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def fatal_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "fatal")

    @property
    def marginal_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "marginal")
```

qc_video 构造点（:52-54）：

```python
                rep.violations.append(Violation(
                    round(t, 2), d.conf,
                    (int(d.x1), int(d.y1), int(d.x2), int(d.y2)),
                    severity_for(d.conf)))
```

- [ ] **Step 4: 跑测试确认通过**

```bash
./venv/bin/python -m pytest tests/test_qc_severity.py tests/test_qc.py -q
```
预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/qc.py tests/test_qc_severity.py
git commit -m "feat(qc): 违规按 conf 单轴分致命/边缘——T_FATAL_CONF=0.7 临时待校准

severity_for + Violation.severity + QCReport.fatal/marginal_count；
passed 二值语义与隔离语义零变动（分级只分流）。box 字段补 xyxy 语义注释
（顺手件 2——2026-09-25 取证已实际误读一次）。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: CLI 层——qc JSON 分级计数、退出码 0/1/3、--sample-fps 全密度

**Files:**
- Modify: `src/faceblur_lite/cli.py`（模块级 helper ×2 + qc 分支 :140-147 + pipeline 返回 :109 + 参数 :60）
- Test: `tests/test_cli.py`（追加纯单元测试）

**Interfaces:**
- Consumes: Task 2 的 `QCReport.fatal_count/marginal_count`、`Violation.severity`
- Produces:
  - `_qc_exit_code(rep) -> int`（0 全过 / 1 仅边缘 / 3 含致命）
  - `_sample_fps(v: str) -> float | None`（"none"/"all" → None）
  - qc 子命令 JSON 顶层新增 `"fatal"`/`"marginal"`；violations 元素经 `v.__dict__` 自然带 severity
  - pipeline 返回码：`any(r.qc.fatal_count > 0) → 3`，否则 all done → 0 / 其他 → 1（Task 5 blur.sh 依赖此 rc 链）

- [ ] **Step 1: 写失败测试（tests/test_cli.py 追加）**

```python
def test_sample_fps_none_all_parses_to_full_density():
    """顺手件 1：--sample-fps 接受 none/all → None（全密度逐帧）。"""
    from faceblur_lite.cli import _sample_fps
    assert _sample_fps("none") is None
    assert _sample_fps("all") is None
    assert _sample_fps("ALL") is None
    assert _sample_fps("2") == 2.0
    assert _sample_fps("0.5") == 0.5


def test_qc_exit_code_three_levels():
    """退出码分级：0 全过 / 1 仅边缘 / 3 含致命（2 留给环境/输入错误）。"""
    from faceblur_lite.cli import _qc_exit_code
    from faceblur_lite.qc import QCReport, Violation
    ok = QCReport()
    marginal = QCReport(violations=[
        Violation(0.0, 0.55, (0, 0, 9, 9), "marginal")])
    fatal = QCReport(violations=[
        Violation(0.0, 0.55, (0, 0, 9, 9), "marginal"),
        Violation(1.0, 0.90, (0, 0, 9, 9), "fatal")])
    assert _qc_exit_code(ok) == 0
    assert _qc_exit_code(marginal) == 1
    assert _qc_exit_code(fatal) == 3
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_cli.py -q -k "sample_fps or exit_code"
```
预期：FAIL——ImportError（_sample_fps/_qc_exit_code 不存在）。

- [ ] **Step 3: 实现 cli.py**

模块级 helper（`from faceblur_lite import __version__` 之后、`def main` 之前）：

```python
def _qc_exit_code(rep) -> int:
    """qc 退出码分级：0 全过 / 1 仅边缘 / 3 含致命。2 留给环境/输入
    错误，优先级 2>3>1（blur.sh rc 链同语义）。"""
    if rep.passed:
        return 0
    return 3 if rep.fatal_count else 1


def _sample_fps(v: str) -> float | None:
    """"none"/"all" → None（全密度逐帧，最终验收用）；其余按 float。"""
    if v.lower() in ("none", "all"):
        return None
    return float(v)
```

qc 子命令参数（:60）：

```python
    q.add_argument("--sample-fps", type=_sample_fps, default=1.0,
                   help='抽样密度；"none"/"all"=全密度逐帧（最终验收）')
```

qc 分支（:140-147）：

```python
    if args.cmd == "qc":
        from faceblur_lite.qc import qc_video
        rep = qc_video(args.video, sample_fps=args.sample_fps)
        print(json.dumps({
            "passed": rep.passed, "sampled": rep.sampled_frames,
            "fatal": rep.fatal_count, "marginal": rep.marginal_count,
            "violations": [v.__dict__ for v in rep.violations]},
            ensure_ascii=False, indent=2))
        return _qc_exit_code(rep)
```

pipeline 返回（:109）：

```python
        if any(r.qc is not None and r.qc.fatal_count > 0 for r in results):
            return 3          # 含致命违规（契约 2026-09-25 分级：2>3>1）
        return 0 if all(r.status == "done" for r in results) else 1
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

```bash
./venv/bin/python -m pytest tests/test_cli.py -q && ./venv/bin/python -m pytest -q
```
预期：全 PASS（既有 pipeline 测试断言 rc 语义处若受影响，仅允许"done→0/qc_failed→1"既有语义不变；det_conf=1.1 的 qc_failed 用例 rc 仍应为 1 或 3——若现有测试硬编码 1 且合成脸 conf≥0.7 使其变 3，把该断言改为 `in (1, 3)` 并在测试注释标注分级语义）。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/cli.py tests/test_cli.py
git commit -m "feat(cli): qc JSON 分级计数 + 退出码 0/1/3 + --sample-fps none/all 全密度

顺手件 1 闭合（全密度验收 CLI 可达）；pipeline 含致命违规 rc=3，
优先级 2>3>1 与 blur.sh rc 链对齐。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 编排层——qc_result 分列 + chunk_retry 事件（progress 提取）

**Files:**
- Modify: `src/faceblur_lite/progress.py`（新增 `emit_chunk_events`——从 orchestrator 内联循环提取）
- Modify: `src/faceblur_lite/orchestrator.py:149-162`（改调 emit_chunk_events）、`:201-208`（qc_result 事件 + ❌ 行分列）
- Test: `tests/test_progress.py`（追加 ×2）

**Interfaces:**
- Consumes: Task 2 的 `fatal_count/marginal_count`
- Produces:
  - `progress.emit_chunk_events(rep: ProgressReporter, prev: dict[tuple[str, int], ChunkState], cur: list[ChunkState], stems: dict[str, str]) -> None`——chunk_start/done/failed/retry 事件统一发射点
  - pipeline.log 事件 `chunk_retry video=… idx=… attempt=…`（顺手件 5）
  - `qc_result` 事件新增 `fatal=`/`marginal=` 键；stderr ❌ 行分列"致命 X · 边缘 Y"（Task 7 文档 §7.2 枚举同步依赖）

- [ ] **Step 1: 写失败测试（tests/test_progress.py 追加；import 行补 emit_chunk_events）**

import 行改为：

```python
from faceblur_lite.progress import (ChunkState, ProgressReporter, VideoState,
                                    chunk_diff, emit_chunk_events,
                                    format_progress, progress_snapshot)
```

```python
def test_chunk_retry_event_and_line(tmp_path):
    """顺手件 5：attempt 增加且回 pending → chunk_retry 事件入 log +
    ↻ console 行（此前仅 console 行，"全量细粒度日志"对重试不成立）。"""
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    prev = {("v1", 0): ChunkState("v1", 0, "running", 0, "w0", 1.0, None)}
    cur = [ChunkState("v1", 0, "pending", 1, None, 1.0, None)]
    emit_chunk_events(r, prev, cur, {"v1": "demo"})
    log = (tmp_path / "pipeline.log").read_text(encoding="utf-8")
    assert "chunk_retry video=v1 idx=0 attempt=1" in log
    assert any("↻ demo chunk#0 第 2 次尝试" in ln for ln in lines)


def test_chunk_retry_not_fired_on_first_seen_or_done(tmp_path):
    """负例：首见（prev=None）与 done 迁移（attempt 虽增）不发 chunk_retry。"""
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    emit_chunk_events(r, {}, [ChunkState("v1", 0, "running", 0, "w0",
                                         1.0, None)], {})
    log1 = (tmp_path / "pipeline.log").read_text(encoding="utf-8")
    assert "chunk_retry" not in log1 and "chunk_start" in log1
    prev = {("v1", 0): ChunkState("v1", 0, "running", 0, "w0", 1.0, None)}
    emit_chunk_events(r, prev,
                      [ChunkState("v1", 0, "done", 1, "w0", 1.0, 2.0)], {})
    assert "chunk_retry" not in (tmp_path / "pipeline.log").read_text(
        encoding="utf-8")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_progress.py -q -k retry
```
预期：FAIL——ImportError（emit_chunk_events 不存在）。

- [ ] **Step 3: 实现 progress.py（chunk_diff 之后新增）**

```python
def emit_chunk_events(rep: ProgressReporter,
                      prev: dict[tuple[str, int], ChunkState],
                      cur: list[ChunkState],
                      stems: dict[str, str]) -> None:
    """巡检 diff 出 chunk 级事件：start/done/failed 全量发射；
    attempt 增加且回 pending = 重试（chunk_retry 入 log——顺手件 5，
    此前只有 stderr ↻ 行）。stems：video_id → 输入主干（console 行用）。"""
    for c in chunk_diff(prev, cur):
        if c.status == "running":
            rep.event("chunk_start", video=c.video_id, idx=c.idx,
                      attempt=c.attempt, worker=c.worker_id or "?")
        elif c.status == "done":
            kv = dict(video=c.video_id, idx=c.idx, attempt=c.attempt,
                      worker=c.worker_id or "?")
            if c.started_at and c.finished_at:
                kv["proc_s"] = round(c.finished_at - c.started_at, 1)
            rep.event("chunk_done", **kv)
        elif c.status == "failed":
            rep.event("chunk_failed", video=c.video_id, idx=c.idx,
                      attempt=c.attempt)
        p = prev.get((c.video_id, c.idx))
        if (p is not None and c.attempt > p.attempt
                and c.status == "pending"):
            rep.event("chunk_retry", video=c.video_id, idx=c.idx,
                      attempt=c.attempt)
            rep.line(f"↻ {stems.get(c.video_id, c.video_id)} "
                     f"chunk#{c.idx} 第 {c.attempt + 1} 次尝试")
```

- [ ] **Step 4: 改 orchestrator.py 两处**

巡检循环（:149-162 的 for 体整体替换为一行调用——`stems` 定义行保留）：

```python
            stems = {v.video_id: Path(v.input_path).stem for v in mine_v}
            emit_chunk_events(prog, prev_chunks, mine_c, stems)
```
（import 行 `from faceblur_lite.progress import ...` 补 `emit_chunk_events`。）

qc 尾部事件（:201-203）：

```python
            prog.event("qc_result", video=vid, sampled=rep.sampled_frames,
                       violations=len(rep.violations),
                       fatal=rep.fatal_count, marginal=rep.marginal_count)
```

stderr 行（:207-209 拆分——✅ 行文本保持逐字不变）：

```python
            if rep.passed:
                prog.line(f"qc {stem} ✅ 0 违规（抽 {rep.sampled_frames} 帧）")
            else:
                prog.line(f"qc {stem} ❌ {len(rep.violations)} 违规"
                          f"（致命 {rep.fatal_count} · 边缘 "
                          f"{rep.marginal_count}，抽 {rep.sampled_frames} 帧）"
                          f"{suffix}")
```

- [ ] **Step 5: 跑测试确认通过 + 回归**

```bash
./venv/bin/python -m pytest tests/test_progress.py tests/test_pipeline.py -q \
  && ./venv/bin/python -m pytest -q
```
预期：全 PASS（既有测试断言旧 ❌ 行文本处若存在，同步为分列格式）。

- [ ] **Step 6: Commit**

```bash
git add src/faceblur_lite/progress.py src/faceblur_lite/orchestrator.py \
        tests/test_progress.py
git commit -m "feat(progress): chunk_retry 事件入 pipeline.log + qc_result/❌ 行致命边缘分列

chunk 事件发射提取为 progress.emit_chunk_events（可单测，不再只靠 E2E）；
顺手件 5 闭合：重试从"只有 stderr ↻ 行"变"全量日志可查"。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: blur.sh——分级解析、rc 链 2>3>1、mv 容错、同名防覆盖

**Files:**
- Modify: `scripts/blur.sh:4`（头部注释）、`:131`（全局量）、`:134-162`（qc_one）、`:329-331`（rc 改写）
- Test: 活体验证（bash -n + demo 回归；分级 rc 链活体在 Task 7 终审内嵌）

**Interfaces:**
- Consumes: Task 3 的 qc JSON `"fatal"` 键与 CLI rc 0/1/3
- Produces: shell 层 rc 优先级链 `2 > 3 > 1`；全局量 `QC_FATAL`/`QC_MARGINAL`；隔离同名 `_1/_2…` 递增 + mv 容错（顺手件 3/4 shell 侧）

- [ ] **Step 1: 头部注释（:4）**

```bash
# 退出码：0=全 done 且 qc 全过；1=仅边缘违规或存在 failed/qc_failed；3=qc 含致命违规；2=环境/输入/参数错误（优先级 2>3>1）
```

- [ ] **Step 2: 全局量（:131）**

```bash
QC_BAD=0 QC_FATAL=0 QC_MARGINAL=0
```

- [ ] **Step 3: qc_one 整体替换（:134-162）**

```bash
qc_one() {  # $1=输出文件
  local tmp rc summary sampled viol fatal qd base stem ext tgt n
  if [ ! -f "$1" ]; then
    echo "  ❌ $(basename "$1") 未生成（该视频 failed）"
    QC_BAD=1
    return 0
  fi
  tmp=$(mktemp)
  set +e
  "$FACEBLUR" qc "$1" >"$tmp" 2>/dev/null
  rc=$?
  set -e
  summary=$("$PY" -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["sampled"],len(d["violations"]),d.get("fatal",0))' "$tmp")
  set -- $summary
  sampled=$1; viol=$2; fatal=$3
  rm -f "$tmp"
  if [ "$rc" -eq 0 ]; then
    echo "  ✅ $(basename "$1") qc 通过（抽 ${sampled} 帧，0 违规）"
  else
    echo "  ❌ $(basename "$1") qc 违规（抽 ${sampled} 帧，${viol} 处：致命 ${fatal} · 边缘 $((viol - fatal))）"
    QC_BAD=1
    if [ "$fatal" -gt 0 ]; then QC_FATAL=1; else QC_MARGINAL=1; fi
    # qc_failed 隔离（spec 2026-09-25）：通过层不留未验收件。basename 守卫——
    # 文件已在 qc_failed/ 内（report_pipeline 二次质检复用本函数）则不重复移；
    # 隔离目录从 $1 自身 dirname 派生，对 OUTDIR 尾斜杠/相对绝对路径免疫。
    # 同名已存在 → _1/_2… 递增（顺手件 4：保住上一轮复核证据，防 mv 静默
    # 覆盖）；mv 失败仅警告不砸收尾（顺手件 3：与 Python 侧"隔离失败不砸
    # 批次、缺陷可见"对齐——set -e 下 if 条件位的失败不触发退出）。
    if [ "$(basename "$(dirname "$1")")" != "qc_failed" ]; then
      qd="$(dirname "$1")/qc_failed"
      mkdir -p "$qd"
      base=$(basename "$1"); stem=${base%.*}; ext=${base##*.}
      tgt="$qd/$base"; n=1
      while [ -e "$tgt" ]; do tgt="$qd/${stem}_${n}.${ext}"; n=$((n+1)); done
      if mv "$1" "$tgt"; then
        echo "  ↳ 已隔离：qc_failed/$(basename "$tgt")（复核后人工移出=转正）"
      else
        echo "  ⚠ 隔离失败（mv 报错），文件留原位——批次继续" >&2
      fi
    fi
  fi
}
```

- [ ] **Step 4: rc 改写链（:329-331）**

```bash
  # rc 优先级链（2026-09-25 分级）：2=环境/输入错误 > 3=qc 含致命 > 1=仅边缘/failed
  if [ "$rc" -eq 2 ]; then :
  elif [ "$QC_FATAL" -ne 0 ] || [ "$rc" -eq 3 ]; then rc=3
  elif [ "$QC_BAD" -ne 0 ]; then rc=1
  fi
  return "$rc"
```
（`[ "$rc" -eq 3 ]` 保 CLI pipeline 返回的 3 不被 QC_BAD 降格——report 未跑或未置 QC_FATAL 时兜底。）

- [ ] **Step 5: 语法 + 回归**

```bash
bash -n scripts/blur.sh && echo OK
tmpd=$(mktemp -d)
./scripts/blur.sh demo --outdir "$tmpd/demo" >/dev/null 2>&1; echo "demo rc=$?"
grep -c "✅" "$tmpd/demo"/*.log 2>/dev/null || true
rm -rf "$tmpd"
```
预期：`OK`；`demo rc=0`（✅ 行格式未变，demo 全过不触分级路径）。

- [ ] **Step 6: Commit**

```bash
git add scripts/blur.sh
git commit -m "feat(blur.sh): qc 分级解析 + rc 优先级链 2>3>1 + 隔离 mv 容错与同名防覆盖

qc_one 解析 fatal 计数、❌ 行分列致命/边缘、QC_FATAL/QC_MARGINAL 拆分；
顺手件 3/4 shell 侧：mv 失败警告不砸收尾、同名 _1 递增保复核证据。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: Python 侧隔离防覆盖（_quarantine 后缀递增）

**Files:**
- Modify: `src/faceblur_lite/orchestrator.py:48-56`（_quarantine）
- Test: `tests/test_pipeline.py`（追加）

**Interfaces:**
- Consumes: 现有 _quarantine 调用点（qc ❌ 分支，不改动）
- Produces: `_quarantine(out_path) -> str`——同名已存在时返回 `qc_failed/<stem>_N<suffix>` 路径（与 Task 5 shell 侧同语义）

- [ ] **Step 1: 写失败测试（tests/test_pipeline.py 追加）**

```python
def test_quarantine_preserves_previous_evidence(tmp_path, move_video):
    """顺手件 4：同名重跑再 ❌ 时隔离件 _1 递增——上一轮待复核证据
    不被 POSIX rename 静默覆盖（隔离波 record-only 遗留收口）。"""
    qdir = tmp_path / "qc_failed"
    qdir.mkdir()
    (qdir / "q3.mp4").write_bytes(b"prev-evidence")   # 上一轮遗留隔离件
    out = str(tmp_path / "q3.mp4")
    res = run_pipeline(move_video[0], out, str(tmp_path / "wd"),
                       workers=1, segment_s=3.0,
                       params=KernelParams(det_every=2, det_conf=1.1))
    assert res.status == "qc_failed"
    assert (qdir / "q3.mp4").read_bytes() == b"prev-evidence"  # 旧证据原样
    assert (qdir / "q3_1.mp4").is_file()                       # 新件递增
    assert res.output_path == str(qdir / "q3_1.mp4")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_pipeline.py -q -k preserves_previous
```
预期：FAIL——旧证据被覆盖（`read_bytes() == b"prev-evidence"` 断言失败，实际为新成品）。

- [ ] **Step 3: 实现 _quarantine**

```python
def _quarantine(out_path: str) -> str:
    """qc_failed 成品移入 outdir/qc_failed/（spec 2026-09-25：通过层
    不留未验收件，转正=人工移出）。同名已存在 → _1/_2… 递增——保住
    上一轮复核证据（POSIX rename 静默覆盖=顺手件 4 收口）。
    OSError 由调用侧容错。"""
    src = Path(out_path)
    dst_dir = src.parent / "qc_failed"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    n = 1
    while dst.exists():
        dst = dst_dir / f"{src.stem}_{n}{src.suffix}"
        n += 1
    src.rename(dst)
    return str(dst)
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

```bash
./venv/bin/python -m pytest tests/test_pipeline.py -q && ./venv/bin/python -m pytest -q
```
预期：全 PASS（既有隔离五件套测试不受影响——首轮隔离无同名冲突，路径不变）。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/orchestrator.py tests/test_pipeline.py
git commit -m "feat(quarantine): 同名隔离件 _1 递增，保住上一轮复核证据

Python 侧与 blur.sh qc_one 同语义；round-trip：res.output_path 与
finish_video COALESCE 均指递增后路径。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: 文档同步 + 校准文档 + 全量回归 + 活体对抗

**Files:**
- Modify: `docs/control-plane-contract.md`（§3/§4/§6 + v1.2 注记）
- Modify: `docs/pipeline-guide.md`（:37 mermaid、§1.2 退出码表、§6.2、§6.5、§7.2）
- Create: `docs/qc-severity-calibration.md`
- Test: 全量 pytest + 活体对抗（stub FACEBLUR 验 rc 链，spec §4）

**Interfaces:**
- Consumes: 前 6 任务全部产出
- Produces: 文档与实现一致；wave 收尾素材

- [ ] **Step 1: control-plane-contract.md 四处**

§3 complete_chunk 行（:54）替换为：

```markdown
| complete_chunk | 置 done+metrics（**仅当前持有者受理**——WHERE `status='running' AND worker_id IS ? AND attempt IS ?`，契约 v1.2 复合围栏；迟到/易主 ack 返回 False 不计 join）；若该 video 全 done → video merging | **恰好一个调用者拿到 merge 认领（原子 Join）** |
```

§3 fail_chunk 行（:56）"终态（done/failed）不受理；" 后插入：

```markdown
终态（done/failed）不受理；**仅当前持有者（worker_id+attempt）受理**（契约 v1.2——僵尸迟到 fail 静默 no-op，不得弹回现任任务）；
```

§4 不变量列表末尾追加一行：

```markdown
- lease 失效/重派后，原持有者（旧 worker_id+attempt）的 complete/fail 一律不受理（围栏键 = **(worker_id, attempt) 复合**——attempt 每次重派 +1，是持有纪元唯一标识，SQS receipt handle 的等价物；裸 worker_id 会被 gocp 共享队列模式的 Worker 同名转世穿透）
```

§6 两行请求体（:83-84）替换 + 表后 JSON 语义段（:96 句末）追加：

```markdown
| complete_chunk | POST /chunks/{id}/complete | {metrics, worker_id, attempt} | {merge_claimed: bool} |
| fail_chunk | POST /chunks/{id}/fail | {error, worker_id, attempt, max_attempts} | {ok: true} |
```
```markdown
缺 worker_id/attempt 的写请求 → **400**（契约 v1.2：防新旧二进制混跑时围栏静默失效）。
```

- [ ] **Step 2: pipeline-guide.md 五处**

:37 mermaid 行替换：

```markdown
    QC -->|"有违规"| BAD["qc_failed · 退出码 1（仅边缘）/ 3（含致命）· 成品隔离 qc_failed/"]
```

§1.2 退出码表（:81 `| 1 |` 行后插入一行，并在表后加优先级句）：

```markdown
| 1 | 仅边缘违规（fatal=0）或存在 failed / qc_failed | 内核/管线/qc |
| 3 | qc 含致命违规（conf ≥ T_FATAL_CONF=0.7，临时待校准） | qc |

优先级 **2 > 3 > 1**：环境/输入错误压过一切；致命压过边缘/failed。
```
（原 `| 1 | 存在 failed / qc_failed |` 行删除，由上两行取代；`| 0 |`/`| 2 |` 行不动。）

§6.2 列表（第 2 条后追加两条 + box 语义句）：

```markdown
3. 违规再按 **conf 单轴**分严重度：`conf ≥ T_FATAL_CONF(0.7)` = 致命（fatal），否则边缘（marginal）——阈值临时待校准，依据与重校准触发条件见 [qc-severity-calibration.md](qc-severity-calibration.md)。分级只影响退出码（1/3）与复核排序，**隔离语义不变：任何违规仍隔离**；
4. 违规清单的 `box` 字段为 `(x1, y1, x2, y2)` 左上/右下像素坐标——**非 xywh**。
```

§6.5 "qc 有违规" 行（:370）行首处置句替换为：

```markdown
| qc 有违规 | 该视频终态 qc_failed（退出码 1=仅边缘 / 3=含致命），**成品移入 `outdir/qc_failed/`**（同名递增 `_1`，上一轮复核证据不覆盖）
```
（该行其余"不自动重跑……"文字保留。）

§7.2 事件枚举（:436）两处替换——枚举清单 `chunk_failed / lease_reclaim` 之间插 `chunk_retry`，`qc_result` 后补字段说明：

```markdown
run_start / video_submit / chunk_start / chunk_done / chunk_failed / chunk_retry / lease_reclaim / video_status / qc_result（含 fatal=/marginal= 分列键）/ quarantine / quarantine_failed / run_end
```

- [ ] **Step 3: 新建 docs/qc-severity-calibration.md**

```markdown
# qc 严重度分级校准记录（T_FATAL_CONF = 0.7，临时）

- **日期**：2026-09-25（当前代码确定性复现；spec：superpowers/specs/2026-09-25-gate-discrimination-design.md §2）
- **分级轴**：纯 conf 单轴——采集数据显示尺寸在各 conf 带间不分离（celebv 大脸
  blur-blob 残块全在 [0.5,0.6)、中位 229px；real-video 小脸半糊 ≥0.84），
  尺寸加权只会把两族搅在一起。
- **语义**：分级只分流（退出码 1/3、stderr 分列、复核排序）；隔离不变——
  任何违规仍隔离 qc_failed/，"qc 是信号，人是裁判"。

## 分布（444 处违规 × conf 带，2026-09-25 采集）

| 样本 | [0.5,0.6) | [0.6,0.7) | [0.7,0.8) | [0.8,1.0) | 合计 | 备注 |
|---|---|---|---|---|---|---|
| celebv_5min（大脸特写） | 9 | 0 | 0 | 0 | 9 | 全边缘——blur-blob 残块 |
| celebv_sample1 | 0 | 0 | 0 | 0 | 0 | qc 全过 |
| musicfest_5min | 25 | 8 | 6 | 0 | 39 | |
| street_5min | 102 | 109 | 118 | 32 | 361 | max conf 0.93 |
| real_video 默认（det3） | 9 | 7 | 4 | 2 | 22 | top-2 = 0.849/0.838 |
| real_video 最优（det1+px8+并集） | 7 | 2 | 3 | 1 | 13 | 同源残余 |
| **合计** | **152** | **126** | **131** | **35** | **444** | |

## T=0.7 依据（外推，非校准）

- 结构分离：celebv 残块族全在 [0.5,0.6)（已知良性模式）；street/real-video
  高置信段集中在 0.7+；仅有的 2 个视觉定性标签（真半糊漏脸）都在
  0.838/0.849 → 高段 = 真信号；
- **已知局限**：0.5–0.7 段零视觉标签——T=0.7 是外推不是校准；无标签段
  归边缘（后续标签证明该段有真漏则下调 T）。

## 重校准触发条件

- 检测器升级（SCRFD，backlog #7）——违规分布整体平移，阈值作废重采；
- 新增视觉定性标签 ≥ 20 处（重点补 0.5–0.7 段）。
```

- [ ] **Step 4: 全量回归**

```bash
./venv/bin/python -m pytest -q && (cd gocp && go vet ./... && go build ./...)
```
预期：pytest 全绿零 skip 异常；go vet/build 干净。

- [ ] **Step 5: 活体对抗验证（spec §4——stub FACEBLUR 直测 blur.sh rc 链）**

```bash
cd /Users/zhujiawei/Desktop/zte/backup/workbackup-main/face-blur-lite
tmpd=$(mktemp -d); mkdir -p "$tmpd/bin" "$tmpd/out"
cat > "$tmpd/bin/faceblur-lite" <<'EOF'
#!/usr/bin/env bash
# stub：single 只落盘空成品；qc 按 $QC_STUB 控制 JSON/退出码
if [ "$1" = "single" ]; then touch "$3"; echo '{}'; exit 0; fi
if [ "$1" = "qc" ]; then
  case "${QC_STUB:-fatal}" in
    fatal)    echo '{"passed":false,"sampled":3,"fatal":2,"marginal":1,"violations":[{},{},{}]}'; exit 3 ;;
    marginal) echo '{"passed":false,"sampled":3,"fatal":0,"marginal":3,"violations":[{},{},{}]}'; exit 1 ;;
    pass)     echo '{"passed":true,"sampled":3,"fatal":0,"marginal":0,"violations":[]}'; exit 0 ;;
  esac
fi
echo '{}'; exit 0
EOF
chmod +x "$tmpd/bin/faceblur-lite"
sed -e "s|^FACEBLUR=.*|FACEBLUR=$tmpd/bin/faceblur-lite|" \
    -e "s|^PY=.*|PY=$(command -v python3)|" \
    -e "s|^REPO=.*|REPO=$PWD|" scripts/blur.sh > "$tmpd/blur.sh"
# R1 致命 → rc=3；R2 仅边缘 → rc=1；R3 通过 → rc=0
for mode in fatal marginal pass; do
  rm -rf "$tmpd/o_$mode"; mkdir -p "$tmpd/src_$mode"
  cp "$tmpd/out/a.mp4" "$tmpd/src_$mode/" 2>/dev/null || touch "$tmpd/src_$mode/a.mp4"
  QC_STUB=$mode bash "$tmpd/blur.sh" run "$tmpd/src_$mode/a.mp4" \
    --outdir "$tmpd/o_$mode" --mode single >/dev/null 2>&1
  echo "$mode rc=$?"
done
# R4 同名防覆盖：预置隔离件，marginal 重跑 → 隔离层出现 a_1.mp4
rm -rf "$tmpd/o_dup"; mkdir -p "$tmpd/o_dup/qc_failed" "$tmpd/src_dup"
touch "$tmpd/src_dup/a.mp4" "$tmpd/o_dup/qc_failed/a_blurred.mp4"
QC_STUB=marginal bash "$tmpd/blur.sh" run "$tmpd/src_dup/a.mp4" \
  --outdir "$tmpd/o_dup" --mode single >/dev/null 2>&1
ls "$tmpd/o_dup/qc_failed/"
rm -rf "$tmpd"
```
预期：`fatal rc=3`；`marginal rc=1`；`pass rc=0`；R4 列表含 `a_blurred.mp4` 与 `a_blurred_1.mp4` 两件。任何不符即回查 Task 5 实现。

- [ ] **Step 6: Commit**

```bash
git add docs/control-plane-contract.md docs/pipeline-guide.md \
        docs/qc-severity-calibration.md
git commit -m "docs: 契约 v1.2 围栏语义 + pipeline-guide 分级/退出码/chunk_retry + 校准记录

444 处 conf 分布落档 docs/qc-severity-calibration.md；T=0.7 标注
临时待校准（0.5-0.7 段零标签=已知局限）。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 7: 收尾（等用户拍板合并方式，不自动合并/推送）**

向用户报告：全量测试计数、活体对抗结果、待办 = `git merge --no-ff`（仅用户明确要求后执行；推送前敏感路径预检 outputs//inputs//.onnx/design.pdf/.db）。

---

## Self-Review 记录

- **Spec 覆盖**：§1 fencing→Task 1（签名/三实现/worker/调用点/2 场景/gocp 400=§1 全条目）；§2 分级→Task 2（内核）+Task 3（CLI/退出码）+Task 5（blur.sh）+Task 7（校准文档）；§3 五件→件 1=Task 3、件 2=Task 2+7、件 3=Task 5、件 4=Task 5+6、件 5=Task 4；§4 测试→各任务 TDD 步骤+Task 7 活体；§5 文档→Task 7（blur.sh 头部在 Task 5）；§6 非目标无违反（severity 未进契约、隔离语义未动）。
- **占位符**：无 TBD/TODO；所有代码步骤含完整代码。
- **类型一致性**：`severity_for(conf)->str`、`Violation.severity: str`（Task 2 定义 = Task 3/4 消费）；`emit_chunk_events(rep, prev, cur, stems)`（Task 4 定义+消费同签名）；`complete_chunk/fail_chunk` 关键字参数名 worker_id/attempt 全任务一致（gocp JSON 键 worker_id/attempt 对应）。
