import pytest

from faceblur_lite.state import SqliteControlPlane


@pytest.fixture(params=["sqlite", "go"])
def cp(request, tmp_path):
    if request.param == "sqlite":
        cp_ = SqliteControlPlane(str(tmp_path / "s.db"))
    else:
        # getfixturevalue 惰性求值：sqlite 分支不触碰 go 工具链
        from faceblur_lite.remote import HttpControlPlane
        cp_ = HttpControlPlane(request.getfixturevalue("go_cp_url"))
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
    assert cp.complete_chunk(t1["id"], {}, worker_id="w1",
                             attempt=t1["attempt"]) is False
    assert cp.complete_chunk(t2["id"], {}, worker_id="w2",
                             attempt=t2["attempt"]) is True   # 最后完成者认领 merge
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
    while t is not None:                    # 现任失败 → 重派 → 再认领，直至耗尽
        cp.fail_chunk(t["id"], "boom", max_attempts=3,
                      worker_id="w1", attempt=t["attempt"])
        t = cp.claim_chunk("w1", 60)
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
    assert cp.complete_chunk(t1["id"], {}, worker_id="w1",
                             attempt=t1["attempt"]) is False  # 迟到 ack：pending 不算数
    t2 = cp.claim_chunk("w2", 60)
    assert cp.complete_chunk(t2["id"], {}, worker_id="w2",
                             attempt=t2["attempt"]) is True    # 现持有者完成才认 join


def test_fail_chunk_terminal_irreversible(cp):
    _mk(cp, n=2)
    t1 = cp.claim_chunk("w1", 60)
    t2 = cp.claim_chunk("w2", 60)
    cp.complete_chunk(t1["id"], {}, worker_id="w1", attempt=t1["attempt"])
    cp.complete_chunk(t2["id"], {}, worker_id="w2", attempt=t2["attempt"])
    cp.fail_chunk(t1["id"], "merge 崩了", max_attempts=3,        # BUG-007 场景
                  worker_id="w1", attempt=t1["attempt"])
    ch = cp.get_chunk(t1["id"])
    assert ch["status"] == "done" and ch["attempt"] == 0   # done 不翻回
    assert cp.video_overview()[0]["status"] == "merging"


def test_reclaim_skips_terminal_video(cp):
    _mk(cp, n=2)
    cp.claim_chunk("w1", ttl_s=-1)          # 过期 running
    t2 = cp.claim_chunk("w2", 60)
    while t2 is not None:
        cp.fail_chunk(t2["id"], "boom", max_attempts=3,
                      worker_id="w2", attempt=t2["attempt"])
        t2 = cp.claim_chunk("w2", 60)       # video → failed，耗尽
    assert cp.reclaim_expired() == 0        # 终态视频的过期 chunk 不回收
    assert cp.video_overview()[0]["error"] == "boom"       # 原因不被改写


def test_fail_exhaustion_records_final_attempt(cp):
    _mk(cp, n=1)
    t = cp.claim_chunk("w1", 60)
    cid = t["id"]
    while t is not None:
        cp.fail_chunk(t["id"], "boom", max_attempts=3,
                      worker_id="w1", attempt=t["attempt"])
        t = cp.claim_chunk("w1", 60)
    ch = cp.get_chunk(cid)
    assert ch["status"] == "failed" and ch["attempt"] == 3  # 耗尽分支也 +1


def test_reclaim_exhaustion_records_final_attempt(cp):
    _mk(cp, n=1)
    t = cp.claim_chunk("w0", ttl_s=-1)
    for _ in range(3):
        cp.reclaim_expired()
        cp.claim_chunk("w", ttl_s=-1)
    ch = cp.get_chunk(t["id"])
    assert ch["status"] == "failed" and ch["attempt"] == 3  # 与 fail 路径对齐


def test_missing_chunk_id_semantics(cp):
    _mk(cp, n=1)                                 # 正常提交，用不存在 id 打守卫路径
    assert cp.complete_chunk("no-such-id", {}, worker_id="w1",
                             attempt=0) is False
    cp.fail_chunk("no-such-id", "x", max_attempts=3,
                  worker_id="w1", attempt=0)   # 静默 no-op，不抛
    t = cp.claim_chunk("w1", 60)
    assert t is not None                          # 缺 id 调用不产生副作用


def test_finish_video_output_path_sync(cp):
    """qc_failed 隔离（spec 2026-09-25 §3.1）：finish_video 可选改写
    output_path，缺省不动——sqlite/go 两控制面一致。"""
    vid = _mk(cp, n=1)
    cp.finish_video(vid, "qc_failed", output_path="out/qc_failed/o.mp4")
    assert cp.get_video(vid)["output_path"] == "out/qc_failed/o.mp4"
    cp.finish_video(vid, "qc_failed")          # 缺省=不动
    assert cp.get_video(vid)["output_path"] == "out/qc_failed/o.mp4"


def test_fencing_late_ack_after_reclaim(cp):
    """契约 v1.2 刀 4 时序：lease 过期重派后，原持有者迟到 ack/fail 拒收。"""
    _mk(cp, n=1)
    t1 = cp.claim_chunk("w1", ttl_s=-1)     # 上一纪元：拿到即过期
    cp.reclaim_expired()                    # 重派 → pending（attempt 1）
    t2 = cp.claim_chunk("w2", 60)           # 现任纪元
    assert cp.complete_chunk(t1["id"], {}, worker_id="w1",
                             attempt=t1["attempt"]) is False  # 僵尸 ack 拒
    cp.fail_chunk(t1["id"], "迟到 fail", worker_id="w1",
                  attempt=t1["attempt"])    # 僵尸 fail：不得弹回现任任务
    cur = cp.get_chunk(t2["id"])
    assert (cur["status"], cur["attempt"]) == ("running", 1)
    assert cp.complete_chunk(t2["id"], {}, worker_id="w2",
                             attempt=t2["attempt"]) is True    # 现任过


def test_fencing_same_name_reincarnation(cp):
    """同名转世：gocp 共享队列模式 Worker 同名复用——裸 worker_id 围栏
    挡不住，(worker_id, attempt) 复合围栏挡住（attempt=持有纪元唯一标识，
    SQS receipt handle 等价物）。"""
    _mk(cp, n=1)
    t1 = cp.claim_chunk("w0", ttl_s=-1)     # 上一轮僵尸 w0，attempt=0
    cp.reclaim_expired()
    t2 = cp.claim_chunk("w0", 60)           # 同名 w0 再认领，attempt=1
    assert t2["worker_id"] == "w0" and t2["attempt"] == 1
    assert cp.complete_chunk(t1["id"], {}, worker_id="w0",
                             attempt=t1["attempt"]) is False  # 旧纪元拒
    assert cp.complete_chunk(t1["id"], {}, worker_id="w0",
                             attempt=t2["attempt"]) is True    # 新纪元过
