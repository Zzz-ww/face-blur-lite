import pytest

from faceblur_lite.state import SqliteControlPlane


@pytest.fixture()
def cp(tmp_path):
    cp_ = SqliteControlPlane(str(tmp_path / "s.db"))
    cp_.init_db()
    return cp_


def _mk(cp, n=3):
    return cp.submit_video(
        "in.mp4", "out.mp4",
        [(i, f"c{i}.mp4", f"o{i}.mp4") for i in range(n)])


def test_claim_exclusive(cp):
    _mk(cp, n=3)
    a = cp.claim_chunk("w1", ttl_s=60)
    b = cp.claim_chunk("w2", ttl_s=60)
    assert a["id"] != b["id"]
    assert a["status"] == "running"


def test_atomic_join_single_claim(cp):
    _mk(cp, n=2)
    t1 = cp.claim_chunk("w1", 60)
    t2 = cp.claim_chunk("w2", 60)
    assert cp.complete_chunk(t1["id"], {}) is False
    assert cp.complete_chunk(t2["id"], {}) is True   # 最后完成者认领 merge
    assert cp.video_overview()[0]["status"] == "merging"
    cp.complete_merge(t2["video_id"], "out.mp4")
    assert cp.video_overview()[0]["status"] == "qc_wait"


def test_lease_reclaim_and_attempt(cp):
    _mk(cp, n=1)
    cp.claim_chunk("w1", ttl_s=-1)          # 立即过期（模拟 Worker 暴死）
    assert cp.reclaim_expired() == 1
    t2 = cp.claim_chunk("w2", 60)           # 可重新认领且 attempt+1
    assert t2["attempt"] == 1


def test_fail_exhausts_to_video_failed(cp):
    _mk(cp, n=1)
    t = cp.claim_chunk("w1", 60)
    for _ in range(3):
        cp.fail_chunk(t["id"], "boom", max_attempts=3)
    assert cp.video_overview()[0]["status"] == "failed"
    assert cp.claim_chunk("w2", 60) is None  # 失败视频不再派发


def test_multi_video_shared_queue(cp):
    v1 = _mk(cp, n=2)
    v2 = _mk(cp, n=2)
    got = [cp.claim_chunk("w", 60) for _ in range(4)]
    assert len({t["id"] for t in got}) == 4          # 互斥：4 个不同 chunk
    assert {t["video_id"] for t in got} == {v1, v2}  # 多视频交错同池（R10）


def test_complete_chunk_late_ack_rejected(cp):
    _mk(cp, n=1)
    t1 = cp.claim_chunk("w1", ttl_s=-1)     # 拿到即过期（模拟暴死）
    cp.reclaim_expired()                    # 重派 → pending
    assert cp.complete_chunk(t1["id"], {}) is False   # 迟到 ack：pending 不算数
    t2 = cp.claim_chunk("w2", 60)
    assert cp.complete_chunk(t2["id"], {}) is True    # 现持有者完成才认 join


def test_fail_chunk_terminal_irreversible(cp):
    _mk(cp, n=2)
    t1 = cp.claim_chunk("w1", 60)
    t2 = cp.claim_chunk("w2", 60)
    cp.complete_chunk(t1["id"], {})
    cp.complete_chunk(t2["id"], {})         # join 完成 → merging
    cp.fail_chunk(t1["id"], "merge 崩了", max_attempts=3)   # BUG-007 场景
    ch = cp.get_chunk(t1["id"])
    assert ch["status"] == "done" and ch["attempt"] == 0   # done 不翻回
    assert cp.video_overview()[0]["status"] == "merging"


def test_reclaim_skips_terminal_video(cp):
    _mk(cp, n=2)
    cp.claim_chunk("w1", ttl_s=-1)          # 过期 running
    t2 = cp.claim_chunk("w2", 60)
    for _ in range(3):
        cp.fail_chunk(t2["id"], "boom", max_attempts=3)    # video → failed
    assert cp.reclaim_expired() == 0        # 终态视频的过期 chunk 不回收
    assert cp.video_overview()[0]["error"] == "boom"       # 原因不被改写


def test_fail_exhaustion_records_final_attempt(cp):
    _mk(cp, n=1)
    t = cp.claim_chunk("w1", 60)
    for _ in range(3):
        cp.fail_chunk(t["id"], "boom", max_attempts=3)
    ch = cp.get_chunk(t["id"])
    assert ch["status"] == "failed" and ch["attempt"] == 3  # 耗尽分支也 +1


def test_reclaim_exhaustion_records_final_attempt(cp):
    _mk(cp, n=1)
    t = cp.claim_chunk("w0", ttl_s=-1)
    for _ in range(3):
        cp.reclaim_expired()
        cp.claim_chunk("w", ttl_s=-1)
    ch = cp.get_chunk(t["id"])
    assert ch["status"] == "failed" and ch["attempt"] == 3  # 与 fail 路径对齐
