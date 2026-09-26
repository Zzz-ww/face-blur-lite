"""控制面编排器：逐视频切片+提交（失败隔离）→ 共享 Worker 池 →
lease 巡检 → drain 后逐视频质检。R3/R10。"""
from __future__ import annotations

import multiprocessing as mp
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from faceblur_lite.kernel import KernelParams
from faceblur_lite.progress import (ChunkState, ProgressReporter, VideoState,
                                    emit_chunk_events, format_progress,
                                    progress_snapshot)
from faceblur_lite.qc import QCReport, qc_video
from faceblur_lite.splitter import split_video
from faceblur_lite.state import SqliteControlPlane
from faceblur_lite.worker import worker_loop


class WorkdirReuseError(RuntimeError):
    """sqlite 模式下 workdir 已含历史视频记录（BUG-017：跨轮复用会
    污染 stats、遗留 pending chunk 会被新 Worker 池误认领）。远端
    控制面（--cp-url）是共享队列语义，豁免。resume=演进 backlog #6）。"""


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


def _fmt_dur(s: float) -> str:
    return f"{int(s // 60)}m{int(s % 60):02d}s" if s >= 60 else f"{int(s)}s"


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
                 qc_detector: str,
                 cp_url: str | None = None) -> list[PipelineResult]:
    t0 = time.time()
    p = params or KernelParams()
    if cp_url:      # Go 控制面模式（Task 12）：注入 HTTP 适配器，其余不变
        from faceblur_lite.remote import HttpControlPlane
        cp = HttpControlPlane(cp_url)
    else:           # 默认：SQLite 内嵌控制面
        cp = SqliteControlPlane(str(Path(workdir) / "pipeline.db"))
    cp.init_db()
    if not cp_url:      # BUG-017：本地 workdir=一次性工件；远端=共享队列豁免
        existing = cp.video_overview()
        if existing:
            raise WorkdirReuseError(
                f"workdir 已含 {len(existing)} 个历史视频记录"
                f"（{Path(workdir) / 'pipeline.db'}）——跨轮复用会污染 stats、"
                f"遗留 pending chunk 会被本轮 Worker 池认领。"
                f"请换新 --workdir，或删除旧 pipeline.db 重来"
                f"（断点续跑=演进项）")

    # 进度双出口（spec 2026-09-25 v2）：stderr 粗粒度 + pipeline.log 全量
    prog_db = _prog_db_path(workdir, cp_url)
    Path(workdir).mkdir(parents=True, exist_ok=True)  # gocp 直连 CLI 时无人建 workdir——日志要能落盘
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
            emit_chunk_events(prog, prev_chunks, mine_c, stems)
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
            rep = qc_video(video["output_path"], sample_fps=qc_sample_fps,
                           detector=qc_detector)
            status = "done" if rep.passed else "qc_failed"
            final_path = video["output_path"]
            suffix = ""
            q_err: str | None = None
            if not rep.passed:
                try:
                    final_path = _quarantine(final_path)
                    suffix = " → 已隔离 qc_failed/"
                except OSError as e:    # 隔离失败不砸批次——缺陷可见
                    q_err = str(e)
                    suffix = f" —— 隔离失败（{type(e).__name__}），文件留原位"
            cp.finish_video(vid, status, output_path=final_path)
            prog.event("qc_result", video=vid, sampled=rep.sampled_frames,
                       violations=len(rep.violations),
                       fatal=rep.fatal_count, marginal=rep.marginal_count)
            if not rep.passed:
                if q_err is None:
                    prog.event("quarantine", video=vid,
                               to=Path(final_path).name)
                else:
                    prog.event("quarantine_failed", video=vid, error=q_err)
            if rep.passed:
                prog.line(f"qc {stem} ✅ 0 违规（抽 {rep.sampled_frames} 帧）")
            else:
                prog.line(f"qc {stem} ❌ {len(rep.violations)} 违规"
                          f"（致命 {rep.fatal_count} · 边缘 "
                          f"{rep.marginal_count}，抽 {rep.sampled_frames} 帧）"
                          f"{suffix}")
            n_done += rep.passed
            n_qcf += not rep.passed
            results.append(PipelineResult(
                vid, video["input_path"], final_path,
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


def run_batch(inputs: list[str], outdir: str, workdir: str,
              workers: int = 2, segment_s: float = 30.0,
              params: KernelParams | None = None,
              qc_sample_fps: float = 1.0,
              qc_detector: str = "yunet",
              cp_url: str | None = None) -> list[PipelineResult]:
    """批量：输出到 outdir/<stem>_blurred.mp4。R10。

    同 stem 输出碰撞（相机卡两目录各一张 DSC_0001 场景）：后到者显式
    failed（复用提交期失败隔离形状），先到者正常处理，不静默重命名。"""
    t0 = time.time()
    specs: list[tuple[str, str]] = []
    collisions: list[PipelineResult] = []
    seen: dict[str, str] = {}                    # 输出路径 → 首个占用输入
    for inp in inputs:
        outp = _output_for(inp, outdir)
        if outp in seen:
            collisions.append(PipelineResult(
                "", inp, outp, "failed", None, time.time() - t0, 0,
                f"输出名碰撞: {inp} 与 {seen[outp]} 最终输出同为 {outp}"
                "，后到者显式失败（先到者正常处理）"))
        else:
            seen[outp] = inp
            specs.append((inp, outp))
    if not specs:                                # 全部碰撞：无需起 Worker 池
        return collisions
    return _orchestrate(specs, workdir, workers, segment_s,
                        params, qc_sample_fps, qc_detector, cp_url) + collisions


def run_pipeline(input_path: str, output_path: str, workdir: str,
                 workers: int = 2, segment_s: float = 30.0,
                 params: KernelParams | None = None,
                 qc_sample_fps: float = 1.0,
                 qc_detector: str = "yunet",
                 cp_url: str | None = None) -> PipelineResult:
    """单视频便捷封装（显式输出路径）。"""
    return _orchestrate([(input_path, output_path)], workdir, workers,
                        segment_s, params, qc_sample_fps, qc_detector,
                        cp_url)[0]
