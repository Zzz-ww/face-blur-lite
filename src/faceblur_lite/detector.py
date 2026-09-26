"""人脸检测器抽象 + YuNet 实现（数据面）。R5：换 SCRFD 只加实现类。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = PROJECT_ROOT / "models" / "face_detection_yunet_2023mar.onnx"


def resolve_model_path() -> str:
    env = os.environ.get("FACEBLUR_MODEL")
    if env:
        return env
    if DEFAULT_MODEL.exists():
        return str(DEFAULT_MODEL)
    raise FileNotFoundError(
        f"未找到模型 {DEFAULT_MODEL}；先运行 scripts/download_assets.sh"
        " 或设置 FACEBLUR_MODEL")


@dataclass(frozen=True)
class Det:
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float

    def xyxy(self) -> np.ndarray:
        return np.array([self.x1, self.y1, self.x2, self.y2])


class Detector(Protocol):
    def detect(self, frame_bgr: np.ndarray) -> list[Det]: ...


class YuNetDetector:
    """opencv_zoo YuNet（实测约 230KB）。预处理（缩放/归一化）在检测器内部完成，
    输入原始 BGR uint8 帧；setInputSize 按帧分辨率设置（R4）。
    upscale>1 时内部上采样检测、框坐标映射回原图（BUG-014 检测放大：
    救 40-50px 档小脸；默认 1.0 关——推理耗时约随线性倍率平方增长）。"""

    def __init__(self, model_path: str | None = None, conf_thr: float = 0.3,
                 top_k: int = 50, upscale: float = 1.0):
        path = model_path or resolve_model_path()
        self.upscale = upscale
        self._det = cv2.FaceDetectorYN.create(
            str(path), "", (320, 320), score_threshold=conf_thr, top_k=top_k)

    def detect(self, frame_bgr: np.ndarray) -> list[Det]:
        img = frame_bgr
        if self.upscale != 1.0:
            img = cv2.resize(frame_bgr, None, fx=self.upscale,
                             fy=self.upscale, interpolation=cv2.INTER_LINEAR)
        h, w = img.shape[:2]
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(img)
        if faces is None:
            return []
        s = 1.0 / self.upscale
        out = []
        for f in faces:
            x, y, bw, bh = (float(f[0]) * s, float(f[1]) * s,
                            float(f[2]) * s, float(f[3]) * s)
            out.append(Det(x, y, x + bw, y + bh, float(f[-1])))
        return out


def letterbox_shape(h: int, w: int, size: int = 640
                    ) -> tuple[float, int, int, int, int]:
    """letterbox 几何（SCRFD，spec 2026-09-26 §1）：长边缩到 size、
    短边居中 pad。返回 (scale, new_h, new_w, pad_top, pad_left)。"""
    scale = size / max(h, w)
    nh = max(1, int(round(h * scale)))
    nw = max(1, int(round(w * scale)))
    return scale, nh, nw, (size - nh) // 2, (size - nw) // 2


def _decode_scrfd(outputs, conf_thr: float, top_k: int,
                  scale: float, pad_top: int, pad_left: int) -> list[Det]:
    """SCRFD buffalo 系 9 张量解码（spike 踩明两坑，docs/bugs/014 附录）：
    输出序 [score×3, dist-bbox×3, kps×3] × strides(8,16,32)，2 锚/cell；
    kps 丢弃（Det 不带关键点）。坐标逆映射回原图：(canvas - pad)/scale。"""
    strides = (8, 16, 32)
    scores = [np.asarray(o).reshape(-1) for o in outputs[0:3]]
    dists = [np.asarray(o).reshape(-1, 4) for o in outputs[3:6]]
    boxes_all, scores_all = [], []
    for i, s in enumerate(strides):
        n = scores[i].shape[0]
        feat = int(round((n / 2) ** 0.5))
        ys, xs = np.meshgrid(np.arange(feat), np.arange(feat),
                             indexing="ij")
        centers = np.stack([(xs + 0.5) * s, (ys + 0.5) * s],
                           axis=-1).reshape(-1, 2)
        centers = np.repeat(centers, 2, axis=0)   # 坑 2：逐位置交错
        keep = scores[i] >= conf_thr
        if not keep.any():
            continue
        d = dists[i][keep] * s                    # 坑 1：dist × stride
        c = centers[keep]
        boxes_all.append(np.stack(
            [c[:, 0] - d[:, 0], c[:, 1] - d[:, 1],
             c[:, 0] + d[:, 2], c[:, 1] + d[:, 3]], axis=1))
        scores_all.append(scores[i][keep])
    if not boxes_all:
        return []
    boxes = np.concatenate(boxes_all)
    scs = np.concatenate(scores_all)
    xywh = np.stack([boxes[:, 0], boxes[:, 1],
                     boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]],
                    axis=1)
    keep_idx = np.array(cv2.dnn.NMSBoxes(
        xywh.tolist(), scs.tolist(), conf_thr, 0.4)).reshape(-1)
    order = keep_idx[np.argsort(scs[keep_idx])[::-1]][:top_k]
    out = []
    for j in order:
        x1, y1, x2, y2 = boxes[j]
        out.append(Det((x1 - pad_left) / scale, (y1 - pad_top) / scale,
                       (x2 - pad_left) / scale, (y2 - pad_top) / scale,
                       float(scs[j])))
    return out
