"""SCRFD 解码纯函数单元测试（合成张量，无模型依赖）——spec 2026-09-26 §1/§7。
spike 踩明的两坑各有专测：bbox dist ×stride、anchor np.repeat 逐位置交错。"""
import numpy as np
import pytest

from faceblur_lite.detector import _decode_scrfd, letterbox_shape


def _mk_outputs(size=64):
    """合成 9 张量：strides(8,16,32) 对应 feat 8/4/2，2 锚/cell。"""
    outs = []
    for feat in (8, 4, 2):        # score×3
        outs.append(np.zeros((feat * feat * 2, 1), np.float32))
    for feat in (8, 4, 2):        # dist-bbox×3
        outs.append(np.ones((feat * feat * 2, 4), np.float32) * 99)
    for feat in (8, 4, 2):        # kps×3（解码不读）
        outs.append(np.zeros((feat * feat * 2, 10), np.float32))
    return outs


def test_letterbox_shape_portrait_and_square():
    """720×1406 竖屏 → 内容带 328×640、上下 pad 156；方图零 pad。"""
    scale, nh, nw, pt, pl = letterbox_shape(720, 1406, 640)
    assert abs(scale - 640 / 1406) < 1e-9
    assert (nh, nw) == (328, 640) and (pt, pl) == (156, 0)
    assert letterbox_shape(640, 640, 640) == (1.0, 640, 640, 0, 0)


def test_decode_dist_times_stride():
    """坑 1：dist 预测 ×stride——dist=1@stride16 = 16px，不是 1px。"""
    outs = _mk_outputs()
    outs[4][11 * 2 + 1] = [1, 1, 1, 1]     # stride16 位置 (row2,col3) 第 2 锚
    outs[1][11 * 2 + 1] = 0.9
    dets = _decode_scrfd(outs, 0.5, 50, 1.0, 0, 0)
    assert len(dets) == 1
    # center=((3+.5)*16,(2+.5)*16)=(56,40)；dist16 → 框 (40,24,72,56)
    assert dets[0].xyxy().tolist() == [40.0, 24.0, 72.0, 56.0]
    assert dets[0].conf == pytest.approx(0.9)


def test_decode_anchor_interleave_per_position():
    """坑 2：同位置两锚共享中心（np.repeat 逐位置交错，非 tile）。
    锚 22/23 必须都落在 center=(56,40)——tile 语义会把它俩分到不同格点。"""
    outs = _mk_outputs()
    outs[4][22] = [3, 2, 3, 2]             # stride16 位置 11 锚 0（对称 l==r,t==b → 框心=锚心）
    outs[4][23] = [1, 1, 1, 1]             # 位置 11 锚 1
    outs[1][22] = 0.9
    outs[1][23] = 0.8
    dets = _decode_scrfd(outs, 0.5, 50, 1.0, 0, 0)
    assert len(dets) == 2
    cx = {(d.x1 + d.x2) / 2 for d in dets}
    cy = {(d.y1 + d.y2) / 2 for d in dets}
    assert cx == {56.0} and cy == {40.0}   # 同中心、不同展宽


def test_decode_maps_back_to_original_coords():
    """letterbox 逆映射：canvas → 原图 = (canvas - pad) / scale。"""
    outs = _mk_outputs()
    outs[4][22] = [1, 1, 1, 1]
    outs[1][22] = 0.9
    dets = _decode_scrfd(outs, 0.5, 50, 0.5, 10, 20)
    # canvas 框 (40,24,72,56) → ((40-20)/.5, (24-10)/.5, (72-20)/.5, (56-10)/.5)
    assert dets[0].xyxy().tolist() == [40.0, 28.0, 104.0, 92.0]


def test_decode_nms_and_top_k():
    """同格点重叠两锚 NMS 留一；三个不相交格点 top_k=2 留分数最高两枚。"""
    outs = _mk_outputs()
    # stride32（feat=2，格点中心 (16,16)/(48,16)/(16,48)/(48,48)）
    outs[2][0] = 0.9                       # (16,16) 锚 0
    outs[2][1] = 0.7                       # (16,16) 锚 1——同中心重叠
    outs[2][4] = 0.85                      # (16,48) 锚 0
    outs[2][6] = 0.8                       # (48,48) 锚 0
    outs[5][0] = outs[5][1] = outs[5][4] = outs[5][6] = [1, 1, 1, 1]
    dets = _decode_scrfd(outs, 0.5, 2, 1.0, 0, 0)
    assert len(dets) == 2
    confs = sorted((d.conf for d in dets), reverse=True)
    assert confs == pytest.approx([0.9, 0.85])   # NMS 杀同中心 0.7；top_k=2 再裁 0.8


def test_decode_empty_when_no_score_above_thr():
    outs = _mk_outputs()
    assert _decode_scrfd(outs, 0.5, 50, 1.0, 0, 0) == []
