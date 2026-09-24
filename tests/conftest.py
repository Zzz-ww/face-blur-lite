import shutil
import socket
import subprocess
import time
import urllib.request
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


@pytest.fixture(scope="session")
def go_bin(tmp_path_factory):
    """构建 gocp 二进制（session 只构建一次）；无 go 工具链则跳过。"""
    if shutil.which("go") is None:
        pytest.skip("需要 go >= 1.25 工具链（brew install go）")
    root = Path(__file__).resolve().parents[1]   # tests/ 上一级=项目根（同 Task 3 修正）
    bin_path = tmp_path_factory.mktemp("gobuild") / "gocp"
    subprocess.run(["go", "build", "-o", str(bin_path), "."],
                   cwd=root / "gocp", check=True)
    return bin_path


@pytest.fixture()
def go_cp_url(go_bin, tmp_path):
    """每个测试一个全新 gocp 服务 + 空库（测试间零干扰）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [str(go_bin), str(tmp_path / "go.db"), f"127.0.0.1:{port}"])
    try:
        for _ in range(50):            # 等服务就绪
            try:
                urllib.request.urlopen(f"{base}/videos", timeout=0.2).read()
                break
            except Exception:
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait()
