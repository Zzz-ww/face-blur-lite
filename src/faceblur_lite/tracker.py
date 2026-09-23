"""mini-SORT 跟踪器（数据面核心，R2 遮挡 + R6 下一帧坐标预测）。

- 恒速运动模型：predict() 把每个 track 按速度外推一帧（= 预测下一帧坐标）
- update()：高分框匈牙利匹配校正；未匹配 track 用低分框二轮救活
  （ByteTrack 思路——遮挡期置信度塌落时不断链）；max_age 个检测轮无匹配才
  淘汰（ghost 剪枝：预测框不能无限存活）
- 速度估计按"距上次检测的帧数 gap"归一，det_every>1 时依然正确
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from faceblur_lite.detector import Det


@dataclass
class Track:
    id: int
    box: np.ndarray                  # xyxy，当前（可能为预测的）位置
    last_box: np.ndarray             # 上一次被检测框校正的位置
    vel: np.ndarray = field(default_factory=lambda: np.zeros(4))  # px/帧
    hits: int = 0
    misses: int = 0                  # 连续未匹配的检测轮数


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a:(n,4) b:(m,4) xyxy → (n,m) IoU。"""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    ax1, ay1, ax2, ay2 = (a[:, i:i + 1] for i in range(4))
    bx1, by1, bx2, by2 = (b[None, :, i] for i in range(4))
    inter = (np.maximum(0, np.minimum(ax2, bx2) - np.maximum(ax1, bx1))
             * np.maximum(0, np.minimum(ay2, by2) - np.maximum(ay1, by1)))
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / (ua + 1e-9)


def associate(tracks_boxes: list, det_boxes: list,
              thr: float) -> tuple[list, list, list]:
    """返回 (匹配对, 未匹配 track 下标, 未匹配 det 下标)。"""
    if not tracks_boxes or not det_boxes:
        return [], list(range(len(tracks_boxes))), list(range(len(det_boxes)))
    m = iou_matrix(np.stack(tracks_boxes), np.stack(det_boxes))
    rows, cols = linear_sum_assignment(1.0 - m)
    pairs = [(int(r), int(c)) for r, c in zip(rows, cols) if m[r, c] >= thr]
    ta = {a for a, _ in pairs}
    db = {b for _, b in pairs}
    return (pairs,
            [i for i in range(len(tracks_boxes)) if i not in ta],
            [j for j in range(len(det_boxes)) if j not in db])


class Tracker:
    def __init__(self, high_thr: float = 0.5, low_thr: float = 0.3,
                 iou_thr: float = 0.1, max_age: int = 3, alpha: float = 0.6):
        self.high_thr = high_thr
        self.low_thr = low_thr
        self.iou_thr = iou_thr
        self.max_age = max_age
        self.alpha = alpha
        self.tracks: list[Track] = []
        self._next_id = 1

    @property
    def total_tracks(self) -> int:
        """累计出生过的 track 数（≈出现人脸次数；遮挡断链重检出会 +1）。"""
        return self._next_id - 1

    # ---- R6：预测下一帧坐标 ----
    def predict(self) -> list[Track]:
        for t in self.tracks:
            t.box = t.box + t.vel
        return self.tracks

    def update(self, dets: list[Det], gap: int = 1) -> list[Track]:
        hi = [d for d in dets if d.conf >= self.high_thr]
        lo = [d for d in dets if self.low_thr <= d.conf < self.high_thr]

        tboxes = [t.box for t in self.tracks]
        pool = hi + lo                      # 全局 det 下标：前 len(hi) 为高分
        p1, um_t, um_hi = associate(
            tboxes, [d.xyxy() for d in hi], self.iou_thr)
        pairs = p1
        if lo and um_t:                     # 二轮：低分框只救活，不出生
            p2, um_sub, _ = associate(
                [tboxes[i] for i in um_t],
                [d.xyxy() for d in lo], self.iou_thr)
            pairs += [(um_t[a], len(hi) + b) for a, b in p2]
            um_t = [um_t[i] for i in um_sub]

        for ti, dj in pairs:
            t, d = self.tracks[ti], pool[dj]
            inst_v = (d.xyxy() - t.last_box) / max(1, gap)
            t.vel = (inst_v if t.hits == 0
                     else self.alpha * t.vel + (1 - self.alpha) * inst_v)
            t.box = t.last_box = d.xyxy()
            t.hits += 1
            t.misses = 0

        for ti in um_t:
            self.tracks[ti].misses += 1
        self.tracks = [t for t in self.tracks if t.misses <= self.max_age]

        for j in um_hi:                     # 只有高分未匹配框允许出生
            self.tracks.append(              # hits=1：出生检测计入 hit（SORT 语义）
                Track(self._next_id, hi[j].xyxy(), hi[j].xyxy(), hits=1))
            self._next_id += 1
        return self.tracks
