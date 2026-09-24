"""最小观测出口（只读旁路，R14）：从控制面库读时间戳/attempt，
输出排队/处理分位、吞吐、失败率、重试计数。

设计要点：不进 ControlPlane 契约——观测读不污染任务状态契约；
SQLite WAL 允许并发只读（gocp 模式同样可直读其库文件）。
血缘 / 时序化（exporter/大盘/告警）为平台期演进（开放细化项 9）。
"""
from __future__ import annotations

import sqlite3


def _pct(values: list[float], p: float) -> float:
    """最近邻分位（demo 级）：空列表返回 0.0。"""
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
    return round(s[k], 3)


def collect_stats(db_path: str) -> dict:
    """只读聚合 videos/chunks。排队=started-enqueued；处理=finished-started。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        chunks = con.execute(
            "SELECT status, attempt, enqueued_at, started_at, finished_at "
            "FROM chunks").fetchall()
        videos = con.execute("SELECT status FROM videos").fetchall()
    finally:
        con.close()

    by_status: dict[str, int] = {}
    for r in chunks:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    queue_s = [r["started_at"] - r["enqueued_at"] for r in chunks
               if r["started_at"] is not None]
    proc_s = [r["finished_at"] - r["started_at"] for r in chunks
              if r["started_at"] is not None and r["finished_at"] is not None]
    fin = [r["finished_at"] for r in chunks if r["finished_at"] is not None]
    enq = [r["enqueued_at"] for r in chunks if r["enqueued_at"] is not None]
    wall = (max(fin) - min(enq)) if fin and enq else 0.0
    done = by_status.get("done", 0)
    return {
        "videos": {s: sum(1 for v in videos if v["status"] == s)
                   for s in {v["status"] for v in videos}},
        "chunks_total": len(chunks),
        "chunks_by_status": by_status,
        "retried": sum(1 for r in chunks if r["attempt"] > 0),
        "queue_s": {"p50": _pct(queue_s, 50), "p95": _pct(queue_s, 95)},
        "proc_s": {"p50": _pct(proc_s, 50), "p95": _pct(proc_s, 95)},
        "throughput_per_min": round(done / (wall / 60), 2) if wall > 0 else 0.0,
        "fail_rate": round(by_status.get("failed", 0) / len(chunks), 4)
                     if chunks else 0.0,
    }


def format_stats(s: dict) -> str:
    v = " ".join(f"{k}={n}" for k, n in sorted(s["videos"].items())) or "-"
    c = " ".join(f"{k}={n}" for k, n in sorted(s["chunks_by_status"].items()))
    return (
        f"videos: {v}\n"
        f"chunks: {s['chunks_total']} {c} retried={s['retried']}\n"
        f"queue_s: p50={s['queue_s']['p50']} p95={s['queue_s']['p95']}\n"
        f"proc_s:  p50={s['proc_s']['p50']} p95={s['proc_s']['p95']}\n"
        f"throughput: {s['throughput_per_min']} chunks/min\n"
        f"fail_rate: {s['fail_rate']:.1%}")
