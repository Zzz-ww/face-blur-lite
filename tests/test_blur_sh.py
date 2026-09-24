import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import face_img, move_video, requires_assets

ROOT = Path(__file__).resolve().parents[1]
BLUR_SH = ROOT / "scripts" / "blur.sh"

pytestmark = requires_assets

needs_go = pytest.mark.skipif(
    shutil.which("go") is None, reason="需要 go 工具链（brew install go）")


def run_blur(*args, timeout=300):
    return subprocess.run(["bash", str(BLUR_SH), *args],
                          capture_output=True, text=True, timeout=timeout)


def run_blur_from(base, *args, timeout=300):
    return subprocess.run(["bash", str(base / "scripts" / "blur.sh"), *args],
                          capture_output=True, text=True, timeout=timeout)


def test_bad_paths_exit_2(tmp_path):
    r = run_blur("run", str(tmp_path / "nope.mp4"))
    assert r.returncode == 2
    assert "不存在" in r.stderr


def test_empty_dir_exit_2(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    r = run_blur("run", str(d))
    assert r.returncode == 2


def test_stem_collision_exit_2(tmp_path):
    """spec §4：同 stem 碰撞在提交前整组报出，不静默重命名。"""
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    d1.mkdir(); d2.mkdir()
    (d1 / "a.mp4").write_bytes(b"x")
    (d2 / "a.mp4").write_bytes(b"x")
    r = run_blur("run", str(d1 / "a.mp4"), str(d2 / "a.mp4"))
    assert r.returncode == 2
    assert "碰撞" in r.stderr
    assert str(d1 / "a.mp4") in r.stderr and str(d2 / "a.mp4") in r.stderr


def test_run_two_videos(tmp_path, face_img):
    """≥2 输入 auto→pipeline：两个 2s 视频一批，全 done。"""
    from faceblur_lite.testmedia import build_video
    v1, v2 = str(tmp_path / "m1.mp4"), str(tmp_path / "m2.mp4")
    build_video(v1, face_img, duration_s=2.0)
    build_video(v2, face_img, duration_s=2.0)
    out = tmp_path / "out"
    r = run_blur("run", v1, v2, "--outdir", str(out),
                 "--workers", "2", "--segment", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / "m1_blurred.mp4").exists()
    assert (out / "m2_blurred.mp4").exists()
    assert "── 批量指标 ──" in r.stdout          # stats 已聚合
    assert "chunks/min" in r.stdout
    assert r.stdout.count("qc 通过") == 2        # 逐输出 qc 摘要行


def test_single_video_report(tmp_path, face_img):
    """1 输入 auto→single：kernel 摘要行 + qc 行都在。"""
    from faceblur_lite.testmedia import build_video
    v = str(tmp_path / "one.mp4")
    build_video(v, face_img, duration_s=2.0)
    out = tmp_path / "o"
    r = run_blur("run", v, "--outdir", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / "one_blurred.mp4").exists()
    assert "帧" in r.stdout and "track" in r.stdout   # kernel 摘要
    assert "qc 通过" in r.stdout                      # qc 摘要


def test_custom_outdir_inside_repo_gitignored(tmp_path):
    """spec §4 输出隔离：仓库内未忽略的 outdir 被自动追加 .gitignore；
    隔离先于输入校验（坏输入也触发追加后 exit 2）。"""
    gi = ROOT / ".gitignore"
    original = gi.read_text()
    probe = ROOT / "iso_probe_dir"
    try:
        r = run_blur("run", str(tmp_path / "nope.mp4"),
                     "--outdir", "iso_probe_dir")
        assert r.returncode == 2                     # 坏输入仍失败
        assert "iso_probe_dir/" in gi.read_text()    # 但隔离已生效
        assert probe.exists()
        assert "自动追加" in r.stdout
    finally:
        gi.write_text(original)
        if probe.exists():
            import shutil
            shutil.rmtree(probe)


def test_option_missing_value_exit_2(tmp_path):
    """选项缺值（--outdir 收尾）→ 干净 exit 2，不是 bash unbound 报错的 1。"""
    r = run_blur("run", str(tmp_path / "nope.mp4"), "--outdir")
    assert r.returncode == 2
    assert "需要一个" in r.stderr


def test_option_empty_value_exit_2(tmp_path):
    r = run_blur("run", str(tmp_path / "nope.mp4"), "--outdir", "")
    assert r.returncode == 2


@needs_go
def test_go_mode_pipeline(tmp_path, move_video):
    """--go 单输入也隐含 pipeline：gocp 构建/起服/跑批/清理全链路。"""
    out = tmp_path / "out"
    r = run_blur("run", move_video[0], "--outdir", str(out),
                 "--go", "--segment", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / "move_blurred.mp4").exists()
    assert "gocp" in r.stdout
    assert "chunks/min" in r.stdout  # stats 从 gocp.db 打出批量指标（F1 回归锁）


def test_go_with_single_mode_conflict(move_video, tmp_path):
    """--go 与 --mode single 矛盾 → exit 2（无需 go 工具链即可测）。"""
    r = run_blur("run", move_video[0], "--outdir", str(tmp_path / "o"),
                 "--go", "--mode", "single")
    assert r.returncode == 2
    assert "矛盾" in r.stderr


def test_outdir_repo_root_via_symlink(tmp_path):
    """符号链接调用 + --outdir 指向链接路径也必须被仓库根护栏拦下（REPO 用 pwd -P）。"""
    link = tmp_path / "repo-link"
    link.symlink_to(ROOT)
    r = run_blur_from(link, "run", str(tmp_path / "nope.mp4"), "--outdir", str(link))
    assert r.returncode == 2
    assert "仓库根" in r.stderr
