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
