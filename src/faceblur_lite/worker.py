"""数据面 Worker：无状态循环——认领任务→执行算子→事务回写。可随时 kill。

只依赖 ControlPlane 协议（依赖注入）：换 Go 控制面/Redis 控制面时
本文件零改动。状态只存在于控制面；Worker 死亡由 lease 回收兜底。
"""
from __future__ import annotations

import time

from faceblur_lite.control import ControlPlane
from faceblur_lite.kernel import KernelParams, process_video
from faceblur_lite.merger import merge_chunks


def worker_loop(cp: ControlPlane, worker_id: str,
                params: KernelParams | None = None,
                poll_s: float = 0.5, idle_exit_s: float = 15.0,
                ttl_s: float = 900.0) -> dict:
    """空闲 idle_exit_s 后退出（demo 级优雅收尾）；idle_exit_s=0 常驻
    永不退出（独立 Worker/容器部署模式，R12）。返回统计。"""
    p = params or KernelParams()
    done, idle = 0, 0.0
    while True:
        task = cp.claim_chunk(worker_id, ttl_s)
        if task is None:
            idle += poll_s
            if idle_exit_s and idle >= idle_exit_s:
                return {"worker": worker_id, "chunks_done": done}
            time.sleep(poll_s)
            continue
        idle = 0.0
        try:
            rep = process_video(task["input_path"], task["output_path"], p)
            merge_claimed = cp.complete_chunk(task["id"], {
                "frames": rep.frames, "detections": rep.detections,
                "predicted_frames": rep.predicted_frames,
                "segments": rep.segments, "wall_s": rep.wall_s})
            if merge_claimed:      # 原子 Join：最后完成者负责合并
                video = cp.get_video(task["video_id"])
                merge_chunks(cp.chunk_outputs(task["video_id"]),
                             video["output_path"])
                cp.complete_merge(task["video_id"], video["output_path"])
            done += 1
        except Exception as e:      # noqa: BLE001 —— 单 chunk 失败不拖死 Worker
            cp.fail_chunk(task["id"], f"{type(e).__name__}: {e}")
