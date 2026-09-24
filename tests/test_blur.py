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
