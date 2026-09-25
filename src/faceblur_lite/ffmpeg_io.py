"""ffmpeg/ffprobe 流式 IO（数据面基础设施）。

R4 输入格式链路：视频文件 --ffmpeg -f rawvideo -pix_fmt bgr24 pipe-->
(H,W,3) BGR uint8 ndarray。解码归 ffmpeg，检测归 ONNX，中间只有 numpy。
"""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class VideoMeta:
    width: int
    height: int
    fps: float
    fps_str: str            # 精确分数表示，如 "30000/1001"
    nb_frames: int | None = None
    has_audio: bool = False
    duration_s: float = 0.0


def probe(path: str) -> VideoMeta:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", path],
        capture_output=True, text=True, check=True)
    data = json.loads(r.stdout)
    streams = data["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    num, _, den = v["r_frame_rate"].partition("/")
    fps = float(num) / float(den) if den and float(den) != 0 else 30.0
    raw_nb = str(v.get("nb_frames", ""))
    nb = int(raw_nb) if raw_nb.isdigit() else None
    dur = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    return VideoMeta(
        width=int(v["width"]), height=int(v["height"]),
        fps=fps, fps_str=v["r_frame_rate"], nb_frames=nb,
        has_audio=any(s["codec_type"] == "audio" for s in streams),
        duration_s=dur,
    )


def sample_stride_for(fps: float, sample_fps: float) -> int:
    """跨步抽样步长：round(fps/sample_fps) 向上取整到与 2..8 全互素的
    最近值（BUG-016：固定步长会与内核 det_every 节奏锁相——30fps@1fps
    步长 30、det_every=3 时恒抽检测帧，2/3 的预测帧零覆盖。步长与
    lcm(2..8)=840 互素后，抽中帧号 mod d 对一切 d∈2..8 均匀扫遍
    全部残差类。代价（量化）：采样间隔 ≈ m/fps 只增不减，增量 ≤9
    （840 的 Jacobsthal 间隙）——常规 24-60fps 增 1-5 帧（3-21%）；
    round 值落 2..10 时一律跳到 11（低帧率/高 sample_fps 档有效密度
    最多降数倍，qc 的 sampled 计数可观测实际密度）。"""
    m = max(1, round(fps / sample_fps))
    while math.gcd(m, 840) != 1:                 # 840 = lcm(2..8)
        m += 1
    return m


class FFmpegReader:
    """逐帧读取。sample_fps 不为 None 时按帧号跨步抽样（质检用）：
    步长 N=sample_stride_for(fps, sample_fps)（BUG-016：互素取整），
    首窗取中点帧，此后每 N 帧一抽。抽样帧因此必然是原始帧序列的
    逐帧成员，时间戳可精确还原（BUG-010：-vf fps 滤镜在部分容器上
    有恒定相位偏移——实测本仓测试视频 +14 帧——且输出的帧不再与
    原始帧一一同源，时间标签不可信）。"""

    def __init__(self, path: str, sample_fps: float | None = None):
        self.path = path
        self.meta = probe(path)
        self.sample_stride = 1
        self.sample_offset = 0
        if sample_fps is not None and self.meta.fps > 0:
            self.sample_stride = sample_stride_for(self.meta.fps, sample_fps)
            self.sample_offset = self.sample_stride // 2
        cmd = ["ffmpeg", "-v", "error", "-i", path,
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    def __enter__(self) -> "FFmpegReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __iter__(self):
        fs = self.meta.width * self.meta.height * 3
        i = 0
        while True:
            buf = self.proc.stdout.read(fs)
            if not buf or len(buf) < fs:
                break
            if i % self.sample_stride == self.sample_offset:
                yield np.frombuffer(buf, np.uint8).reshape(
                    self.meta.height, self.meta.width, 3)
            i += 1

    def close(self) -> None:
        if self.proc.stdout:
            self.proc.stdout.close()
        self.proc.wait()


class FFmpegWriter:
    """帧 pipe 给 ffmpeg 编码（全局钉死编码参数）。audio_from 给定时
    从原片流复制音轨（绝不重编）。gop 强制关键帧间隔（切片前提）。"""

    def __init__(self, path: str, meta: VideoMeta, crf: int = 20,
                 audio_from: str | None = None, gop: int | None = None):
        self.path = path
        self.meta = meta
        cmd = ["ffmpeg", "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{meta.width}x{meta.height}",
               "-r", meta.fps_str, "-i", "-"]
        if audio_from is not None:
            cmd += ["-i", audio_from, "-map", "0:v:0", "-map", "1:a:0?",
                    "-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        if gop is not None:
            cmd += ["-g", str(gop)]
        cmd.append(path)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        self.proc.stdin.write(np.ascontiguousarray(frame, np.uint8).tobytes())

    def __enter__(self) -> "FFmpegWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.proc.stdin.close()
        ret = self.proc.wait()
        if ret != 0:
            raise RuntimeError(f"ffmpeg 编码失败 rc={ret}: {self.path}")
