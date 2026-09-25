import shutil

import numpy as np
import pytest

from faceblur_lite.ffmpeg_io import (FFmpegReader, FFmpegWriter, VideoMeta,
                                     probe, sample_stride_for)

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
    """BUG-010：抽样必须是原始帧序列的逐帧成员、时间戳可精确还原；
    BUG-016：步长取整到与 2..8 互素（10fps→11），首窗仍取中点。"""
    meta = VideoMeta(64, 48, 10.0, "10/1")
    out = str(tmp_path / "stride.mp4")
    with FFmpegWriter(out, meta) as w:
        for i in range(20):
            w.write(np.full((48, 64, 3), (i * 10) % 255, dtype=np.uint8))
    with FFmpegReader(out, sample_fps=1.0) as rd:
        assert rd.sample_stride == 11
        got = list(rd)
    assert len(got) == 2
    # 抽中帧 5（灰度 50）与 16（灰度 160）：k*stride+offset 逐帧同源
    assert abs(float(got[0].mean()) - 50) < 6
    assert abs(float(got[1].mean()) - 160) < 6


def test_sample_stride_for_coprime_values():
    """BUG-016：步长取整到与 2..8 互素的最近值。"""
    assert sample_stride_for(30.0, 1.0) == 31
    assert sample_stride_for(60.0, 2.0) == 31       # round(30) 同样锁相档
    assert sample_stride_for(25.0, 1.0) == 29
    assert sample_stride_for(24.0, 1.0) == 29
    assert sample_stride_for(10.0, 1.0) == 11
    assert sample_stride_for(10.0, 30.0) == 1       # 欠采样 → 全密度


def test_sampling_covers_all_det_phase_classes():
    """BUG-016 性质测试：对 det_every∈2..8，连续 8 窗内抽中帧号 mod d
    必须覆盖全部残差类（旧行为 30fps@1fps 恒 ≡0 mod 3=只抽检测帧）。"""
    for fps in (30.0, 29.97, 25.0, 24.0, 60.0, 50.0, 23.976):
        m = sample_stride_for(fps, 1.0)
        idx = [k * m + m // 2 for k in range(8)]
        for d in range(2, 9):
            assert {i % d for i in idx} == set(range(d)), (fps, m, d)


def test_sample_fps_30fps_reader_hits_all_phase_classes(tmp_path):
    """BUG-016 E2E：30fps 视频例行抽样的抽中帧号 mod 3 覆盖 {0,1,2}。
    帧号由 k*stride+offset 还原（qc.py 同式），并以灰度值锚定首帧。"""
    meta = VideoMeta(64, 48, 30.0, "30/1")
    out = str(tmp_path / "p30.mp4")
    with FFmpegWriter(out, meta) as w:
        for i in range(180):        # 6s
            w.write(np.full((48, 64, 3), (i * 4) % 255, dtype=np.uint8))
    with FFmpegReader(out, sample_fps=1.0) as rd:
        stride, offset = rd.sample_stride, rd.sample_offset
        got = list(rd)
    assert (stride, offset) == (31, 15)
    idx = [k * stride + offset for k in range(len(got))]
    assert len(got) >= 5
    assert {i % 3 for i in idx} == {0, 1, 2}
    assert abs(float(got[0].mean()) - 60) < 6      # 帧 15 灰度 60
