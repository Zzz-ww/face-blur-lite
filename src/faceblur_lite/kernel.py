"""face-blur 算子内核（数据面）：一个视频文件 → 一个脱敏视频文件。R1。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from faceblur_lite.blur import blur_regions
from faceblur_lite.detector import YuNetDetector
from faceblur_lite.ffmpeg_io import FFmpegReader, FFmpegWriter, probe
from faceblur_lite.tracker import Tracker


@dataclass
class KernelParams:
    det_every: int = 3          # 每 N 帧检测一次，中间帧用跟踪预测补位
    det_conf: float = 0.3       # 源域检测阈值（宁可多糊）
    margin: float = 0.15        # 模糊框外扩比例
    margin_px: int = 8          # 外扩像素下限（BUG-012：小脸比例外扩仅 2-4px 盖不住框抖动）
    crf: int = 20
    high_thr: float = 0.5       # 跟踪高分阈值
    low_thr: float = 0.3        # 跟踪低分阈值（二轮救活下限）
    max_age: int = 3            # 遮挡容忍的检测轮数


@dataclass
class KernelReport:
    frames: int = 0
    detections: int = 0             # 检测帧出框总数
    predicted_frames: int = 0       # 纯预测帧数（无检测的那些帧）
    track_count: int = 0            # 累计 track 数
    segments: list[tuple[float, float]] = field(default_factory=list)
    duration_s: float = 0.0
    wall_s: float = 0.0


def process_video(input_path: str, output_path: str,
                  params: KernelParams | None = None,
                  model_path: str | None = None) -> KernelReport:
    p = params or KernelParams()
    rep = KernelReport()
    t0 = time.time()
    meta = probe(input_path)
    detector = YuNetDetector(model_path, conf_thr=p.det_conf)
    tracker = Tracker(high_thr=p.high_thr, low_thr=p.low_thr, max_age=p.max_age,
                      frame_wh=(meta.width, meta.height))

    face_frames: list[bool] = []
    rd = FFmpegReader(input_path)
    wr = FFmpegWriter(output_path, meta, crf=p.crf,
                      audio_from=input_path if meta.has_audio else None)
    with rd, wr:
        for i, frame in enumerate(rd):
            if i % p.det_every == 0:
                dets = detector.detect(frame)
                rep.detections += len(dets)
                active = tracker.update(dets, gap=p.det_every)
            else:
                active = tracker.predict()
                rep.predicted_frames += 1
            boxes = [t.box for t in active]
            face_frames.append(bool(boxes))
            wr.write(blur_regions(frame, boxes, margin=p.margin,
                                  margin_px=p.margin_px))

    rep.frames = len(face_frames)
    rep.track_count = tracker.total_tracks
    rep.segments = _segments(face_frames, meta.fps)
    rep.duration_s = rep.frames / meta.fps if meta.fps else 0.0
    rep.wall_s = time.time() - t0
    return rep


def _segments(face_frames: list[bool],
              fps: float) -> list[tuple[float, float]]:
    """连续有人脸的帧段 → [(起秒, 止秒)]。"""
    segs: list[tuple[float, float]] = []
    start = None
    for i, has in enumerate(list(face_frames) + [False]):   # 哨兵收尾
        if has and start is None:
            start = i
        elif not has and start is not None:
            segs.append((round(start / fps, 2), round(i / fps, 2)))
            start = None
    return segs
