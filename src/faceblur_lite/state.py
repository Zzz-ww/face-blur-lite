"""SqliteControlPlane——控制面契约的参考实现（SQLite WAL）。

方法语义与 docs/control-plane-contract.md 一一对应；每方法短连接+事务，
实例只含 db_path（可 pickle，spawn 传参安全）。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class SqliteControlPlane:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS videos(
                id TEXT PRIMARY KEY,
                input_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                merged_path TEXT,
                status TEXT NOT NULL,
                error TEXT,
                created_at REAL NOT NULL,
                finished_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS chunks(
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
                UNIQUE(video_id, idx))""")

    def submit_video(self, input_path: str, final_output: str,
                     chunk_inputs: list[tuple[int, str, str]]) -> str:
        """chunk_inputs: [(idx, chunk输入, chunk输出)]。返回 video_id。"""
        vid = uuid.uuid4().hex[:12]
        now = time.time()
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO videos(id,input_path,output_path,merged_path,"
                "status,error,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                (vid, input_path, final_output, None, "processing",
                 None, now, None))
            c.executemany(
                "INSERT INTO chunks(id,video_id,idx,input_path,output_path,"
                "status,attempt,worker_id,lease_until,metrics,enqueued_at,"
                "started_at,finished_at,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(uuid.uuid4().hex[:12], vid, i, cin, cout, "pending", 0,
                  None, None, None, now, None, None, None)
                 for i, cin, cout in chunk_inputs])
        return vid

    def claim_chunk(self, worker_id: str, ttl_s: float) -> dict | None:
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                """SELECT c.* FROM chunks c JOIN videos v ON c.video_id = v.id
                   WHERE c.status='pending' AND v.status='processing'
                   ORDER BY v.created_at, c.idx LIMIT 1""").fetchone()
            if row is None:
                return None
            now = time.time()
            c.execute(
                """UPDATE chunks SET status='running', worker_id=?,
                   lease_until=?, started_at=? WHERE id=? AND status='pending'""",
                (worker_id, now + ttl_s, now, row["id"]))
            out = dict(row)
            out.update(status="running", worker_id=worker_id,
                       lease_until=now + ttl_s)
            return out

    def complete_chunk(self, chunk_id: str, metrics: dict) -> bool:
        """置 done；若该 video 全部 done，同一事务内认领 merge。

        并发下恰好一个调用者拿到 True——原子 Join（scatter-gather 门控）。
        仅 running 态受理：迟到 ack（chunk 已被重派或终态）返回 False。
        """
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM chunks WHERE id=?",
                            (chunk_id,)).fetchone()
            cur = c.execute(
                "UPDATE chunks SET status='done', metrics=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (json.dumps(metrics), time.time(), chunk_id))
            if cur.rowcount == 0:
                return False
            left = c.execute(
                "SELECT COUNT(*) FROM chunks WHERE video_id=? AND status!='done'",
                (row["video_id"],)).fetchone()[0]
            if left == 0:
                cur = c.execute(
                    "UPDATE videos SET status='merging' "
                    "WHERE id=? AND status='processing'",
                    (row["video_id"],))
                return cur.rowcount == 1
            return False

    def complete_merge(self, video_id: str, merged_path: str) -> None:
        with _connect(self.db_path) as c:
            c.execute(
                "UPDATE videos SET status='qc_wait', merged_path=? WHERE id=?",
                (merged_path, video_id))

    def fail_chunk(self, chunk_id: str, error: str,
                   max_attempts: int = 3) -> None:
        """终态（done/failed）不可逆：迟到 fail 不受理（BUG-007 场景——
        merge 崩溃后对已完成 chunk 的 fail 不得把 done 翻回 pending）。
        每次受理 attempt+1（含耗尽分支——终态记录最终计数，与 reclaim 对齐）。"""
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM chunks WHERE id=?",
                            (chunk_id,)).fetchone()
            if row is None or row["status"] in ("done", "failed"):
                return
            now = time.time()
            if row["attempt"] + 1 >= max_attempts:
                c.execute(
                    "UPDATE chunks SET status='failed', attempt=attempt+1, "
                    "error=?, finished_at=? WHERE id=?",
                    (error, now, chunk_id))
                c.execute(
                    "UPDATE videos SET status='failed', error=?, "
                    "finished_at=? WHERE id=? AND status='processing'",
                    (error, now, row["video_id"]))
            else:
                c.execute(
                    """UPDATE chunks SET status='pending', attempt=attempt+1,
                       worker_id=NULL, lease_until=NULL, error=? WHERE id=?""",
                    (error, chunk_id))

    def reclaim_expired(self, max_attempts: int = 3) -> int:
        """lease 过期的 running chunk → pending 重派；耗尽 → video failed。
        仅回收 video 仍 processing 的 chunk：终态视频的失败原因不被改写。"""
        now = time.time()
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                """SELECT c.* FROM chunks c JOIN videos v ON c.video_id = v.id
                   WHERE c.status='running' AND c.lease_until < ?
                   AND v.status='processing'""", (now,)).fetchall()
            for r in rows:
                if r["attempt"] + 1 >= max_attempts:
                    c.execute(
                        "UPDATE chunks SET status='failed', attempt=attempt+1 "
                        "WHERE id=?", (r["id"],))
                    c.execute(
                        "UPDATE videos SET status='failed', "
                        "error='lease 过期且重试耗尽', finished_at=? "
                        "WHERE id=? AND status='processing'",
                        (now, r["video_id"]))
                else:
                    c.execute(
                        """UPDATE chunks SET status='pending',
                           attempt=attempt+1, worker_id=NULL,
                           lease_until=NULL WHERE id=?""", (r["id"],))
            return len(rows)

    def get_video(self, video_id: str) -> dict:
        with _connect(self.db_path) as c:
            row = c.execute("SELECT * FROM videos WHERE id=?",
                            (video_id,)).fetchone()
            if row is None:
                raise KeyError(video_id)
            return dict(row)

    def get_chunk(self, chunk_id: str) -> dict:
        with _connect(self.db_path) as c:
            row = c.execute("SELECT * FROM chunks WHERE id=?",
                            (chunk_id,)).fetchone()
            if row is None:
                raise KeyError(chunk_id)
            return dict(row)

    def chunk_outputs(self, video_id: str) -> list[str]:
        with _connect(self.db_path) as c:
            rows = c.execute(
                "SELECT output_path FROM chunks WHERE video_id=? ORDER BY idx",
                (video_id,)).fetchall()
            return [r["output_path"] for r in rows]

    def video_overview(self) -> list[dict]:
        with _connect(self.db_path) as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM videos ORDER BY created_at").fetchall()]

    def finish_video(self, video_id: str, status: str,
                     output_path: str | None = None) -> None:
        with _connect(self.db_path) as c:
            c.execute(
                "UPDATE videos SET status=?, finished_at=?, "
                "output_path=COALESCE(?, output_path) WHERE id=?",
                (status, time.time(), output_path, video_id))
