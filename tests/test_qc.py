from conftest import requires_assets

from faceblur_lite.kernel import KernelParams, process_video
from faceblur_lite.qc import qc_video

pytestmark = requires_assets


def test_qc_passes_fully_blurred(move_video, tmp_path):
    out = str(tmp_path / "ok.mp4")
    process_video(move_video[0], out, KernelParams(det_every=2))
    rep = qc_video(out)
    assert rep.sampled_frames >= 5
    assert rep.passed, rep.violations


def test_qc_catches_unblurred(move_video, tmp_path):
    out = str(tmp_path / "leak.mp4")
    # det_conf>1 强制零检测 → 输出完全未脱敏 → qc 必须兜住
    process_video(move_video[0], out,
                  KernelParams(det_every=2, det_conf=1.1))
    rep = qc_video(out)
    assert not rep.passed
    assert rep.violations and rep.violations[0].conf >= 0.5
