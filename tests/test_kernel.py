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
