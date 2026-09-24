from conftest import requires_assets, face_img

from faceblur_lite.kernel import KernelParams, process_video
from faceblur_lite.testmedia import build_video

pytestmark = requires_assets


def test_occlusion_scenario(tmp_path, face_img):
    src = str(tmp_path / "occ.mp4")
    build_video(src, face_img, scenario="occlude", duration_s=6.0)
    out = str(tmp_path / "occ_out.mp4")
    rep = process_video(src, out, KernelParams(det_every=2))

    # 开局即检出；遮挡中段跟踪可能断链（segments 可能 1~3 段）；
    # 断链后重检出会增加 track 数——对"糊脸"而言 ID 切换不是错误，
    # 每一帧的脸都被糊才是安全底线
    assert rep.segments[0][0] <= 0.2
    assert 1 <= len(rep.segments) <= 3
    assert rep.track_count >= 1
    assert abs(rep.duration_s - 6.0) < 0.4
