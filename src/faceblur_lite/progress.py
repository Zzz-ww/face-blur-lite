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
from urllib.parse import quote


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
        con = sqlite3.connect(f"file:{quote(db_path)}?mode=ro", uri=True)
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
