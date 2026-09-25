from conftest import go_cp_url, requires_assets

from faceblur_lite.control import ControlPlane
from faceblur_lite.kernel import KernelParams
from faceblur_lite.orchestrator import run_pipeline
from faceblur_lite.remote import HttpControlPlane


def test_http_adapter_satisfies_protocol(go_cp_url):
    """HttpControlPlane 是 ControlPlane 协议的合法实现。"""
    cp: ControlPlane = HttpControlPlane(go_cp_url)
    cp.init_db()
    vid = cp.submit_video("in.mp4", "out.mp4", [(0, "c0.mp4", "o0.mp4")])
    t = cp.claim_chunk("w1", 60)
    assert t is not None and t["video_id"] == vid
    assert cp.complete_chunk(t["id"], {}, worker_id="w1",
                             attempt=t["attempt"]) is True   # 单 chunk：完成即认领 merge
    cp.complete_merge(vid, "out.mp4")
    assert cp.get_video(vid)["status"] == "qc_wait"


@requires_assets
def test_pipeline_with_go_control_plane(tmp_path, move_video, go_cp_url):
    """R7：Go 控制面 + Python 数据面，同一契约跑通完整管线。"""
    out = str(tmp_path / "e2e_go.mp4")
    res = run_pipeline(move_video[0], out, str(tmp_path / "wd"),
                       workers=2, segment_s=2.0,
                       params=KernelParams(det_every=2), cp_url=go_cp_url)
    assert res.status == "done"
    assert res.qc.passed
    assert res.chunk_count >= 2


@requires_assets
def test_go_control_plane_reuse_exempt(go_cp_url, move_video, tmp_path):
    """BUG-017 边界裁定回归：gocp=共享队列语义，同 workdir 连跑两轮
    不触发 WorkdirReuseError（若豁免被删，此处在第二轮变红）。"""
    wd = str(tmp_path / "wd")
    r1 = run_pipeline(move_video[0], str(tmp_path / "a.mp4"), wd,
                      workers=1, segment_s=3.0, cp_url=go_cp_url)
    r2 = run_pipeline(move_video[0], str(tmp_path / "b.mp4"), wd,
                      workers=1, segment_s=3.0, cp_url=go_cp_url)
    assert r1.status == "done" and r2.status == "done"
    vids = HttpControlPlane(go_cp_url).video_overview()
    assert len(vids) == 2            # 库即队列：两轮累积而非拒绝


def test_gocp_rejects_unfenced_write(go_cp_url):
    """契约 v1.2：缺 worker_id/attempt 的 complete/fail POST → 400——
    防新旧二进制混跑时围栏静默失效（旧客户端字段缺失必须显式炸）。"""
    import json as _json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    for path, body in [("/chunks/x/complete", {"metrics": {}}),
                       ("/chunks/x/fail", {"error": "e"})]:
        req = Request(go_cp_url + path, method="POST",
                      data=_json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
        try:
            urlopen(req)
            raise AssertionError(f"{path} 缺围栏字段应 400")
        except HTTPError as e:
            assert e.code == 400
