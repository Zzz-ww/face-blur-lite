import shutil

import numpy as np
import pytest

from faceblur_lite.ffmpeg_io import FFmpegReader, FFmpegWriter, VideoMeta, probe

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="需要系统 ffmpeg")


def test_writer_reader_roundtrip(tmp_path):
    meta = VideoMeta(64, 48, 10.0, "10/1")
    out = str(tmp_path / "rt.mp4")
    with FFmpegWriter(out, meta) as w:
        for i in range(5):
            w.write(np.full((48, 64, 3), ((i + 1) * 40) % 255, dtype=np.uint8))

    m = probe(out)
    assert (m.width, m.height) == (64, 48)
    assert abs(m.fps - 10.0) < 0.1
    assert abs(m.duration_s - 0.5) < 0.1
    assert not m.has_audio

    with FFmpegReader(out) as rd:
        got = list(rd)
    assert len(got) == 5
    assert got[0].shape == (48, 64, 3)
    # 纯色帧近似无损：编码误差远小于帧间差
    assert np.mean(np.abs(got[0].astype(int) - got[4].astype(int))) > 100
    assert np.mean(np.abs(got[0].astype(int) - 40)) < 6


def test_sample_fps_reader(tmp_path):
    meta = VideoMeta(64, 48, 10.0, "10/1")
    out = str(tmp_path / "s.mp4")
    with FFmpegWriter(out, meta) as w:
        for i in range(20):
            w.write(np.full((48, 64, 3), (i * 10) % 255, dtype=np.uint8))
    with FFmpegReader(out, sample_fps=1.0) as rd:
        got = list(rd)
    assert len(got) == 2   # 20 帧 @10fps = 2s，1fps 抽 2 帧


def test_sample_fps_stride_yields_midwindow_frames(tmp_path):
    """BUG-010：抽样必须是原始帧序列的逐帧成员、取窗口中点。
    旧实现走 -vf fps 滤镜，实测有 +14 帧恒定相位偏移（机制未明，像素级
    MSE 定谳），且抽出的帧不再与原始帧一一同源。"""
    meta = VideoMeta(64, 48, 10.0, "10/1")
    out = str(tmp_path / "stride.mp4")
    with FFmpegWriter(out, meta) as w:
        for i in range(20):
            w.write(np.full((48, 64, 3), (i * 10) % 255, dtype=np.uint8))
    with FFmpegReader(out, sample_fps=1.0) as rd:
        assert rd.sample_stride == 10
        got = list(rd)
    assert len(got) == 2
    # 窗口 [0..9] 取中点 5 → 灰度 50；[10..19] 取 15 → 灰度 150
    assert abs(float(got[0].mean()) - 50) < 6
    assert abs(float(got[1].mean()) - 150) < 6
