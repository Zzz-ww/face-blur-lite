"""人脸区域模糊（数据面算子库）。脱敏代价不对称：宁可多糊。"""
from __future__ import annotations

import cv2
import numpy as np


def expand_box(box: np.ndarray, frame_w: int, frame_h: int,
               margin: float = 0.15) -> tuple[int, int, int, int]:
    """框四边外扩 margin 比例（按宽高较大者），clip 到帧内，返回整数像素。"""
    x1, y1, x2, y2 = (float(v) for v in box)
    m = margin * max(x2 - x1, y2 - y1)
    x1 = max(0, int(round(x1 - m)))
    y1 = max(0, int(round(y1 - m)))
    x2 = min(frame_w, int(round(x2 + m)))
    y2 = min(frame_h, int(round(y2 + m)))
    return x1, y1, max(x2, x1 + 1), max(y2, y1 + 1)


def blur_regions(frame: np.ndarray, boxes: list[np.ndarray],
                 margin: float = 0.15) -> np.ndarray:
    """对每个 xyxy 框外扩后做高斯模糊，返回新帧。"""
    out = frame.copy()
    h, w = frame.shape[:2]
    for box in boxes:
        x1, y1, x2, y2 = expand_box(box, w, h, margin)
        crop = out[y1:y2, x1:x2]
        k = max(3, int(min(crop.shape[:2]) * 0.15) // 2 * 2 + 1)  # 奇数核
        out[y1:y2, x1:x2] = cv2.GaussianBlur(crop, (k, k), 0)
    return out
