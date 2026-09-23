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
    """opencv_zoo YuNet（约 1MB）。预处理（缩放/归一化）在检测器内部完成，
    输入原始 BGR uint8 帧；setInputSize 按帧分辨率设置（R4）。"""

    def __init__(self, model_path: str | None = None, conf_thr: float = 0.3,
                 top_k: int = 50):
        path = model_path or resolve_model_path()
        self._det = cv2.FaceDetectorYN.create(
            str(path), "", (320, 320), score_threshold=conf_thr, top_k=top_k)

    def detect(self, frame_bgr: np.ndarray) -> list[Det]:
        h, w = frame_bgr.shape[:2]
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(frame_bgr)
        if faces is None:
            return []
        out = []
        for f in faces:
            x, y, bw, bh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
            out.append(Det(x, y, x + bw, y + bh, float(f[-1])))
        return out
