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


def test_cli_pipeline_workdir_reuse_exit_2(move_video, tmp_path):
    """BUG-017：复用非空 workdir → CLI 退出码 2（输入错误类）+ stderr 指引。"""
    wd = str(tmp_path / "wd")
    assert main(["pipeline", move_video[0], "--outdir", str(tmp_path / "o"),
                 "--workdir", wd, "--workers", "1",
                 "--segment", "3"]) == 0
    assert main(["pipeline", move_video[0], "--outdir", str(tmp_path / "o2"),
                 "--workdir", wd, "--workers", "1",
                 "--segment", "3"]) == 2
