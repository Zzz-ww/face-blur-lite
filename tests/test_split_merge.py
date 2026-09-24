from pathlib import Path

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


def test_merge_relative_paths(move_video, monkeypatch, tmp_path):
    """BUG-007 回归（2026-09-24 执行期追加）：ffmpeg concat demuxer 按列表
    文件所在目录解析相对条目——输出目录 ≠ chunk 目录时相对条目 ENOENT，
    merger 必须写绝对路径。"""
    monkeypatch.chdir(tmp_path)          # 全程相对路径，复现用户 CLI 场景
    chunks = split_video(move_video[0], "chunks", segment_s=2.0)
    assert len(chunks) >= 2
    Path("out").mkdir()                  # 输出父目录（编排器/CLI 职责）
    merge_chunks(chunks, "out/merged.mp4")
    assert abs(probe("out/merged.mp4").duration_s - 6.0) < 1.0


def test_split_cleans_stale_chunks(move_video, tmp_path):
    """终审修复回归：同 workdir 重跑不得拾取上一次的残留切片。"""
    out_dir = tmp_path / "c"
    out_dir.mkdir()
    (out_dir / "chunk_005.mp4").write_bytes(b"stale")   # 上一次更细切片的残留
    chunks = split_video(move_video[0], str(out_dir), segment_s=2.0)
    assert len(chunks) == 3                              # 旧代码=4（拾取残留；实测 6s/2s 新切=3 片）
    assert not (out_dir / "chunk_005.mp4").exists()      # 残留已被清理
    merge_chunks(chunks, str(tmp_path / "m.mp4"))
    assert abs(probe(str(tmp_path / "m.mp4")).duration_s - 6.0) < 1.0


def test_merge_apostrophe_path(move_video, tmp_path):
    """终审修复回归：路径含单引号时 concat 列表仍可解析。"""
    odd = tmp_path / "jack's"
    odd.mkdir()
    chunks = split_video(move_video[0], str(odd / "c"), segment_s=2.0)
    out = str(odd / "merged.mp4")
    merge_chunks(chunks, out)
    assert abs(probe(out).duration_s - 6.0) < 1.0
