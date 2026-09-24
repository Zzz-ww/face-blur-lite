"""关键帧流复制切片 + 短视频快路径（数据面）。R3。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from faceblur_lite.ffmpeg_io import probe


def split_video(input_path: str, out_dir: str,
                segment_s: float = 30.0) -> list[str]:
    """返回切片路径列表（按 idx 排序）。时长 ≤ segment_s 时快路径不切。

    重跑同目录先清旧 chunk（防残留混入）；数字感知排序
    （%03d 在 1000 片后字典序乱序）。
    """
    if probe(input_path).duration_s <= segment_s:
        return [input_path]
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for old in Path(out_dir).glob("chunk_*.mp4"):   # 清上次残留，防混入
        old.unlink()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", input_path,
         "-c", "copy", "-map", "0", "-f", "segment",
         "-segment_time", str(segment_s), "-reset_timestamps", "1",
         str(Path(out_dir) / "chunk_%03d.mp4")],
        check=True)
    return sorted((str(p) for p in Path(out_dir).glob("chunk_*.mp4")),
                  key=lambda p: int(re.search(r"(\d+)", Path(p).stem).group(1)))
