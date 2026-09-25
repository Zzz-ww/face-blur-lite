import time

import cv2
import numpy as np

from faceblur_lite.blur import blur_regions, expand_box


def test_blur_changes_only_region():
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    box = np.array([200.0, 200.0, 300.0, 300.0])
    out = blur_regions(frame, [box], margin=0.0)
    # 框内被模糊（变化）
    assert not np.array_equal(out[210:290, 210:290], frame[210:290, 210:290])
    # 框外原样
    assert np.array_equal(out[:100, :100], frame[:100, :100])
    assert np.array_equal(out[400:, 400:], frame[400:, 400:])


def test_blur_empty_boxes_noop():
    frame = np.zeros((48, 64, 3), np.uint8)
    out = blur_regions(frame, [])
    assert np.array_equal(out, frame)


def test_expand_clips_to_frame():
    x1, y1, x2, y2 = expand_box(np.array([5.0, 5.0, 60.0, 60.0]), 64, 48, 0.5)
    assert x1 == 0 and y1 == 0
    assert 0 < x2 <= 64 and 0 < y2 <= 48


def test_expand_fully_outside_returns_none():
    """BUG-009：完全出画的框与画面无交 → None（此前 x1 越上界 → numpy
    越界切片静默空数组 → GaussianBlur 断言崩溃；出左画则糊出 1px 边缘条带）。"""
    assert expand_box(np.array([80.0, 10.0, 100.0, 30.0]), 64, 48) is None   # 右
    assert expand_box(np.array([-30.0, 10.0, -10.0, 30.0]), 64, 48) is None  # 左
    assert expand_box(np.array([10.0, 60.0, 30.0, 80.0]), 64, 48) is None    # 下
    assert expand_box(np.array([10.0, -30.0, 30.0, -10.0]), 64, 48) is None  # 上


def test_blur_fully_outside_box_noop():
    """完全出画的框：跳过不崩、整帧原样。"""
    rng = np.random.default_rng(2)
    frame = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    out = blur_regions(frame, [np.array([700.0, 200.0, 800.0, 300.0])], margin=0.0)
    assert np.array_equal(out, frame)


def test_blur_partial_outside_clamps():
    """半出画的框：只糊画内部分，画外区域与框外原样。"""
    rng = np.random.default_rng(3)
    frame = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    out = blur_regions(frame, [np.array([600.0, 200.0, 700.0, 300.0])], margin=0.0)
    assert not np.array_equal(out[210:290, 610:640], frame[210:290, 610:640])
    assert np.array_equal(out[:, :590], frame[:, :590])


def test_margin_px_floor_governs_small_boxes():
    """BUG-012：小脸的 margin 下限按像素计。比例 margin（0.15×12px≈2px）
    盖不住检测框抖动露出的脸缘；大框仍由比例主导。"""
    # 12px 小框：0.15×12≈2px，下限 8px 生效 → 糊区 [0, 24]
    x1, y1, x2, y2 = expand_box(np.array([100.0, 100.0, 112.0, 112.0]),
                                640, 480, 0.15, margin_px=8)
    assert (x1, y1) == (92, 92) and (x2, y2) == (120, 120)
    # 100px 大框：0.15×100=15px > 8px，比例主导
    x1, y1, x2, y2 = expand_box(np.array([100.0, 100.0, 200.0, 200.0]),
                                640, 480, 0.15, margin_px=8)
    assert (x1, y1) == (85, 85) and (x2, y2) == (215, 215)
    # 默认 0：旧语义不变
    x1, y1, x2, y2 = expand_box(np.array([100.0, 100.0, 112.0, 112.0]), 640, 480, 0.15)
    assert (x1, y1) == (98, 98) and (x2, y2) == (114, 114)


def test_margin_px_floor_blurs_wider_than_box():
    """下限生效时，框外 8px 环带必须被糊到（半糊治本）。"""
    rng = np.random.default_rng(4)
    frame = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    out = blur_regions(frame, [np.array([100.0, 100.0, 112.0, 112.0])],
                       margin=0.15, margin_px=8)
    assert not np.array_equal(out[92:120, 92:120], frame[92:120, 92:120])
    assert np.array_equal(out[:91, :], frame[:91, :])        # 糊区外原样


# ---------- BUG-015：大脸大核的降采样快路径 ----------

def _structured(h, w, seed=7):
    """结构化测试图（低频图样 + 噪声）：std 明显、糊后可量化对比。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 128 + 60 * np.sin(yy / 18.0) + 40 * np.cos(xx / 11.0)
    img = np.clip(base + rng.integers(-25, 26, (h, w)), 0, 255).astype(np.uint8)
    return np.repeat(img[:, :, None], 3, axis=2)


def _k_of(short):
    return max(3, int(short * 0.75) // 2 * 2 + 1)


def test_big_box_fast_path_matches_direct_kernel():
    """BUG-015：大框（k>201）降采样快路径与直接大核 σ 视觉等效——
    均值守恒、结构摧毁程度相当（std 不显著高于直接核）。"""
    frame = _structured(700, 760)
    box = np.array([20.0, 20.0, 660.0, 700.0])            # crop 640×640 → k=481
    fast = blur_regions(frame, [box], margin=0.0)[20:700, 20:660].astype(np.float64)
    direct = cv2.GaussianBlur(frame[20:700, 20:660],
                              (_k_of(640),) * 2, 0).astype(np.float64)
    src = frame[20:700, 20:660].astype(np.float64)
    assert abs(fast.mean() - direct.mean()) < 1.5          # 均值近似守恒
    assert fast.std() < 0.06 * src.std()   # 摧毁 ≥94%（直接核实测 5.1%，边界反射带贡献）
    assert fast.std() < direct.std() * 1.1 + 1.0           # 不比直接核少糊


def test_big_box_fast_path_is_faster():
    """快路径成本 O(像素)：640² 大框耗时须 < 直接 k=481 核的一半。"""
    frame = _structured(700, 760, seed=5)
    box = np.array([20.0, 20.0, 660.0, 700.0])
    t0 = time.perf_counter()
    blur_regions(frame, [box], margin=0.0)
    t_fast = time.perf_counter() - t0
    t0 = time.perf_counter()
    cv2.GaussianBlur(frame[20:700, 20:660], (_k_of(640),) * 2, 0)
    t_direct = time.perf_counter() - t0
    assert t_fast < t_direct / 2


def test_small_and_boundary_boxes_stay_direct_path():
    """短边 ≤269（k≤201）不走快路径：与手工直接核逐位一致（既有行为锚，
    real_video 小脸 / demo 中脸场景零行为变化）。"""
    frame = _structured(300, 320, seed=11)
    out = blur_regions(frame, [np.array([40.0, 30.0, 280.0, 270.0])], margin=0.0)
    manual = frame.copy()
    manual[30:270, 40:280] = cv2.GaussianBlur(frame[30:270, 40:280],
                                              (_k_of(240),) * 2, 0)
    assert np.array_equal(out, manual)


def test_boundary_269_k_equals_threshold_stays_direct():
    """短边 269（k=201 恰等于 _MAX_DIRECT_K）：判据是 <=，等值行走直接核，
    与手工核逐位一致——和 270 形成真正的边界括号对（240/270 只测了严格
    两侧，把 <= 改成 < 不会红）。"""
    frame = _structured(340, 320, seed=19)
    out = blur_regions(frame, [np.array([30.0, 20.0, 299.0, 310.0])], margin=0.0)
    manual = frame.copy()
    manual[20:310, 30:299] = cv2.GaussianBlur(frame[20:310, 30:299],
                                              (_k_of(269),) * 2, 0)
    assert np.array_equal(out, manual)


def test_boundary_270_engages_fast_path():
    """短边 270（k=203>201）切快路径：与直接核不再逐位相同，但仍糊且框外不动。"""
    frame = _structured(320, 320, seed=13)
    out = blur_regions(frame, [np.array([25.0, 25.0, 295.0, 295.0])], margin=0.0)
    manual = frame.copy()
    manual[25:295, 25:295] = cv2.GaussianBlur(frame[25:295, 25:295],
                                              (_k_of(270),) * 2, 0)
    assert not np.array_equal(out[25:295, 25:295], manual[25:295, 25:295])
    assert not np.array_equal(out[25:295, 25:295], frame[25:295, 25:295])
    assert np.array_equal(out[:25, :], frame[:25, :])


def test_fast_path_multi_box_margin_combo():
    """多框 + margin 组合：放回 resize 的 dsize 按外扩后 ROI（形状驱动）。
    两个重叠大框各自走快路径，双 ROI 外逐位原样、两个 ROI 内均有变化。"""
    frame = _structured(760, 720, seed=17)
    b1 = np.array([100.0, 100.0, 480.0, 420.0])   # ROI (43,43)-(537,477)
    b2 = np.array([300.0, 260.0, 640.0, 560.0])   # ROI (249,209)-(691,611)
    out = blur_regions(frame, [b1, b2], margin=0.15, margin_px=8)
    assert np.array_equal(out[:40, :40], frame[:40, :40])                    # 双 ROI 外
    assert not np.array_equal(out[60:180, 60:230], frame[60:180, 60:230])    # 仅 ROI1 内
    assert not np.array_equal(out[500:600, 500:650], frame[500:600, 500:650])  # 仅 ROI2 内


def _esw_px(img, y, x_lo, x_hi):
    """10%-90% 边缘展宽（px）：沿水平线量锐利阶跃被抹开的宽度——
    直接度量有效模糊尺度，对测试图频率成分不敏感。"""
    line = img[y, x_lo:x_hi].astype(np.float64)
    lo_v, hi_v = line[0], line[-1]
    x10 = int(np.argmax(line >= lo_v + 0.1 * (hi_v - lo_v)))
    x90 = int(np.argmax(line >= lo_v + 0.9 * (hi_v - lo_v)))
    return x90 - x10


def test_fast_path_edge_spread_width_matches_direct():
    """双侧锚定 σ（对抗核查盲区）：std 类断言只咬"糊得不够"，在已摧毁
    94% 的区间里分不出 σ_eff≈σ_target 与 σ_eff≈10×（降采样倍率算错的
    典型 bug）；阶跃边展宽比直接锚定有效模糊尺度，欠糊/过糊双向都咬。"""
    frame = np.zeros((700, 760, 3), np.uint8)
    frame[:, 380:] = 255                          # x=380 锐利阶跃
    box = np.array([40.0, 40.0, 680.0, 660.0])    # crop 620×640 → k=465 快路径
    fast = blur_regions(frame, [box], margin=0.0)
    manual = frame.copy()
    manual[40:660, 40:680] = cv2.GaussianBlur(frame[40:660, 40:680],
                                              (_k_of(620),) * 2, 0)
    w_fast = _esw_px(fast, 350, 160, 600)
    w_direct = _esw_px(manual, 350, 160, 600)
    assert w_direct > 0                           # 直接核自身确已抹开（~2.56σ）
    assert 0.7 <= w_fast / w_direct <= 1.4
