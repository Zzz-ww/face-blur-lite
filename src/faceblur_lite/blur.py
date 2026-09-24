"""人脸区域模糊（数据面算子库）。脱敏代价不对称：宁可多糊。"""
from __future__ import annotations

import cv2
import numpy as np


def expand_box(box: np.ndarray, frame_w: int, frame_h: int,
               margin: float = 0.15,
               margin_px: int = 0) -> tuple[int, int, int, int] | None:
    """框四边外扩 margin 比例（按宽高较大者），clip 到帧内，返回整数像素；
    框与画面无交（完全出画）返回 None（BUG-009：x1 越上界曾致空切片崩溃）。
    margin_px 为像素下限（BUG-012）：小脸的比例 margin 只有 2-4px，盖不住
    检测框抖动露出的脸缘，取 max(比例, 下限)。"""
    x1, y1, x2, y2 = (float(v) for v in box)
    m = max(margin * max(x2 - x1, y2 - y1), margin_px)
    x1 = max(0, int(round(x1 - m)))
    y1 = max(0, int(round(y1 - m)))
    x2 = min(frame_w, int(round(x2 + m)))
    y2 = min(frame_h, int(round(y2 + m)))
    if x1 >= x2 or y1 >= y2:
        return None
    return x1, y1, x2, y2


def blur_regions(frame: np.ndarray, boxes: list[np.ndarray],
                 margin: float = 0.15,
                 margin_px: int = 0) -> np.ndarray:
    """对每个 xyxy 框外扩后做高斯模糊，返回新帧；完全出画的框跳过。"""
    out = frame.copy()
    h, w = frame.shape[:2]
    for box in boxes:
        r = expand_box(box, w, h, margin, margin_px)
        if r is None:
            continue
        x1, y1, x2, y2 = r
        crop = out[y1:y2, x1:x2]
        # 奇数核；比例 0.75：σ 须与人脸低频结构尺度相当，否则 std 验收不降
        # （2026-09-24 执行期调参，原 0.15 只抹高频细节）
        k = max(3, int(min(crop.shape[:2]) * 0.75) // 2 * 2 + 1)
        out[y1:y2, x1:x2] = cv2.GaussianBlur(crop, (k, k), 0)
    return out
