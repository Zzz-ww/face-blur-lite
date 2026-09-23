from conftest import requires_assets

from faceblur_lite.ffmpeg_io import probe
from faceblur_lite.merger import merge_chunks
from faceblur_lite.splitter import split_video

pytestmark = requires_assets


def test_split_fast_path(move_video, tmp_path):
    # move_video 是 6s，segment 30s → 不切，原样返回
    assert split_video(move_video[0], str(tmp_path / "s"),
                       segment_s=30.0) == [move_video[0]]


def test_split_and_merge_roundtrip(move_video, tmp_path):
    chunks = split_video(move_video[0], str(tmp_path / "c"), segment_s=2.0)
    assert len(chunks) >= 2                       # 6s/2s，关键帧对齐
    out = str(tmp_path / "merged.mp4")
    merge_chunks(chunks, out)
    assert abs(probe(out).duration_s - 6.0) < 1.0
