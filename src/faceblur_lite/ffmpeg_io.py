"""ffmpeg/ffprobe 流式 IO（数据面基础设施）。

R4 输入格式链路：视频文件 --ffmpeg -f rawvideo -pix_fmt bgr24 pipe-->
(H,W,3) BGR uint8 ndarray。解码归 ffmpeg，检测归 ONNX，中间只有 numpy。
"""
from __future__ import annotations

import json
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


class FFmpegReader:
    """逐帧读取。sample_fps 不为 None 时经 fps 滤镜抽帧（质检用）。"""

    def __init__(self, path: str, sample_fps: float | None = None):
        self.path = path
        self.meta = probe(path)
        cmd = ["ffmpeg", "-v", "error", "-i", path]
        if sample_fps is not None:
            cmd += ["-vf", f"fps={sample_fps}"]
        cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    def __enter__(self) -> "FFmpegReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __iter__(self):
        fs = self.meta.width * self.meta.height * 3
        while True:
            buf = self.proc.stdout.read(fs)
            if not buf or len(buf) < fs:
                break
            yield np.frombuffer(buf, np.uint8).reshape(
                self.meta.height, self.meta.width, 3)

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
