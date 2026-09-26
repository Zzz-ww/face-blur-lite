import numpy as np
from conftest import requires_assets

from faceblur_lite.ffmpeg_io import FFmpegReader, probe
from faceblur_lite.kernel import KernelParams, process_video

pytestmark = requires_assets


def test_process_video_blurs_and_keeps_duration(move_video, tmp_path):
    src, frames = move_video
    out = str(tmp_path / "out.mp4")
    rep = process_video(src, out, KernelParams(det_every=2))

    # 报告：60 帧全处理；隔帧检测 → 30 检测帧 + 30 预测帧
    assert rep.frames == 60
    assert rep.predicted_frames == 30
    assert rep.detections >= 30
    assert rep.track_count >= 1
    assert rep.segments and rep.segments[0][0] <= 0.2

    # 输出时长保持
    m = probe(out)
    assert abs(m.duration_s - 6.0) < 0.4

    # 模糊生效：人脸区域方差显著下降
    with FFmpegReader(out) as rd:
        got = list(rd)
    i = 30
    x0, x1 = 640 // 5, 640 * 4 // 5
    x = int(x0 + (x1 - x0) * i / 59)
    crop_in = frames[i][170:270, x + 10:x + 110]
    crop_out = got[i][170:270, x + 10:x + 110]
    assert crop_out.std() < crop_in.std() * 0.8


def test_blur_all_dets_blurs_low_conf_no_birth(move_video, tmp_path, monkeypatch):
    """BUG-013：源域 conf 0.3-0.5 的检测进低分池只救活不出生，曾导致
    长段裸奔。blur_all_dets=True 时检测框与 track 框取并集，当场糊掉。"""
    import faceblur_lite.kernel as K
    from faceblur_lite.detector import Det

    src, frames = move_video
    # 构造点走 make_detector 后（2026-09-26 注入点改造），mock 缝同步搬家
    monkeypatch.setattr(
        K, "make_detector",
        lambda kind, mp, **kw: type(
            "FakeDet", (), {"detect": staticmethod(
                lambda frame: [Det(280, 180, 380, 280, 0.35)])})())
    monkeypatch.setattr(K, "Tracker", K.Tracker)   # 真 tracker：0.35 永不出生

    out = str(tmp_path / "union.mp4")
    process_video(src, out, KernelParams(det_every=2, blur_all_dets=True))
    with FFmpegReader(out) as rd:
        got = list(rd)
    i = 30
    crop_in = frames[i][185:275, 285:375]
    crop_out = got[i][185:275, 285:375]
    assert crop_out.std() < crop_in.std() * 0.8      # 检测帧：并集糊上了

    out2 = str(tmp_path / "nounion.mp4")
    process_video(src, out2, KernelParams(det_every=2, blur_all_dets=False))
    with FFmpegReader(out2) as rd:
        got2 = list(rd)
    assert got2[i][185:275, 285:375].std() > crop_in.std() * 0.9   # 关掉则裸奔


def test_kernel_threads_detector_kind(tmp_path, move_video, monkeypatch):
    """注入点：KernelParams.detector 透传到 make_detector（mock 断言）。"""
    from faceblur_lite import kernel
    from faceblur_lite.kernel import KernelParams

    calls = []

    class FakeDet:
        def detect(self, frame):
            return []

    def fake_make(kind, mp, **kw):
        calls.append((kind, kw.get("conf_thr"), kw.get("upscale")))
        return FakeDet()

    monkeypatch.setattr(kernel, "make_detector", fake_make)
    kernel.process_video(
        move_video[0], str(tmp_path / "o.mp4"),
        params=KernelParams(detector="scrfd", det_every=2, det_conf=0.4,
                            det_upscale=1.5))
    assert calls == [("scrfd", 0.4, 1.5)]
