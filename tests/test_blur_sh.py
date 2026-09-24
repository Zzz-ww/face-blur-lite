import subprocess
from pathlib import Path

from conftest import face_img, requires_assets

ROOT = Path(__file__).resolve().parents[1]
BLUR_SH = ROOT / "scripts" / "blur.sh"

pytestmark = requires_assets


def run_blur(*args, timeout=300):
    return subprocess.run(["bash", str(BLUR_SH), *args],
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
