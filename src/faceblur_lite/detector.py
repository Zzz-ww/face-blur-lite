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
