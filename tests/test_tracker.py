import numpy as np

from faceblur_lite.detector import Det
from faceblur_lite.tracker import Tracker


def d(x, y, s=40, conf=0.9):
    return Det(x, y, x + s, y + s, conf)


def test_static_target_predict_holds_position():
    tr = Tracker()
    tr.update([d(100, 100)], gap=3)
    [t] = tr.predict()
    assert abs(t.box[0] - 100) < 1


def test_constant_velocity_estimated():
    tr = Tracker()
    for k in range(6):                       # 30px/3帧 = 10px/帧
        tr.update([d(100 + 30 * k, 100)], gap=3)
        tr.predict(); tr.predict()           # 模拟 det_every=3 的中间帧
    assert 7 <= tr.tracks[0].vel[0] <= 11    # 速度收敛到 10px/帧
    before = tr.tracks[0].box[0]
    tr.predict()                             # 预测下一帧坐标
    assert 5 <= tr.tracks[0].box[0] - before <= 12


def test_occlusion_prediction_survival_then_prune():
    tr = Tracker(max_age=2)
    tr.update([d(100, 100)], gap=3)
    tr.update([d(130, 100)], gap=3)
    for _ in range(2):                       # 遮挡 2 个检测轮：预测外推维持
        tr.predict()
        assert len(tr.tracks) == 1
        tr.update([], gap=3)
    assert 130 <= tr.tracks[0].box[0] <= 260  # 按速度外推，在合理区间
    tr.predict()
    tr.update([], gap=3)                     # 第 3 轮无匹配 → 剪枝（ghost 防护）
    assert tr.tracks == []


def test_low_conf_second_pass_recovery():
    tr = Tracker()
    tr.update([d(100, 100)], gap=1)
    tr.update([d(112, 100, conf=0.35)], gap=1)   # 遮挡期低分框
    t = tr.tracks[0]
    assert t.hits == 2                        # 被二轮关联救活并校正
    assert abs(t.box[0] - 112) < 1


def test_unmatched_high_conf_births_new_track():
    tr = Tracker()
    tr.update([d(100, 100)], gap=1)
    tr.update([d(100, 100), d(400, 400)], gap=1)
    assert len(tr.tracks) == 2


def test_predict_prunes_fully_outside_tracks():
    """BUG-009：注入 frame_wh 时，完全出画的 track 在 predict 后剪掉
    （人脸已离画，无脱敏价值，segments 也不再虚假延长）。"""
    tr = Tracker(frame_wh=(640, 480))
    tr.update([d(100, 100)], gap=1)
    tr.tracks[0].box = np.array([700.0, 100.0, 740.0, 140.0])   # 完全出右画
    assert tr.predict() == []
    # 半出画保留（画内还有脸，继续糊画内部分）
    tr2 = Tracker(frame_wh=(640, 480))
    tr2.update([d(100, 100)], gap=1)
    tr2.tracks[0].box = np.array([620.0, 100.0, 700.0, 140.0])  # 半出画
    assert len(tr2.predict()) == 1
    # 未注入 frame_wh：保留纯运动学语义（旧调用方/单元测试不受影响）
    tr3 = Tracker()
    tr3.update([d(100, 100)], gap=1)
    tr3.tracks[0].box = np.array([700.0, 100.0, 740.0, 140.0])
    assert len(tr3.predict()) == 1
