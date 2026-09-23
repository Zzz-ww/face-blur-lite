"""控制面编排器：逐视频切片+提交（失败隔离）→ 共享 Worker 池 →
lease 巡检 → drain 后逐视频质检。R3/R10。"""
from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import dataclass
from pathlib import Path

from faceblur_lite.kernel import KernelParams
from faceblur_lite.qc import QCReport, qc_video
from faceblur_lite.splitter import split_video
from faceblur_lite.state import SqliteControlPlane
from faceblur_lite.worker import worker_loop


@dataclass
class PipelineResult:
    video_id: str
    input_path: str
    output_path: str
    status: str
    qc: QCReport | None
    wall_s: float
    chunk_count: int
    error: str | None = None


def _output_for(input_path: str, outdir: str) -> str:
    Path(outdir).mkdir(parents=True, exist_ok=True)
    return str(Path(outdir) / f"{Path(input_path).stem}_blurred.mp4")


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
        except Exception as e:              # noqa: BLE001
            submit_errors.append((inp, outp, f"{type(e).__name__}: {e}"))

    # 2) 共享 Worker 池：一个池消化所有视频的 chunk（队列天然交错）
    procs = []
    for n in range(workers):
        proc = mp.Process(target=worker_loop, args=(cp, f"w{n}", p),
                          kwargs={"idle_exit_s": 3.0},
                          daemon=True, name=f"worker-{n}")
        proc.start()
        procs.append(proc)

    # 3) 巡检：lease 回收；单视频 failed 不终止批次（失败隔离）
    while any(pr.is_alive() for pr in procs):
        cp.reclaim_expired()
        time.sleep(0.5)
    for pr in procs:
        pr.join()

    # 4) drain 后逐视频质检收尾
    results: list[PipelineResult] = []
    for vid, nchunks in submitted:
        video = cp.get_video(vid)
        wall = time.time() - t0
        if video["status"] == "qc_wait":
            rep = qc_video(video["output_path"], sample_fps=qc_sample_fps)
            status = "done" if rep.passed else "qc_failed"
            cp.finish_video(vid, status)
            results.append(PipelineResult(
                vid, video["input_path"], video["output_path"],
                status, rep, wall, nchunks))
        else:   # failed / Worker 池退出时仍未完成（如回收发生在退出边缘）
            results.append(PipelineResult(
                vid, video["input_path"], video["output_path"],
                video["status"], None, wall, nchunks,
                video["error"] or "Worker 池退出时任务未到 qc_wait"))
    for inp, outp, err in submit_errors:
        results.append(PipelineResult(
            "", inp, outp, "failed", None, time.time() - t0, 0, err))
    return results


def run_batch(inputs: list[str], outdir: str, workdir: str,
              workers: int = 2, segment_s: float = 30.0,
              params: KernelParams | None = None,
              qc_sample_fps: float = 1.0,
              cp_url: str | None = None) -> list[PipelineResult]:
    """批量：输出到 outdir/<stem>_blurred.mp4。R10。"""
    specs = [(inp, _output_for(inp, outdir)) for inp in inputs]
    return _orchestrate(specs, workdir, workers, segment_s,
                        params, qc_sample_fps, cp_url)


def run_pipeline(input_path: str, output_path: str, workdir: str,
                 workers: int = 2, segment_s: float = 30.0,
                 params: KernelParams | None = None,
                 qc_sample_fps: float = 1.0,
                 cp_url: str | None = None) -> PipelineResult:
    """单视频便捷封装（显式输出路径）。"""
    return _orchestrate([(input_path, output_path)], workdir, workers,
                        segment_s, params, qc_sample_fps, cp_url)[0]
