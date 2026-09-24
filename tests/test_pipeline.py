from pathlib import Path

from conftest import face_img, requires_assets

from faceblur_lite.ffmpeg_io import probe
from faceblur_lite.kernel import KernelParams
from faceblur_lite.orchestrator import run_batch, run_pipeline
from faceblur_lite.state import SqliteControlPlane
from faceblur_lite.testmedia import build_video
from faceblur_lite.worker import worker_loop

pytestmark = requires_assets


def test_worker_loop_full_single_chunk(tmp_path, move_video):
    """单 chunk 直通：处理 → 原子认领 merge → 合并 → qc_wait。"""
    cp = SqliteControlPlane(str(tmp_path / "w.db"))
    cp.init_db()
    final = str(tmp_path / "final.mp4")
    cp.submit_video(move_video[0], final,
                    [(0, move_video[0], str(tmp_path / "o0.mp4"))])
    stats = worker_loop(cp, "w0", KernelParams(det_every=2),
                        idle_exit_s=0.3, poll_s=0.05, ttl_s=60)
    assert stats == {"worker": "w0", "chunks_done": 1}
    assert cp.video_overview()[0]["status"] == "qc_wait"
    assert Path(final).exists()


def test_pipeline_end_to_end(tmp_path, move_video):
    """6s 视频 segment 2s → 多 chunk 多 Worker 并行 → done + qc 通过。"""
    out = str(tmp_path / "e2e.mp4")
    res = run_pipeline(move_video[0], out, str(tmp_path / "wd"),
                       workers=2, segment_s=2.0,
                       params=KernelParams(det_every=2))
    assert res.status == "done"
    assert res.qc.passed
    assert res.chunk_count >= 2
    assert abs(probe(out).duration_s - 6.0) < 1.0


def test_batch_multi_video_shared_pool(tmp_path, move_video, face_img):
    """R10：一个长视频（切 3 片）+ 一个短视频（快路径）共用 Worker 池。"""
    short = str(tmp_path / "short.mp4")
    build_video(short, face_img, duration_s=2.0)
    results = run_batch([move_video[0], short], str(tmp_path / "out"),
                        str(tmp_path / "wd"), workers=2, segment_s=2.0,
                        params=KernelParams(det_every=2))
    assert [r.status for r in results] == ["done", "done"]
    assert all(r.qc.passed for r in results)
    assert results[0].chunk_count >= 2      # 长视频切片并行
    assert results[1].chunk_count == 1      # 短视频快路径不切
    assert Path(results[0].output_path).exists()
    assert Path(results[1].output_path).exists()


def test_batch_failure_isolation(tmp_path, move_video):
    """R10：坏输入只废自己，好视频照常 done。"""
    results = run_batch([move_video[0], "/nonexistent.mp4"],
                        str(tmp_path / "out"), str(tmp_path / "wd"),
                        workers=2, segment_s=2.0,
                        params=KernelParams(det_every=2))
    by_status = {r.input_path: r.status for r in results}
    assert by_status[move_video[0]] == "done"
    assert by_status["/nonexistent.mp4"] == "failed"


def test_batch_same_stem_collision_isolated(move_video, tmp_path):
    """终审修复回归：同 stem 输出碰撞→后者显式 failed，前者正常完成（失败隔离）。"""
    import shutil as _sh
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    d1.mkdir(); d2.mkdir()
    _sh.copy(move_video[0], d1 / "a.mp4")
    _sh.copy(move_video[0], d2 / "a.mp4")
    res = run_batch([str(d1 / "a.mp4"), str(d2 / "a.mp4")],
                    outdir=str(tmp_path / "o"), workdir=str(tmp_path / "w"),
                    workers=2, segment_s=2.0)
    by_in = {r.input_path: r for r in res}
    assert by_in[str(d1 / "a.mp4")].status == "done"
    assert by_in[str(d2 / "a.mp4")].status == "failed"
    assert "碰撞" in (by_in[str(d2 / "a.mp4")].error or "")
