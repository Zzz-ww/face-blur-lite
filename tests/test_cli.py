import subprocess
import sys
from pathlib import Path

from conftest import requires_assets

from faceblur_lite.cli import main

pytestmark = requires_assets


def test_cli_single(move_video, tmp_path):
    out = str(tmp_path / "cli.mp4")
    assert main(["single", move_video[0], out, "--det-every", "2"]) == 0
    assert Path(out).exists()


def test_cli_split_merge_roundtrip(move_video, tmp_path):
    """R13：算子 CLI 子命令化——可作为独立命令被 DAG 引擎直接调用。"""
    r = subprocess.run(
        [sys.executable, "-m", "faceblur_lite.cli", "split",
         move_video[0], str(tmp_path / "c"), "--segment", "2.0"],
        capture_output=True, text=True, check=True)
    chunks = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(chunks) >= 2
    out = str(tmp_path / "merged.mp4")
    subprocess.run(
        [sys.executable, "-m", "faceblur_lite.cli", "merge", out, *chunks],
        check=True)
    assert Path(out).exists()


def test_cli_pipeline_workdir_reuse_exit_2(move_video, tmp_path, capsys):
    """BUG-017：复用非空 workdir → CLI 退出码 2（输入错误类）+ stderr 指引。"""
    wd = str(tmp_path / "wd")
    assert main(["pipeline", move_video[0], "--outdir", str(tmp_path / "o"),
                 "--workdir", wd, "--workers", "1",
                 "--segment", "3"]) == 0
    capsys.readouterr()                   # 清第一轮输出
    assert main(["pipeline", move_video[0], "--outdir", str(tmp_path / "o2"),
                 "--workdir", wd, "--workers", "1",
                 "--segment", "3"]) == 2
    captured = capsys.readouterr()
    assert "workdir 复用被拒绝" in captured.err
    assert "pipeline.db" in captured.err   # 指引须点名处置对象
    assert captured.out == ""              # 拒绝轮 stdout 无 JSON


def test_sample_fps_none_all_parses_to_full_density():
    """顺手件 1：--sample-fps 接受 none/all → None（全密度逐帧）。"""
    from faceblur_lite.cli import _sample_fps
    assert _sample_fps("none") is None
    assert _sample_fps("all") is None
    assert _sample_fps("ALL") is None
    assert _sample_fps("2") == 2.0
    assert _sample_fps("0.5") == 0.5


def test_qc_exit_code_three_levels():
    """退出码分级：0 全过 / 1 仅边缘 / 3 含致命（2 留给环境/输入错误）。"""
    from faceblur_lite.cli import _qc_exit_code
    from faceblur_lite.qc import QCReport, Violation
    ok = QCReport()
    marginal = QCReport(violations=[
        Violation(0.0, 0.55, (0, 0, 9, 9), "marginal")])
    fatal = QCReport(violations=[
        Violation(0.0, 0.55, (0, 0, 9, 9), "marginal"),
        Violation(1.0, 0.90, (0, 0, 9, 9), "fatal")])
    assert _qc_exit_code(ok) == 0
    assert _qc_exit_code(marginal) == 1
    assert _qc_exit_code(fatal) == 3


def _fake_results():
    class R:
        video_id = "v"
        status = "done"
        qc = None
        chunk_count = 1
        wall_s = 0.0
        output_path = "o"
        error = None
    return [R()]


def test_single_passes_detector_to_params(monkeypatch, tmp_path):
    from faceblur_lite import cli

    class FakeRep:                    # cli single 分支只读这些字段进 JSON
        frames = 1
        detections = 0
        predicted_frames = 0
        track_count = 0
        segments = []
        duration_s = 0.0
        wall_s = 0.0

    seen = {}
    monkeypatch.setattr(
        "faceblur_lite.kernel.process_video",
        lambda i, o, params=None, model_path=None:
        seen.update(params=params) or FakeRep())
    rc = cli.main(["single", str(tmp_path / "in.mp4"),
                   str(tmp_path / "out.mp4"), "--detector", "scrfd"])
    assert rc == 0 and seen["params"].detector == "scrfd"


def test_pipeline_qc_detector_defaults_to_detector(monkeypatch, tmp_path):
    """--qc-detector 缺省跟随 --detector；显式给则覆盖（交叉验收位）。"""
    from faceblur_lite import cli
    seen = {}
    monkeypatch.setattr(
        "faceblur_lite.orchestrator.run_batch",
        lambda *a, **kw: seen.update(kw) or _fake_results())
    cli.main(["pipeline", str(tmp_path / "x.mp4"),
              "--outdir", str(tmp_path), "--workdir", str(tmp_path / "wd1"),
              "--detector", "scrfd"])
    assert seen["qc_detector"] == "scrfd" and seen["params"].detector == "scrfd"
    cli.main(["pipeline", str(tmp_path / "x.mp4"),
              "--outdir", str(tmp_path), "--workdir", str(tmp_path / "wd2"),
              "--detector", "scrfd", "--qc-detector", "yunet"])
    assert seen["qc_detector"] == "yunet"


def test_qc_passes_detector(monkeypatch, tmp_path):
    from faceblur_lite import cli
    from faceblur_lite.qc import QCReport
    seen = {}
    monkeypatch.setattr("faceblur_lite.qc.qc_video",
                        lambda path, **kw: seen.update(kw) or QCReport())
    rc = cli.main(["qc", str(tmp_path / "v.mp4"),
                   "--detector", "scrfd"])
    assert rc == 0 and seen["detector"] == "scrfd"
