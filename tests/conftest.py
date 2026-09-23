import shutil
from pathlib import Path

import pytest

from faceblur_lite.testmedia import build_video, load_face

ROOT = Path(__file__).resolve().parents[1]   # tests/ 上一级=项目根（同 Task 3 修正）


def assets_ready() -> bool:
    return (shutil.which("ffmpeg") is not None
            and (ROOT / "assets" / "lena.jpg").exists()
            and (ROOT / "models" / "face_detection_yunet_2023mar.onnx").exists())


requires_assets = pytest.mark.skipif(
    not assets_ready(), reason="需要 ffmpeg 与下载资产（scripts/download_assets.sh）")


@pytest.fixture(scope="session")
def face_img():
    return load_face(str(ROOT / "assets" / "lena.jpg"))


@pytest.fixture(scope="session")
def move_video(tmp_path_factory, face_img):
    """6s@10fps，人脸匀速从左到右。返回 (视频路径, 帧列表)。"""
    path = str(tmp_path_factory.mktemp("media") / "move.mp4")
    frames, _ = build_video(path, face_img, duration_s=6.0)
    return path, frames
