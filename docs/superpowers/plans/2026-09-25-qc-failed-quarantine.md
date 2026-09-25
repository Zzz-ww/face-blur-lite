# qc_failed 输出隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** qc 判定 ❌ 的瞬间把成品移入 `outdir/qc_failed/`——通过层永不见未验收件，人工复核后移出即转正。

**Architecture:** 两处 qc 判定各守一层——pipeline 模式在编排器 Python 侧（`_quarantine` + `finish_video` 可选 `output_path` 三控制面同步），single 模式在 blur.sh `qc_one` shell 侧（basename 守卫防二次质检重复移）。spec=[2026-09-25-qc-failed-quarantine-design](../specs/2026-09-25-qc-failed-quarantine-design.md)。

**Tech Stack:** Python 3.10+/pytest；bash 3.2；Go stdlib（gocp 单文件）。

## Global Constraints

- 依赖白名单：Python 仅 opencv-python/numpy/scipy/pytest；Go 仅 modernc.org/sqlite——不新增
- 生成结果不上传远端 = hard guarantee（outputs/ 整体 gitignore，qc_failed/ 在其下天然覆盖）
- commit 尾行 `Co-Authored-By: Claude Code <noreply@anthropic.com>`
- bash 3.2 兼容（无关联数组/无 ${var,,}）
- qc 不自动重跑哲学不变——隔离只移文件，不触发任何重试
- 文档精简（用户偏好：树+职责注释级别，不写多层抽象）
- 实验产物写 mktemp -d（严禁写 outputs/ 或 inputs/；测试用 tmp_path）

---

### Task 1: finish_video 可选 output_path——三控制面同步 + 契约双跑

**Files:**
- Modify: `src/faceblur_lite/state.py:217-221`（finish_video）
- Modify: `src/faceblur_lite/remote.py:78-79`（HttpControlPlane.finish_video）
- Modify: `gocp/main.go`（handleFinish）
- Test: `tests/test_contract.py`（追加）

**Interfaces:**
- Produces: `finish_video(video_id: str, status: str, output_path: str | None = None) -> None`
  （缺省 None=不动 output_path；三实现语义一致，COALESCE 式）。Task 2 的编排器
  以关键字参数调用 `cp.finish_video(vid, status, output_path=final_path)`。

- [ ] **Step 1: 写失败测试**（tests/test_contract.py 追加）

```python
def test_finish_video_output_path_sync(cp):
    """qc_failed 隔离（spec 2026-09-25 §3.1）：finish_video 可选改写
    output_path，缺省不动——sqlite/go 两控制面一致。"""
    vid = _mk(cp, n=1)
    cp.finish_video(vid, "qc_failed", output_path="out/qc_failed/o.mp4")
    assert cp.get_video(vid)["output_path"] == "out/qc_failed/o.mp4"
    cp.finish_video(vid, "qc_failed")          # 缺省=不动
    assert cp.get_video(vid)["output_path"] == "out/qc_failed/o.mp4"
```

- [ ] **Step 2: 跑红**

Run: `./venv/bin/pytest tests/test_contract.py::test_finish_video_output_path_sync -v`
Expected: FAIL（sqlite 分支 TypeError: unexpected keyword `output_path`；go 分支同）

- [ ] **Step 3: 实现**

`src/faceblur_lite/state.py` 的 finish_video 整体替换为：

```python
    def finish_video(self, video_id: str, status: str,
                     output_path: str | None = None) -> None:
        with _connect(self.db_path) as c:
            c.execute(
                "UPDATE videos SET status=?, finished_at=?, "
                "output_path=COALESCE(?, output_path) WHERE id=?",
                (status, time.time(), output_path, video_id))
```

`src/faceblur_lite/remote.py` 的 finish_video 整体替换为：

```python
    def finish_video(self, video_id: str, status: str,
                     output_path: str | None = None) -> None:
        body = {"status": status}
        if output_path is not None:
            body["output_path"] = output_path
        self._call("POST", f"/videos/{video_id}/finish", body)
```

`gocp/main.go` 的 handleFinish 整体替换为（OutputPath 用指针区分缺省；
database/sql 对 nil 指针自动转 NULL → COALESCE 不动）：

```go
func handleFinish(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Status     string  `json:"status"`
		OutputPath *string `json:"output_path"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if _, err := db.Exec(
		"UPDATE videos SET status=?, finished_at=?, "+
			"output_path=COALESCE(?, output_path) WHERE id=?",
		req.Status, nowF(), req.OutputPath, r.PathValue("id")); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"ok": true})
}
```

- [ ] **Step 4: 跑绿 + 回归**

Run: `./venv/bin/pytest tests/test_contract.py tests/test_go_cp.py -q`
Expected: 全 PASS（含既有参数化双跑；go_bin fixture 从源码重构建，Go 改动即生效）

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/state.py src/faceblur_lite/remote.py gocp/main.go tests/test_contract.py
git commit -m "feat(cp): finish_video 可选 output_path——sqlite/remote/gocp 三面同步 + 契约双跑"
```

---

### Task 2: 编排器隔离接线——_quarantine + qc ❌ 分支 + 容错

**Files:**
- Modify: `src/faceblur_lite/orchestrator.py`（加 `_quarantine`；重写 qc_wait 分支）
- Test: `tests/test_pipeline.py`（追加两个测试）

**Interfaces:**
- Consumes: Task 1 的 `finish_video(..., output_path=...)`
- Produces: 模块级 `_quarantine(out_path: str) -> str`（抛 OSError 由调用侧容错）；
  log 事件 `quarantine video=… to=<name>` / `quarantine_failed video=… error=…`；
  `PipelineResult.output_path` 指隔离后路径（JSON `output` 字段自动反映）。

- [ ] **Step 1: 写失败测试**（tests/test_pipeline.py 追加）

```python
def test_pipeline_quarantine_on_qc_failed(tmp_path, move_video, capsys):
    """qc ❌ → 成品移入 outdir/qc_failed/（spec 契约 1）五件套：
    原位消失、隔离位存在、result 路径、DB 路径、log 事件+stderr 行。"""
    out = str(tmp_path / "q.mp4")
    res = run_pipeline(move_video[0], out, str(tmp_path / "wd"),
                       workers=1, segment_s=3.0,
                       params=KernelParams(det_every=2, det_conf=1.1))
    assert res.status == "qc_failed"
    quarantined = tmp_path / "qc_failed" / "q.mp4"
    assert quarantined.is_file()
    assert not Path(out).exists()
    assert res.output_path == str(quarantined)
    cp = SqliteControlPlane(str(tmp_path / "wd" / "pipeline.db"))
    assert cp.get_video(res.video_id)["output_path"] == str(quarantined)
    log = (tmp_path / "wd" / "pipeline.log").read_text(encoding="utf-8")
    assert "quarantine video=" in log
    assert "qc_result video=" in log
    assert "已隔离 qc_failed/" in capsys.readouterr().err


def test_quarantine_rename_failure_tolerated(tmp_path, move_video,
                                             monkeypatch, capsys):
    """spec 契约 4：隔离失败不砸批次——状态仍 qc_failed、log 记
    quarantine_failed 事件、文件留原位（Worker 在子进程，不受父进程
    monkeypatch 影响）。"""
    def boom(self, target):
        raise OSError("simulated rename failure")

    monkeypatch.setattr("pathlib.Path.rename", boom)
    out = str(tmp_path / "q2.mp4")
    res = run_pipeline(move_video[0], out, str(tmp_path / "wd"),
                       workers=1, segment_s=3.0,
                       params=KernelParams(det_every=2, det_conf=1.1))
    assert res.status == "qc_failed"
    assert Path(out).is_file()                       # 留原位
    assert not (tmp_path / "qc_failed" / "q2.mp4").exists()
    log = (tmp_path / "wd" / "pipeline.log").read_text(encoding="utf-8")
    assert "quarantine_failed video=" in log
    assert "隔离失败" in capsys.readouterr().err
```

- [ ] **Step 2: 跑红**

Run: `./venv/bin/pytest tests/test_pipeline.py::test_pipeline_quarantine_on_qc_failed tests/test_pipeline.py::test_quarantine_rename_failure_tolerated -v`
Expected: FAIL（qc ❌ 后文件仍在原位 / 无 quarantine 事件）

- [ ] **Step 3: 实现**

orchestrator.py 模块级（`_fmt_dur` 之后）加：

```python
def _quarantine(out_path: str) -> str:
    """qc_failed 成品移入 outdir/qc_failed/（spec 2026-09-25：通过层
    不留未验收件，转正=人工移出）。OSError 由调用侧容错。"""
    src = Path(out_path)
    dst_dir = src.parent / "qc_failed"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    src.rename(dst)
    return str(dst)
```

qc 收尾的 `if video["status"] == "qc_wait":` 分支体整体替换为（注意：
事件序=qc_result 先、quarantine 后，spec §3.1 不变量）：

```python
        if video["status"] == "qc_wait":
            prog.line(f"qc {stem} …")
            rep = qc_video(video["output_path"], sample_fps=qc_sample_fps)
            status = "done" if rep.passed else "qc_failed"
            final_path = video["output_path"]
            suffix = ""
            q_err: str | None = None
            if not rep.passed:
                try:
                    final_path = _quarantine(final_path)
                    suffix = " → 已隔离 qc_failed/"
                except OSError as e:    # 隔离失败不砸批次——缺陷可见
                    q_err = str(e)
                    suffix = f" —— 隔离失败（{type(e).__name__}），文件留原位"
            cp.finish_video(vid, status, output_path=final_path)
            prog.event("qc_result", video=vid, sampled=rep.sampled_frames,
                       violations=len(rep.violations))
            if not rep.passed:
                if q_err is None:
                    prog.event("quarantine", video=vid,
                               to=Path(final_path).name)
                else:
                    prog.event("quarantine_failed", video=vid, error=q_err)
            prog.line(f"qc {stem} {'✅' if rep.passed else '❌'} "
                      f"{len(rep.violations)} 违规（抽 {rep.sampled_frames} 帧）"
                      f"{suffix}")
            n_done += rep.passed
            n_qcf += not rep.passed
            results.append(PipelineResult(
                vid, video["input_path"], final_path,
                status, rep, wall, nchunks))
```

（与旧版差异：`PipelineResult` 第三参 `video["output_path"]`→`final_path`；
原合并打印行拆出 suffix；其余逐字保留。）

- [ ] **Step 4: 跑绿 + 回归**

Run: `./venv/bin/pytest tests/test_pipeline.py tests/test_progress.py -q`
Expected: 全 PASS（既有 `test_pipeline_progress_console_and_log` 是 qc 通过
路径，suffix=""，行格式不变）

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/orchestrator.py tests/test_pipeline.py
git commit -m "feat(pipeline): qc_failed 成品隔离 outdir/qc_failed/——事件+容错+五件套回归"
```

---

### Task 3: blur.sh qc_one 隔离 + report_pipeline 双层解析

**Files:**
- Modify: `scripts/blur.sh`（qc_one ❌ 分支、report_pipeline 解析、usage --outdir 行）

**Interfaces:**
- Consumes: Task 2 后 pipeline 模式已把隔离件放 `outdir/qc_failed/`——本任务
  让 shell 侧（single 模式 qc 判定 + report 二次质检）行为一致且不重复移。
- Produces: qc_one ❌ → mv + `↳ 已隔离` 行；已在 qc_failed/ 目录内的文件跳过
  （basename 守卫，斜杠风格无关）。

- [ ] **Step 1: 实现**

qc_one 函数头的 `local tmp rc summary sampled viol` 行须追加 `qd`（新分支
引入的隔离目录变量；否则 bash 3.2 下泄漏为全局——功能可用但违背本函数
local 纪律）。

qc_one 的 `else`（❌）分支替换为：

```sh
  else
    echo "  ❌ $(basename "$1") qc 违规（抽 ${sampled} 帧，${viol} 处）"
    QC_BAD=1
    # qc_failed 隔离（spec 2026-09-25）：通过层不留未验收件。basename 守卫——
    # 文件已在 qc_failed/ 内（report_pipeline 二次质检复用本函数）则不重复移；
    # 隔离目录从 $1 自身 dirname 派生，对 OUTDIR 尾斜杠/相对绝对路径免疫。
    if [ "$(basename "$(dirname "$1")")" != "qc_failed" ]; then
      qd="$(dirname "$1")/qc_failed"
      mkdir -p "$qd"
      mv "$1" "$qd/"
      echo "  ↳ 已隔离：qc_failed/$(basename "$1")（复核后人工移出=转正）"
    fi
  fi
```

report_pipeline 的输出质检循环替换为（先通过层、缺则隔离层、再缺才未生成）：

```sh
  echo ""
  echo "── 输出质检 ──"
  local p f base
  for p in "${INPUTS[@]}"; do
    base="$(stem_of "$p")_blurred.mp4"
    f="$OUTDIR/$base"
    if [ ! -f "$f" ] && [ -f "$OUTDIR/qc_failed/$base" ]; then
      f="$OUTDIR/qc_failed/$base"
    fi
    qc_one "$f"
  done
```

usage 的 `--outdir DIR` 说明行（`输出目录（默认 outputs/；生成结果不进 git）`）
之后追加一行：

```sh
  #                 qc_failed 成品隔离在 $OUTDIR/qc_failed/（复核后人工移出=转正）
```
（对齐既有缩进风格；写在 --outdir 两行说明下方。）

- [ ] **Step 2: 验证**

Run: `bash -n scripts/blur.sh && ./venv/bin/pytest tests/test_blur_sh.py -q`
Expected: 语法 OK + 全 PASS（demo 场景全通过不触发隔离）

- [ ] **Step 3: Commit**

```bash
git add scripts/blur.sh
git commit -m "feat(blur.sh): single 模式 qc ❌ 隔离 + report 双层解析（basename 守卫）"
```

---

### Task 4: 文档同步

**Files:**
- Modify: `docs/pipeline-guide.md`（§6.5 qc_failed 行重写、§0.1 输出侧、§7.2 收尾、实操入口）
- Modify: `outputs/README.md`（**本地文件，不入库**——outputs/ 整体 gitignore，改完不进 commit）

- [ ] **Step 1: 实现**

§6.5 表第一行 `| qc 有违规 | …` 的处置列替换为：

```markdown
| qc 有违规 | 该视频终态 qc_failed（退出码 1），**成品移入 `outdir/qc_failed/`**（通过层不留未验收件）——不自动重跑（确定性系统原参数重试必然同样结果）。复核=按违规清单 t_sec 精确回放定性（真漏/半糊/误检），可接受则人工移出转正；调参重跑换新 workdir 后同名写通过层 |
```

§7.2 各环节机理的质检环节提一句（紧跟 `qc_wait → 编排器逐视频 qc（§6）` 相关行后）：
`qc ❌ → 成品隔离 qc_failed/（Python 侧 _quarantine；single 侧 blur.sh qc_one 同语义）`

实操入口 `qc` 子命令示例后补一行：

```bash
> ls outputs/<会话>/qc_failed/     # qc_failed 成品隔离区（复核后人工移出=转正）
```

outputs/README.md 成品目录约定表加一行：`| qc_failed/ | qc 未过成品的隔离区（复核现场；人工移出=转正） |`

- [ ] **Step 2: 验证**

Run: `grep -n "qc_failed" docs/pipeline-guide.md | head` 人工核对四处落点齐全

- [ ] **Step 3: Commit**（只提交 pipeline-guide）

```bash
git add docs/pipeline-guide.md
git commit -m "docs: qc_failed 隔离入指南（§6.5 处置流/§7.2/实操入口）"
```
