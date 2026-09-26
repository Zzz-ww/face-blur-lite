from pathlib import Path

import cv2
import numpy as np
import pytest

from conftest import ROOT, requires_scrfd
from faceblur_lite.detector import YuNetDetector

# 注：tests/ 距项目根一层，故 parents[1]（brief 原文 parents[2] 指到仓库上一级，
# models/assets 不在那里，skipif 会误跳过）
ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
LENA = ROOT / "assets" / "lena.jpg"

pytestmark = pytest.mark.skipif(
    not MODEL.exists() or not LENA.exists(),
    reason="先运行 scripts/download_assets.sh")


def test_detects_real_face():
    img = cv2.imread(str(LENA))
    dets = YuNetDetector(str(MODEL), conf_thr=0.6).detect(img)
    assert len(dets) >= 1
    d = dets[0]
    assert 0 < d.x2 - d.x1 < img.shape[1]
    assert 0 < d.y2 - d.y1 < img.shape[0]
    assert 0.6 <= d.conf <= 1.0


def test_no_face_on_blank():
    blank = np.full((480, 640, 3), 128, dtype=np.uint8)
    assert YuNetDetector(str(MODEL), conf_thr=0.6).detect(blank) == []


def iou(a, b):
    ix = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = ((a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1)
          - inter)
    return inter / ua


def test_upscale_maps_box_to_original_coords():
    """检测放大（BUG-014 能力位）：内部上采样检测，框坐标必须映射回
    原图坐标系——与 1x 检出同一张脸、位置一致。"""
    img = cv2.imread(str(LENA))
    d1 = YuNetDetector(str(MODEL), conf_thr=0.6).detect(img)
    d2 = YuNetDetector(str(MODEL), conf_thr=0.6, upscale=2.0).detect(img)
    assert d1 and d2
    assert iou(d1[0], d2[0]) > 0.5
    assert 0 < d2[0].x2 - d2[0].x1 < img.shape[1]   # 原图坐标尺度


def test_upscale_rescues_small_face():
    """48px 小脸 1x 检不出、2x 能检出（检测放大的存在意义；16-32px
    仍超出能力包络——见 docs/bugs/014 实测）。"""
    img = cv2.imread(str(LENA))
    scale = 48 / img.shape[1]
    small = cv2.resize(img, None, fx=scale, fy=scale)
    h, w = small.shape[:2]
    canvas = np.full((480, 640, 3), 120, dtype=np.uint8)
    canvas[200:200 + h, 300:300 + w] = small
    assert YuNetDetector(str(MODEL), conf_thr=0.3).detect(canvas) == []
    dets = YuNetDetector(str(MODEL), conf_thr=0.3, upscale=2.0).detect(canvas)
    hits = [d for d in dets if 290 < d.x1 and d.x2 < 360]
    assert hits and hits[0].conf >= 0.3


@requires_scrfd
def test_make_detector_dispatch_and_unknown_kind():
    """工厂分派：yunet→YuNetDetector、scrfd→SCRFDDetector；未知 kind 抛错。"""
    from faceblur_lite.detector import (SCRFDDetector, YuNetDetector,
                                        make_detector)
    assert isinstance(make_detector("yunet", conf_thr=0.3), YuNetDetector)
    assert isinstance(make_detector("scrfd", conf_thr=0.3), SCRFDDetector)
    with pytest.raises(ValueError, match="未知检测器"):
        make_detector("retina", conf_thr=0.3)


@requires_scrfd
def test_make_detector_scrfd_ignores_upscale():
    """scrfd 无 upscale 旋钮（小脸能力即升级目的）——传了也不炸不生效。"""
    from faceblur_lite.detector import SCRFDDetector, make_detector
    det = make_detector("scrfd", conf_thr=0.3, upscale=2.0)
    assert isinstance(det, SCRFDDetector)
    assert not hasattr(det, "upscale") or getattr(det, "upscale", 1.0) == 1.0


def test_resolve_model_path_per_kind(monkeypatch):
    """模型解析分家：FACEBLUR_SCRFD_MODEL 只影响 scrfd。"""
    from faceblur_lite import detector as D
    monkeypatch.setenv("FACEBLUR_SCRFD_MODEL", "/tmp/fake-scrfd.onnx")
    assert D.resolve_model_path("scrfd") == "/tmp/fake-scrfd.onnx"
    monkeypatch.setenv("FACEBLUR_MODEL", "/tmp/fake-yunet.onnx")
    assert D.resolve_model_path("yunet") == "/tmp/fake-yunet.onnx"
    monkeypatch.delenv("FACEBLUR_SCRFD_MODEL")
    monkeypatch.delenv("FACEBLUR_MODEL")
    with pytest.raises(ValueError, match="未知检测器"):
        D.resolve_model_path("mega")


@requires_scrfd
def test_scrfd_detector_finds_lena_face():
    """spike 金丝雀：det_500m 在 lena 出正确位置框（0.811，014 附录）。
    与 YuNet 框 IoU>0.5——同时锁输出张量序没接错。"""
    import cv2
    from faceblur_lite.detector import SCRFDDetector, YuNetDetector
    frame = cv2.imread(str(ROOT / "assets" / "lena.jpg"))
    s = SCRFDDetector(conf_thr=0.3).detect(frame)
    y = YuNetDetector(conf_thr=0.3).detect(frame)
    assert s and y
    sx1, sy1, sx2, sy2 = s[0].xyxy()
    yx1, yy1, yx2, yy2 = y[0].xyxy()
    ix1, iy1 = max(sx1, yx1), max(sy1, yy1)
    ix2, iy2 = min(sx2, yx2), min(sy2, yy2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = (sx2 - sx1) * (sy2 - sy1) + (yx2 - yx1) * (yy2 - yy1) - inter
    assert inter / union > 0.5
