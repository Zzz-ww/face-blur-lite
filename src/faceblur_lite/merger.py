"""concat 流复制合并，失败降级重编码（数据面）。"""
from __future__ import annotations

import subprocess
from pathlib import Path


def merge_chunks(chunk_paths: list[str], output_path: str) -> str:
    """所有切片由同一套钉死参数编码 → concat + -c copy 秒级合并；
    流复制失败（参数不一致等边界）降级为重编码合并。

    concat 列表条目写绝对路径：调用方给的相对路径以调用方 cwd 为准，
    而 ffmpeg concat demuxer 按列表文件所在目录解析相对条目（2026-09-24
    批量冒烟发现：--outdir outputs --workdir work-smoke 相对路径下合并
    ENOENT，视频永卡 merging）。
    """
    lst = Path(output_path).with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{Path(p).resolve()}'\n"
                           for p in chunk_paths), encoding="utf-8")
    base = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(lst)]
    try:
        subprocess.run(base + ["-c", "copy", output_path], check=True)
    except subprocess.CalledProcessError:
        subprocess.run(
            base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "copy", output_path],
            check=True)
    finally:
        lst.unlink(missing_ok=True)
    return output_path
