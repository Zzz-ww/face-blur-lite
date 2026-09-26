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


def resolve_model_path(kind: str = "yunet") -> str:
    """模型路径分家（spec 2026-09-26 §2）：yunet→FACEBLUR_MODEL，
    scrfd→FACEBLUR_SCRFD_MODEL；各自回落 models/ 下默认件。"""
    if kind == "yunet":
        env = os.environ.get("FACEBLUR_MODEL")
        default = DEFAULT_MODEL
    elif kind == "scrfd":
        env = os.environ.get("FACEBLUR_SCRFD_MODEL")
        default = PROJECT_ROOT / "models" / "det_500m.onnx"
    else:
        raise ValueError(f"未知检测器 kind：{kind}（yunet|scrfd）")
    if env:
        return env
    if default.exists():
        return str(default)
    raise FileNotFoundError(
        f"未找到 {kind} 模型 {default}；先运行 scripts/download_assets.sh"
        f" 或设置 "
        f"{'FACEBLUR_SCRFD_MODEL' if kind == 'scrfd' else 'FACEBLUR_MODEL'}")


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
    # NMSBoxes 用 strict > 过滤（numpy 门是 >=）——全恰等时返回空，勿让 float64 空数组走到索引
    if keep_idx.size == 0:
        return []
    order = keep_idx[np.argsort(scs[keep_idx])[::-1]][:top_k]
    out = []
    for j in order:
        x1, y1, x2, y2 = boxes[j]
        out.append(Det((x1 - pad_left) / scale, (y1 - pad_top) / scale,
                       (x2 - pad_left) / scale, (y2 - pad_top) / scale,
                       float(scs[j])))
    return out


class SCRFDDetector:
    """insightface buffalo_s det_500m（SCRFD，2.5MB——spike 见 docs/bugs/014
    附录）。letterbox 640：长边缩到 640、pad 到 640×640（720×1406 → 内容带
    328×640）。预处理常数 (x-127.5)/128 与输出张量序均为 spike 验证值。
    无 upscale——SCRFD 小脸能力即升级目的，该旋钮 YuNet 专属。"""

    STRIDES = (8, 16, 32)
    INPUT_SIZE = 640

    def __init__(self, model_path: str | None = None, conf_thr: float = 0.3,
                 top_k: int = 50):
        path = model_path or resolve_model_path("scrfd")
        self.conf_thr, self.top_k = conf_thr, top_k
        self._net = cv2.dnn.readNetFromONNX(str(path))

    def detect(self, frame_bgr: np.ndarray) -> list[Det]:
        h, w = frame_bgr.shape[:2]
        scale, nh, nw, pt, pl = letterbox_shape(h, w, self.INPUT_SIZE)
        canvas = np.full((self.INPUT_SIZE, self.INPUT_SIZE, 3), 114,
                         np.uint8)
        canvas[pt:pt + nh, pl:pl + nw] = cv2.resize(
            frame_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(
            canvas, 1.0 / 128.0, (self.INPUT_SIZE, self.INPUT_SIZE),
            (127.5, 127.5, 127.5), swapRB=False)
        self._net.setInput(blob)
        outs = self._net.forward(self._net.getUnconnectedOutLayersNames())
        return _decode_scrfd(outs, self.conf_thr, self.top_k,
                             scale, pt, pl)


def make_detector(kind: str, model_path: str | None = None, *,
                  conf_thr: float = 0.3, top_k: int = 50,
                  upscale: float = 1.0) -> Detector:
    """检测器工厂（spec 2026-09-26 §2）。scrfd 忽略 upscale（其小脸能力
    即升级目的，放大旋钮 YuNet 专属）；未知 kind 抛 ValueError。"""
    if kind == "yunet":
        return YuNetDetector(model_path, conf_thr=conf_thr, top_k=top_k,
                             upscale=upscale)
    if kind == "scrfd":
        return SCRFDDetector(model_path, conf_thr=conf_thr, top_k=top_k)
    raise ValueError(f"未知检测器 kind：{kind}（yunet|scrfd）")
