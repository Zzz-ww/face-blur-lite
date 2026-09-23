from pathlib import Path

import cv2
import numpy as np
import pytest

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
