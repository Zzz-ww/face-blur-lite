from conftest import requires_assets

from faceblur_lite.kernel import KernelParams
from faceblur_lite.orchestrator import run_batch
from faceblur_lite.stats import collect_stats, format_stats
from faceblur_lite.state import SqliteControlPlane


@requires_assets
def test_stats_after_pipeline(tmp_path, move_video):
    run_batch([move_video[0]], str(tmp_path / "out"), str(tmp_path / "wd"),
              workers=2, segment_s=2.0, params=KernelParams(det_every=2))
    s = collect_stats(str(tmp_path / "wd" / "pipeline.db"))
    assert s["videos"].get("done") == 1
    assert s["chunks_total"] >= 2
    assert s["chunks_by_status"].get("done") == s["chunks_total"]
    assert 0 <= s["queue_s"]["p50"] <= s["queue_s"]["p95"]
    assert 0 <= s["proc_s"]["p50"] <= s["proc_s"]["p95"]
    assert s["throughput_per_min"] > 0
    assert s["fail_rate"] == 0.0
    assert "chunks/min" in format_stats(s)


def test_stats_empty_db(tmp_path):
    """空库零除/空列表安全（无资产依赖）。"""
    cp = SqliteControlPlane(str(tmp_path / "e.db"))
    cp.init_db()
    s = collect_stats(str(tmp_path / "e.db"))
    assert s["chunks_total"] == 0
    assert s["queue_s"]["p50"] == 0.0
    assert s["throughput_per_min"] == 0.0
    assert s["fail_rate"] == 0.0
