"""进度双出口单测：注入时钟/捕获写入，锁定节流、前缀、日志格式、
追加语义、写失败静默、快照 roundtrip、diff 规则。"""
import re

from faceblur_lite.progress import (ChunkState, ProgressReporter, VideoState,
                                    chunk_diff, format_progress,
                                    progress_snapshot)
from faceblur_lite.state import SqliteControlPlane


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_reporter(tmp_path, mono, lines):
    return ProgressReporter(console=lines.append,
                            log_path=tmp_path / "pipeline.log",
                            now_mono=mono, now_wall=mono)


def test_line_has_elapsed_prefix(tmp_path):
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    r.line("提交 3 视频 → 17 chunks")
    mono.advance(7)
    r.line("qc b_30s ✅ 0 违规")
    assert lines == ["[pipeline +0s] 提交 3 视频 → 17 chunks",
                     "[pipeline +7s] qc b_30s ✅ 0 违规"]


def test_aggregate_throttle_interval_and_change(tmp_path):
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    r.aggregate("1/3 · a 1/3")          # 首次即打
    mono.advance(1)
    r.aggregate("1/3 · a 1/3")          # 同文本 → 抑制
    mono.advance(1)                     # t=2，距首打 2s
    r.aggregate("2/3 · a 2/3")          # 有变化但 <5s → 抑制
    mono.advance(4)                     # t=6，距首打 6s
    r.aggregate("2/3 · a 2/3")          # ≥5s 且距上次打出已 6s → 打
    assert len(lines) == 2
    assert lines[0] == "[pipeline +0s] 1/3 · a 1/3"
    assert lines[1].endswith("2/3 · a 2/3")


def test_log_event_kv_and_iso_timestamp(tmp_path):
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    r.event("chunk_done", video="10b5dd2f", idx=3, worker="w0", proc_s=28.3)
    text = (tmp_path / "pipeline.log").read_text(encoding="utf-8")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3} chunk_done "
        r"video=10b5dd2f idx=3 worker=w0 proc_s=28.3\n", text)


def test_run_start_end_separators(tmp_path):
    mono, lines = FakeClock(), []
    r = make_reporter(tmp_path, mono, lines)
    r.run_start(inputs=2, workers=2)
    r.run_end(done=1, qc_failed=1, failed=0, wall_s=12.5)
    lines_out = (tmp_path / "pipeline.log").read_text(
        encoding="utf-8").splitlines()
    assert lines_out[0] == "=== run start ==="
    assert any(l.split(" ", 1)[1].startswith("run_start inputs=2") for l in lines_out)
    assert any(l.split(" ", 1)[1].startswith("run_end done=1") for l in lines_out)
    assert lines_out[-1] == "=== run end ==="


def test_log_write_failure_swallowed(tmp_path):
    mono, lines = FakeClock(), []
    r = ProgressReporter(console=lines.append,
                         log_path=tmp_path / "no-such-dir" / "x.log",
                         now_mono=mono, now_wall=mono)
    r.run_start(inputs=1)      # 目录不存在 → 静默，不抛
    r.event("chunk_done", video="v", idx=0)
    r.run_end(done=1)
    assert lines == []          # log 失败不影响 console 通道（此处无输出）


def test_console_none_disables_console(tmp_path):
    mono = FakeClock()
    r = ProgressReporter(console=None, log_path=tmp_path / "p.log",
                         now_mono=mono, now_wall=mono)
    r.line("不应出现")
    r.aggregate("1/2")
    assert r.event("run_start", inputs=1) is None   # 不抛即过


def test_snapshot_roundtrip(tmp_path):
    cp = SqliteControlPlane(str(tmp_path / "p.db"))
    cp.init_db()
    vid = cp.submit_video("in/a.mp4", "out/a.mp4",
                          [(0, "in/c0", "out/o0"), (1, "in/c1", "out/o1"),
                           (2, "in/c2", "out/o2")])
    c0 = cp.claim_chunk("w0", 900)
    c1 = cp.claim_chunk("w0", 900)
    cp.complete_chunk(c0["id"], {"ms": 100})
    cp.fail_chunk(c1["id"], "boom")            # attempt 0→1 回 pending
    snap = progress_snapshot(str(tmp_path / "p.db"))
    assert snap is not None
    videos, chunks = snap
    assert len(videos) == 1
    v = videos[0]
    assert (v.video_id, v.total, v.done) == (vid, 3, 1)
    by_idx = {c.idx: c for c in chunks}
    assert by_idx[0].status == "done"
    assert by_idx[0].worker_id == "w0"          # complete 后保留持有者
    assert by_idx[0].started_at is not None and by_idx[0].finished_at is not None
    assert (by_idx[1].status, by_idx[1].attempt, by_idx[1].worker_id) == ("pending", 1, None)
    assert (by_idx[2].status, by_idx[2].attempt) == ("pending", 0)


def test_snapshot_bad_path_returns_none(tmp_path):
    assert progress_snapshot(str(tmp_path / "ghost.db")) is None


def test_chunk_diff_rules():
    prev = {("v", 0): ChunkState("v", 0, "done", 0, "w0", 1.0, 2.0),
            ("v", 1): ChunkState("v", 1, "running", 0, "w1", 3.0, None),
            ("v", 2): ChunkState("v", 2, "pending", 1, None, None, None)}
    cur = [ChunkState("v", 0, "done", 0, "w0", 1.0, 2.0),      # 不变 → 排除
           ChunkState("v", 1, "done", 0, "w1", 3.0, 4.0),      # 状态变 → 收
           ChunkState("v", 2, "pending", 2, None, None, None),  # attempt 变 → 收
           ChunkState("v", 3, "running", 0, "w2", 5.0, None)]  # 新行 → 收
    diff = chunk_diff(prev, cur)
    assert [c.idx for c in diff] == [1, 2, 3]


def test_format_progress():
    vs = [VideoState("v1", "/x/a_5min.mp4", "processing", 10, 10),
          VideoState("v2", "/x/b_30s.mp4", "merging", 2, 1),
          VideoState("v3", "/x/c.mp4", "qc_wait", 1, 0)]
    assert format_progress(vs) == "11/13 · a_5min ✔ · b_30s 1/2 · c 0/1"
    assert format_progress([]) == "0/0"
