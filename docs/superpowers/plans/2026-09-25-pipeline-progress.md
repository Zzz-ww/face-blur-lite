# pipeline 运行进度行与运行日志 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** pipeline 模式运行期双出口进度——stderr 粗粒度行（提交/聚合/重试/qc/终态/收尾）+ `workdir/pipeline.log` 全量 key=value 事件日志。

**Architecture:** 新模块 `progress.py`（快照查询 + diff + ProgressReporter 双 sink，全部依赖注入可单测）；orchestrator 巡检循环挂快照 diff（0.5s tick），L1 自有事件直打；blur.sh 收尾 echo 日志路径。三层事件源与格式见 spec `docs/superpowers/specs/2026-09-25-pipeline-progress-design.md`（v2）。

**Tech Stack:** Python 3.10+（stdlib sqlite3/time/datetime/dataclasses），bash 3.2。零新依赖。

## Global Constraints

- 依赖白名单：opencv-python / numpy / scipy / pytest——**零新依赖**（本特性纯 stdlib）
- **stdout 最终 JSON 不变**；全部进度行走 **stderr**
- **ControlPlane 契约零变更**（12 场景契约测试不动）；快照走只读旁路 `file:...?mode=ro`（stats.py 先例）
- **进度绝不反噬主流程**：快照异常静默跳过该 tick；日志/控制台写失败 try/except 全包
- bash 3.2 兼容（blur.sh）
- 87 测试零回归
- commit message 尾行 `Co-Authored-By: Claude Code <noreply@anthropic.com>`
- 分支：`feat/pipeline-progress`
- DDL 事实（写死，勿凭记忆）：chunks 表**已有** `worker_id TEXT` 列（claim 时写入，fail/reclaim 清 NULL）；`complete_chunk` 无 `AND worker_id=?` 围栏是另一 backlog，本特性不修

---

### Task 1: progress.py——快照 + diff + ProgressReporter 双 sink

**Files:**
- Create: `src/faceblur_lite/progress.py`
- Test: `tests/test_progress.py`

**Interfaces:**
- Consumes: `SqliteControlPlane`（仅测试造态用）
- Produces（Task 2 依赖，签名精确如下）:
  - `ChunkState(video_id: str, idx: int, status: str, attempt: int, worker_id: str | None, started_at: float | None, finished_at: float | None)` frozen dataclass
  - `VideoState(video_id: str, input_path: str, status: str, total: int, done: int)` frozen dataclass
  - `progress_snapshot(db_path: str) -> tuple[list[VideoState], list[ChunkState]] | None`
  - `chunk_diff(prev: dict[tuple[str, int], ChunkState], cur: list[ChunkState]) -> list[ChunkState]`
  - `format_progress(videos: list[VideoState]) -> str`
  - `ProgressReporter(console: Callable[[str], None] | None, log_path: str | Path | None, now_mono: Callable[[], float] = time.monotonic, now_wall: Callable[[], float] = time.time, min_interval: float = 5.0)`；方法 `line(text)` / `aggregate(text)` / `event(name, **kv)` / `run_start(**kv)` / `run_end(**kv)`

- [ ] **Step 1: Write the failing tests**

`tests/test_progress.py` 全文：

```python
"""进度双出口单测：注入时钟/捕获写入，锁定节流、前缀、日志格式、
追加语义、写失败静默、快照 roundtrip、diff 规则。"""
import re

from faceblur_lite.progress import (ChunkState, ProgressReporter, VideoState,
                                    chunk_diff, format_progress,
                                    progress_snapshot)
from faceblur_lite.state import SqliteControlPlane


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_reporter(tmp_path, mono, lines):
    return ProgressReporter(console=lines.append,
                            log_path=tmp_path / "pipeline.log",
                            now_mono=mono, now_wall=mono)


def test_line_has_elapsed_prefix(tmp_path):
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    r.line("提交 3 视频 → 17 chunks")
    mono.advance(7)
    r.line("qc b_30s ✅ 0 违规")
    assert lines == ["[pipeline +0s] 提交 3 视频 → 17 chunks",
                     "[pipeline +7s] qc b_30s ✅ 0 违规"]


def test_aggregate_throttle_interval_and_change(tmp_path):
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    r.aggregate("1/3 · a 1/3")          # 首次即打
    mono.advance(1)
    r.aggregate("1/3 · a 1/3")          # 同文本 → 抑制
    mono.advance(1)                     # t=2，距首打 2s
    r.aggregate("2/3 · a 2/3")          # 有变化但 <5s → 抑制
    mono.advance(4)                     # t=6，距首打 6s
    r.aggregate("2/3 · a 2/3")          # ≥5s 且距上次打出已 6s → 打
    assert len(lines) == 2
    assert lines[0] == "[pipeline +0s] 1/3 · a 1/3"
    assert lines[1].endswith("2/3 · a 2/3")


def test_log_event_kv_and_iso_timestamp(tmp_path):
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    r.event("chunk_done", video="10b5dd2f", idx=3, worker="w0", proc_s=28.3)
    text = (tmp_path / "pipeline.log").read_text(encoding="utf-8")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3} chunk_done "
        r"video=10b5dd2f idx=3 worker=w0 proc_s=28.3\n", text)


def test_run_start_end_separators(tmp_path):
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    r.run_start(inputs=2, workers=2)
    r.run_end(done=1, qc_failed=1, failed=0, wall_s=12.5)
    lines_out = (tmp_path / "pipeline.log").read_text(
        encoding="utf-8").splitlines()
    assert lines_out[0] == "=== run start ==="
    assert any(l.split(" ", 1)[1].startswith("run_start inputs=2") for l in lines_out)
    assert any(l.split(" ", 1)[1].startswith("run_end done=1") for l in lines_out)
    assert lines_out[-1] == "=== run end ==="


def test_log_write_failure_swallowed(tmp_path):
    mono, lines = FakeClock(), []
    r = ProgressReporter(console=lines.append,
                         log_path=tmp_path / "no-such-dir" / "x.log",
                         now_mono=mono, now_wall=mono)
    r.run_start(inputs=1)      # 目录不存在 → 静默，不抛
    r.event("chunk_done", video="v", idx=0)
    r.run_end(done=1)
    assert lines == []          # log 失败不影响 console 通道（此处无输出）


def test_console_none_disables_console(tmp_path):
    mono = FakeClock()
    r = ProgressReporter(console=None, log_path=tmp_path / "p.log",
                         now_mono=mono, now_wall=mono)
    r.line("不应出现")
    r.aggregate("1/2")
    assert r.event("run_start", inputs=1) is None   # 不抛即过


def test_snapshot_roundtrip(tmp_path):
    cp = SqliteControlPlane(str(tmp_path / "p.db"))
    cp.init_db()
    vid = cp.submit_video("in/a.mp4", "out/a.mp4",
                          [(0, "in/c0", "out/o0"), (1, "in/c1", "out/o1"),
                           (2, "in/c2", "out/o2")])
    c0 = cp.claim_chunk("w0", 900)
    c1 = cp.claim_chunk("w0", 900)
    cp.complete_chunk(c0["id"], {"ms": 100})
    cp.fail_chunk(c1["id"], "boom")            # attempt 0→1 回 pending
    snap = progress_snapshot(str(tmp_path / "p.db"))
    assert snap is not None
    videos, chunks = snap
    assert len(videos) == 1
    v = videos[0]
    assert (v.video_id, v.total, v.done) == (vid, 3, 1)
    by_idx = {c.idx: c for c in chunks}
    assert by_idx[0].status == "done"
    assert by_idx[0].worker_id == "w0"          # complete 后保留持有者
    assert by_idx[0].started_at is not None and by_idx[0].finished_at is not None
    assert (by_idx[1].status, by_idx[1].attempt, by_idx[1].worker_id) == ("pending", 1, None)
    assert (by_idx[2].status, by_idx[2].attempt) == ("pending", 0)


def test_snapshot_bad_path_returns_none(tmp_path):
    assert progress_snapshot(str(tmp_path / "ghost.db")) is None


def test_chunk_diff_rules():
    prev = {("v", 0): ChunkState("v", 0, "done", 0, "w0", 1.0, 2.0),
            ("v", 1): ChunkState("v", 1, "running", 0, "w1", 3.0, None),
            ("v", 2): ChunkState("v", 2, "pending", 1, None, None, None)}
    cur = [ChunkState("v", 0, "done", 0, "w0", 1.0, 2.0),      # 不变 → 排除
           ChunkState("v", 1, "done", 0, "w1", 3.0, 4.0),      # 状态变 → 收
           ChunkState("v", 2, "pending", 2, None, None, None),  # attempt 变 → 收
           ChunkState("v", 3, "running", 0, "w2", 5.0, None)]  # 新行 → 收
    diff = chunk_diff(prev, cur)
    assert [c.idx for c in diff] == [1, 2, 3]


def test_format_progress():
    vs = [VideoState("v1", "/x/a_5min.mp4", "processing", 10, 10),
          VideoState("v2", "/x/b_30s.mp4", "merging", 2, 1),
          VideoState("v3", "/x/c.mp4", "qc_wait", 1, 0)]
    assert format_progress(vs) == "11/13 · a_5min ✔ · b_30s 1/2 · c 0/1"
    assert format_progress([]) == "0/0"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/test_progress.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'faceblur_lite.progress'`

- [ ] **Step 3: Write progress.py**

`src/faceblur_lite/progress.py` 全文：

```python
"""pipeline 运行进度与日志（spec: docs/superpowers/specs/
2026-09-25-pipeline-progress-design.md，v2 双出口）。

双出口都是尽力而为：快照异常静默跳过，写失败静默吞——
进度绝不反噬主流程。"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ChunkState:
    video_id: str
    idx: int
    status: str
    attempt: int
    worker_id: str | None
    started_at: float | None
    finished_at: float | None


@dataclass(frozen=True)
class VideoState:
    video_id: str
    input_path: str
    status: str
    total: int
    done: int


def progress_snapshot(db_path: str) -> tuple[list[VideoState],
                                              list[ChunkState]] | None:
    """只读旁路快照（stats.py 同款 mode=ro，不进 ControlPlane 契约）。
    任何异常返回 None——调用方静默跳过本 tick。"""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            videos = [
                VideoState(r["id"], r["input_path"], r["status"],
                           r["total"], r["done"])
                for r in con.execute(
                    "SELECT v.id, v.input_path, v.status, "
                    "COUNT(c.id) AS total, "
                    "COALESCE(SUM(c.status = 'done'), 0) AS done "
                    "FROM videos v LEFT JOIN chunks c ON c.video_id = v.id "
                    "GROUP BY v.id")]
            chunks = [
                ChunkState(r["video_id"], r["idx"], r["status"], r["attempt"],
                           r["worker_id"], r["started_at"], r["finished_at"])
                for r in con.execute(
                    "SELECT video_id, idx, status, attempt, worker_id, "
                    "started_at, finished_at FROM chunks")]
        finally:
            con.close()
        return videos, chunks
    except Exception:                                   # noqa: BLE001
        return None


def chunk_diff(prev: dict[tuple[str, int], ChunkState],
               cur: list[ChunkState]) -> list[ChunkState]:
    """状态或尝试次数发生变化的 chunk（tick 跨级跳变按行级终态补发）。"""
    return [c for c in cur
            if (p := prev.get((c.video_id, c.idx))) is None
            or p.status != c.status or p.attempt != c.attempt]


def format_progress(videos: list[VideoState]) -> str:
    """聚合行：'11/13 · a_5min ✔ · b_30s 1/2'（完成打 ✔，进行中打 x/y）。"""
    if not videos:
        return "0/0"
    total = sum(v.total for v in videos)
    done = sum(v.done for v in videos)
    parts = [f"{Path(v.input_path).stem} ✔" if v.done >= v.total
             else f"{Path(v.input_path).stem} {v.done}/{v.total}"
             for v in videos]
    return f"{done}/{total} · " + " · ".join(parts)


class ProgressReporter:
    """控制台（即时事件 + 节流聚合）与文件日志（全量）双 sink。
    时钟与两个出口均依赖注入——节流与格式可单测。"""

    def __init__(self, console: Callable[[str], None] | None,
                 log_path: str | Path | None,
                 now_mono: Callable[[], float] = time.monotonic,
                 now_wall: Callable[[], float] = time.time,
                 min_interval: float = 5.0):
        self._console = console
        self._log_path = Path(log_path) if log_path else None
        self._mono, self._wall = now_mono, now_wall
        self._min_interval = min_interval
        self._t0 = now_mono()
        self._last_agg_at = float("-inf")
        self._last_agg = ""

    # ---- 控制台（stderr，粗粒度） ----
    def line(self, text: str) -> None:
        """即时事件行（提交/重试/qc/终态/收尾）。"""
        if self._console is None:
            return
        try:
            self._console(f"[pipeline +{int(self._mono() - self._t0)}s] {text}")
        except Exception:                               # noqa: BLE001
            pass

    def aggregate(self, text: str) -> None:
        """聚合进度行：距上次打出 ≥min_interval 且文本有变化才打（首次即打）。"""
        now = self._mono()
        if text == self._last_agg or now - self._last_agg_at < self._min_interval:
            return
        self._last_agg_at, self._last_agg = now, text
        self.line(text)

    # ---- 文件日志（workdir/pipeline.log，全量） ----
    def event(self, name: str, **kv) -> None:
        """全量事件：ISO 毫秒时间 + key=value，追加写。失败静默。"""
        if self._log_path is None:
            return
        try:
            ts = datetime.fromtimestamp(self._wall()).isoformat(
                timespec="milliseconds")
            fields = " ".join(f"{k}={v}" for k, v in kv.items())
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(f"{ts} {name} {fields}".rstrip() + "\n")
        except Exception:                               # noqa: BLE001
            pass

    def run_start(self, **kv) -> None:
        self._raw("=== run start ===")
        self.event("run_start", **kv)

    def run_end(self, **kv) -> None:
        self.event("run_end", **kv)
        self._raw("=== run end ===")

    def _raw(self, text: str) -> None:
        if self._log_path is None:
            return
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:                               # noqa: BLE001
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_progress.py -q`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/progress.py tests/test_progress.py
git commit -m "feat(progress): 快照/diff/双 sink ProgressReporter（spec v2）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: orchestrator 接线——巡检挂 diff，L1 事件直打

**Files:**
- Modify: `src/faceblur_lite/orchestrator.py`（`_orchestrate` 全函数重写；imports 增补）
- Test: `tests/test_pipeline.py`（文末追加 2 个集成测试）

**Interfaces:**
- Consumes: Task 1 全部产物（签名见 Task 1 Produces）
- Produces: `_orchestrate` 行为变更——stderr 进度行 + `workdir/pipeline.log`；返回值 `list[PipelineResult]` **不变**

- [ ] **Step 1: Write the failing integration tests**

`tests/test_pipeline.py` 文末追加：

```python
def test_pipeline_progress_console_and_log(tmp_path, capsys, move_video):
    """进度双出口：stderr 粗粒度行 + workdir/pipeline.log 全量事件。"""
    out = str(tmp_path / "prog.mp4")
    res = run_pipeline(move_video[0], out, str(tmp_path / "wd"),
                       workers=2, segment_s=2.0,
                       params=KernelParams(det_every=2))
    assert res.status == "done"
    err = capsys.readouterr().err
    assert "[pipeline +0s] 提交 1 视频 → " in err
    assert "qc " in err
    assert "收尾：1 done · 0 qc_failed · 0 failed" in err
    log = (tmp_path / "wd" / "pipeline.log").read_text(encoding="utf-8")
    assert log.startswith("=== run start ===")
    assert "run_start inputs=1" in log
    assert "video_submit video=" in log
    assert log.count("chunk_done video=") == res.chunk_count
    assert "qc_result video=" in log
    assert "run_end done=1 qc_failed=0 failed=0" in log
    assert log.rstrip().endswith("=== run end ===")


def test_pipeline_log_appends_across_runs(tmp_path, move_video):
    """追加语义 + 快照按本轮 video_id 过滤（复用 workdir 不吃旧行）。"""
    wd = str(tmp_path / "wd")
    r1 = run_pipeline(move_video[0], str(tmp_path / "a.mp4"), wd,
                      workers=1, segment_s=3.0)
    r2 = run_pipeline(move_video[0], str(tmp_path / "b.mp4"), wd,
                      workers=1, segment_s=3.0)
    assert r1.status == "done" and r2.status == "done"
    log = (Path(wd) / "pipeline.log").read_text(encoding="utf-8")
    assert log.count("=== run start ===") == 2
    assert log.count("=== run end ===") == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/test_pipeline.py::test_pipeline_progress_console_and_log -x -q`
Expected: FAIL — stderr 无 `[pipeline` 前缀行 / pipeline.log 不存在

- [ ] **Step 3: Rewrite _orchestrate with progress wiring**

`src/faceblur_lite/orchestrator.py`——头部 imports 改为：

```python
import multiprocessing as mp
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from faceblur_lite.kernel import KernelParams
from faceblur_lite.progress import (ChunkState, ProgressReporter, VideoState,
                                    chunk_diff, format_progress,
                                    progress_snapshot)
from faceblur_lite.qc import QCReport, qc_video
from faceblur_lite.splitter import split_video
from faceblur_lite.state import SqliteControlPlane
from faceblur_lite.worker import worker_loop
```

`_orchestrate` 整函数替换为（`_output_for` 及其余函数不动）：

```python
def _fmt_dur(s: float) -> str:
    return f"{int(s // 60)}m{int(s % 60):02d}s" if s >= 60 else f"{int(s)}s"


def _prog_db_path(workdir: str, cp_url: str | None) -> str | None:
    """快照库选择（与 blur.sh report_pipeline 同规则）：--go 且 workdir
    下有 gocp.db → 它；sqlite 模式 → pipeline.db；否则 None（仅 L1 事件）。"""
    wd = Path(workdir)
    if cp_url:
        return str(wd / "gocp.db") if (wd / "gocp.db").is_file() else None
    return str(wd / "pipeline.db")


def _orchestrate(specs: list[tuple[str, str]], workdir: str,
                 workers: int, segment_s: float,
                 params: KernelParams | None,
                 qc_sample_fps: float,
                 cp_url: str | None = None) -> list[PipelineResult]:
    t0 = time.time()
    p = params or KernelParams()
    if cp_url:      # Go 控制面模式（Task 12）：注入 HTTP 适配器，其余不变
        from faceblur_lite.remote import HttpControlPlane
        cp = HttpControlPlane(cp_url)
    else:           # 默认：SQLite 内嵌控制面
        cp = SqliteControlPlane(str(Path(workdir) / "pipeline.db"))
    cp.init_db()

    # 进度双出口（spec 2026-09-25 v2）：stderr 粗粒度 + pipeline.log 全量
    prog_db = _prog_db_path(workdir, cp_url)
    prog = ProgressReporter(console=lambda s: print(s, file=sys.stderr),
                            log_path=Path(workdir) / "pipeline.log")
    prog.run_start(inputs=len(specs), workers=workers,
                   segment_s=segment_s, det_every=p.det_every,
                   cp="http" if cp_url else "sqlite")

    # 1) 逐视频切片+提交——失败隔离：单视频失败记结果，批次继续（R10）
    submitted: list[tuple[str, int]] = []   # (video_id, chunk_count)
    submit_errors: list[tuple[str, str, str]] = []  # (input, output, error)
    for k, (inp, outp) in enumerate(specs):
        try:
            chunk_dir = Path(workdir) / "chunks" / f"{k:02d}_{Path(inp).stem}"
            # 快路径不切不建目录——chunk 输出目录由编排器负责（2026-09-24 执行期修正）
            chunk_dir.mkdir(parents=True, exist_ok=True)
            files = split_video(inp, str(chunk_dir), segment_s)
            items = [(i, f, str(chunk_dir / f"blurred_{i:03d}.mp4"))
                     for i, f in enumerate(files)]
            vid = cp.submit_video(inp, outp, items)
            submitted.append((vid, len(items)))
            prog.event("video_submit", video=vid, input=Path(inp).name,
                       output=Path(outp).name, chunks=len(items))
        except Exception as e:              # noqa: BLE001
            submit_errors.append((inp, outp, f"{type(e).__name__}: {e}"))
            prog.line(f"✖ 提交失败：{Path(inp).name}（{type(e).__name__}）")
    prog.line(f"提交 {len(submitted)} 视频 → "
              f"{sum(n for _, n in submitted)} chunks"
              f"（workers ×{workers}，segment {segment_s:g}s）")

    # 2) 共享 Worker 池：一个池消化所有视频的 chunk（队列天然交错）
    procs = []
    for n in range(workers):
        proc = mp.Process(target=worker_loop, args=(cp, f"w{n}", p),
                          kwargs={"idle_exit_s": 3.0},
                          daemon=True, name=f"worker-{n}")
        proc.start()
        procs.append(proc)

    # 3) 巡检：lease 回收 + 只读快照 diff 出进度（单视频 failed 不终止批次）
    vid_set = {vid for vid, _ in submitted}
    prev_chunks: dict[tuple[str, int], ChunkState] = {}
    prev_videos: dict[str, VideoState] = {}
    while any(pr.is_alive() for pr in procs):
        reclaimed = cp.reclaim_expired()
        if reclaimed:
            prog.event("lease_reclaim", count=reclaimed)
        snap = progress_snapshot(prog_db) if prog_db else None
        if snap is not None:
            videos, chunks = snap
            mine_v = [v for v in videos if v.video_id in vid_set]
            mine_c = [c for c in chunks if c.video_id in vid_set]
            stems = {v.video_id: Path(v.input_path).stem for v in mine_v}
            for c in chunk_diff(prev_chunks, mine_c):
                if c.status == "running":
                    prog.event("chunk_start", video=c.video_id, idx=c.idx,
                               attempt=c.attempt, worker=c.worker_id or "?")
                elif c.status == "done":
                    kv = dict(video=c.video_id, idx=c.idx, attempt=c.attempt,
                              worker=c.worker_id or "?")
                    if c.started_at and c.finished_at:
                        kv["proc_s"] = round(c.finished_at - c.started_at, 1)
                    prog.event("chunk_done", **kv)
                elif c.status == "failed":
                    prog.event("chunk_failed", video=c.video_id, idx=c.idx,
                               attempt=c.attempt)
                pc = prev_chunks.get((c.video_id, c.idx))
                if (pc is not None and c.attempt > pc.attempt
                        and c.status == "pending"):
                    prog.line(f"↻ {stems.get(c.video_id, c.video_id)} "
                              f"chunk#{c.idx} 第 {c.attempt + 1} 次尝试")
            for v in mine_v:
                pv = prev_videos.get(v.video_id)
                if pv is not None and pv.status != v.status:
                    prog.event("video_status", video=v.video_id,
                               **{"from": pv.status, "to": v.status})
            if mine_v:
                prog.aggregate(format_progress(mine_v))
            prev_chunks = {(c.video_id, c.idx): c for c in mine_c}
            prev_videos = {v.video_id: v for v in mine_v}
        time.sleep(0.5)
    for pr in procs:
        pr.join()

    # 4) drain 后逐视频质检收尾
    results: list[PipelineResult] = []
    n_done = n_qcf = n_fail = 0
    for vid, nchunks in submitted:
        video = cp.get_video(vid)
        wall = time.time() - t0
        stem = Path(video["input_path"]).stem
        if video["status"] == "qc_wait":
            prog.line(f"qc {stem} …")
            rep = qc_video(video["output_path"], sample_fps=qc_sample_fps)
            status = "done" if rep.passed else "qc_failed"
            cp.finish_video(vid, status)
            prog.event("qc_result", video=vid, sampled=rep.sampled_frames,
                       violations=len(rep.violations))
            prog.line(f"qc {stem} {'✅' if rep.passed else '❌'} "
                      f"{len(rep.violations)} 违规（抽 {rep.sampled_frames} 帧）")
            n_done += rep.passed
            n_qcf += not rep.passed
            results.append(PipelineResult(
                vid, video["input_path"], video["output_path"],
                status, rep, wall, nchunks))
        else:   # failed / Worker 池退出时仍未完成（如回收发生在退出边缘）
            err = video["error"] or "Worker 池退出时任务未到 qc_wait"
            prog.line(f"✖ {stem} failed：{err}")
            n_fail += 1
            results.append(PipelineResult(
                vid, video["input_path"], video["output_path"],
                video["status"], None, wall, nchunks, err))
    n_fail += len(submit_errors)
    for inp, outp, err in submit_errors:
        results.append(PipelineResult(
            "", inp, outp, "failed", None, time.time() - t0, 0, err))
    prog.line(f"收尾：{n_done} done · {n_qcf} qc_failed · {n_fail} failed · "
              f"总耗时 {_fmt_dur(time.time() - t0)}")
    prog.run_end(done=n_done, qc_failed=n_qcf, failed=n_fail,
                 wall_s=round(time.time() - t0, 1))
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_pipeline.py -q`
Expected: 全部 passed（原 6 + 新 2）

- [ ] **Step 5: 全量回归**

Run: `./venv/bin/pytest -q`
Expected: 97 passed（87 + Task 1 的 10 + 新 2；如有已知 flake `test_batch_same_stem_collision_isolated` 复跑一次确认）

- [ ] **Step 6: Commit**

```bash
git add src/faceblur_lite/orchestrator.py tests/test_pipeline.py
git commit -m "feat(pipeline): 编排器接进度双出口——巡检 diff + L1 事件直打

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: blur.sh 日志路径提示 + 文档同步

**Files:**
- Modify: `scripts/blur.sh:164-166`（`report_pipeline` 批量指标块后）
- Modify: `docs/pipeline-guide.md`（§7.2 观测出口 bullet 后加进度 bullet；§7 实操入口样例后加控制台样例）
- Test: `tests/test_blur_sh.py:65` 附近（既有 pipeline 用例加一行断言）

**Interfaces:**
- Consumes: Task 2 落地的 `workdir/pipeline.log`
- Produces: 无代码接口——纯提示与文档

- [ ] **Step 1: 加失败断言（先红）**

`tests/test_blur_sh.py` 中既有用例（≈54 行，`≥2 输入 auto→pipeline`）的断言区（`assert "── 批量指标 ──" in r.stdout` 之后）追加：

```python
    assert "详细日志" in r.stdout            # 进度日志路径提示（spec v2）
```

Run: `./venv/bin/pytest "tests/test_blur_sh.py::test_auto_mode_two_inputs_pipeline" -q`（以实际测试函数名为准——54 行注释所在用例）
Expected: FAIL（blur.sh 尚未 echo）

- [ ] **Step 2: blur.sh report_pipeline 加提示**

`scripts/blur.sh` `report_pipeline` 中，`fi`（164 行）与 `echo ""`（165 行）之间插入：

```bash
  if [ -f "$1/pipeline.log" ]; then
    echo "详细日志：$1/pipeline.log"
  fi
```

- [ ] **Step 3: 文档同步**

`docs/pipeline-guide.md` §7.2「观测出口（stats）」bullet 之后追加 bullet：

```markdown
- **运行进度与日志（spec 2026-09-25）**：运行期 stderr 打粗粒度进度——`[pipeline +Ns]` 前缀，提交汇总、聚合行（`x/y · 视频 x/y`，≥5s 且有变化才打）、↻ 重试、qc 前后、✖ 终态、收尾汇总；全量细粒度事件追加 `workdir/pipeline.log`（ISO 毫秒时间 + `key=value`：run_start / video_submit / chunk_start / chunk_done / lease_reclaim / video_status / qc_result / run_end，`=== run start/end ===` 头尾分隔，跨轮追加）。数据源 = 父进程巡检 0.5s 只读快照 diff（stats 同款旁路，不进契约，按本轮 video_id 过滤）；快照与写失败一律静默——进度绝不反噬主流程。
```

§7「▸ 实操入口」代码块之后追加样例：

```markdown
> 运行期控制台（stderr）长这样：
> ```
> [pipeline +0s] 提交 2 视频 → 4 chunks（workers ×2，segment 2s）
> [pipeline +5s] 2/4 · move 2/3 · short 0/1
> [pipeline +8s] qc move …
> [pipeline +9s] qc move ✅ 0 违规（抽 3 帧）
> [pipeline +12s] 收尾：2 done · 0 qc_failed · 0 failed · 总耗时 12s
> ```
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_blur_sh.py -q`
Expected: 全部 passed

- [ ] **Step 5: 全量回归 + Commit**

Run: `./venv/bin/pytest -q`
Expected: 98 passed

```bash
git add scripts/blur.sh docs/pipeline-guide.md tests/test_blur_sh.py
git commit -m "feat(blur.sh): 批量收尾 echo pipeline.log 路径；管线指南同步进度双出口

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review 记录

- **Spec 覆盖**：控制台行格式（Task 2 L1+L2）/ 文件日志 8 事件（run_start✓ video_submit✓ chunk_start✓ chunk_done✓ lease_reclaim✓ video_status✓ qc_result✓ run_end✓ + chunk_failed 附赠）/ 追加分隔✓ / 双控面 db 规则✓ / 快照容错✓ / blur.sh 提示✓ / 文档✓ / 契约零变更✓（progress.py 不 import control 协议）
- **类型一致**：`ChunkState`/`VideoState` 字段与 snapshot SQL 列名一一对应；`rep`（QCReport）与 `prog`（ProgressReporter）命名解耦避免遮挡
