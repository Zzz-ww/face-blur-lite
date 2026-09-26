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


def test_qc_t_sec_exact_frame_timestamps(move_video, tmp_path):
    """BUG-010：t_sec 必须是被抽中原始帧的真实时间戳。
    10fps 视频 1fps 抽样，步长 11（BUG-016 互素取整）→ 抽中帧
    5,16,27,38,49 → t ∈ {0.5,1.6,2.7,3.8,4.9}（旧实现 t=i/sample_fps=
    整数，标签整体错位半个窗口。）"""
    out = str(tmp_path / "leak_t.mp4")
    process_video(move_video[0], out,
                  KernelParams(det_every=2, det_conf=1.1))
    rep = qc_video(out)
    assert not rep.passed
    expected = {0.5, 1.6, 2.7, 3.8, 4.9}
    for v in rep.violations:
        assert any(abs(v.t_sec - e) < 0.01 for e in expected), v.t_sec


def test_qc_video_threads_detector(tmp_path, move_video, monkeypatch):
    """注入点：qc_video(detector=) 透传到 make_detector，conf 用 pass_conf。"""
    from faceblur_lite import qc as qcmod
    from faceblur_lite.qc import qc_video

    calls = []

    class FakeDet:
        def detect(self, frame):
            return []

    def fake_make(kind, mp, **kw):
        calls.append((kind, kw.get("conf_thr")))
        return FakeDet()

    monkeypatch.setattr(qcmod, "make_detector", fake_make)
    rep = qc_video(move_video[0], sample_fps=2.0, pass_conf=0.5,
                   detector="scrfd")
    assert calls == [("scrfd", 0.5)] and rep.passed
