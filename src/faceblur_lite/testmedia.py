"""合成测试视频（数据面测试基建）：人脸图贴到移动轨迹，可选中段遮挡。"""
from __future__ import annotations

import cv2
import numpy as np

from faceblur_lite.ffmpeg_io import FFmpegWriter, VideoMeta


def load_face(path: str) -> np.ndarray:
    """读入人脸图并裁成特写（2026-09-24 执行期修正）。

    YuNet 只检出 lena 的内脸（512 图坐标约 (201,186)-(355,388)，实测）；
    不裁的话贴图大部分是帽/肩/背景，检测框+margin 足迹只覆盖贴图约 1/3，
    模糊覆盖率断言（std 对比窗口≈整个贴图）数学上不可满足（下限 0.877）。
    裁成特写让 贴图≈检测框，"人脸区域方差显著下降"的断言语义才成立。
    """
    img = cv2.imread(path)
    if img is None:
        raise FileNotFoundError(path)
    return img[170:400, 185:385]


def build_video(out_path: str, face_img: np.ndarray, scenario: str = "move",
                duration_s: float = 6.0, fps: int = 10,
                size: tuple[int, int] = (640, 480),
                face_w: int = 120) -> tuple[list[np.ndarray], list[bool]]:
    """生成合成视频，返回 (帧列表, 每帧人脸是否被遮挡)。

    move:    人脸从左 1/5 匀速移到右 4/5
    occlude: 同上，中段 1/3 时段画灰矩形盖住人脸（模拟手臂遮挡，R2）
    """
    w, h = size
    n = int(duration_s * fps)
    fh = int(face_w * face_img.shape[0] / face_img.shape[1])
    face = cv2.resize(face_img, (face_w, fh))
    x0, x1 = w // 5, w * 4 // 5
    step = (x1 - x0) / max(1, n - 1)
    occl_lo, occl_hi = n // 3, n // 3 * 2

    frames: list[np.ndarray] = []
    occluded: list[bool] = []
    for i in range(n):
        frame = np.full((h, w, 3), 60, np.uint8)
        x, y = int(x0 + step * i), h // 3
        frame[y:y + fh, x:x + face_w] = face
        occ = scenario == "occlude" and occl_lo <= i < occl_hi
        if occ:
            frame[y - 10:y + fh + 10, x - 10:x + face_w + 10] = (120, 120, 120)
        frames.append(frame)
        occluded.append(occ)

    meta = VideoMeta(w, h, float(fps), f"{fps}/1")
    with FFmpegWriter(out_path, meta, gop=25) as vw:   # 密集关键帧，供切片测试
        for f in frames:
            vw.write(f)
    return frames, occluded
