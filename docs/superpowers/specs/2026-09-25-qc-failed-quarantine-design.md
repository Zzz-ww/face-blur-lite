# qc_failed 输出隔离（quarantine）设计 v1

- **日期**：2026-09-25
- **状态**：待用户评审
- **背景**：发布前脱敏场景，qc_failed 成品与验收通过件在 outputs/ 同层混放，
  人工发布时有误拿风险。演示实证：`outputs/20260925-test1/real_video_blurred.mp4`
  （qc ❌ 20 违规）躺在通过层。用户已拍板两项：**子目录 `qc_failed/`** 形态
  ＋ **pipeline/single 双模式覆盖**。

## 1. 行为契约

1. **隔离时机**：qc 判定 ❌ 的瞬间，输出从 `outdir/<stem>_blurred.mp4`
   移到 `outdir/qc_failed/<同名>`，原位消失。通过层永不见未验收件
   （`cp outputs/*.mp4`、不带递归的 rsync 天然排除）。
2. **转正 = 人工移出**。无 promote 子命令（YAGNI）——人工介入本身是设计：
   qc 是信号，人是裁判。
3. **重跑语义**：重跑（须换新 workdir/会话目录，BUG-017）同名输出写通过层，
   与隔离区旧件命名空间分离、无碰撞；若重跑后仍 qc_failed，新隔离件
   **同名覆盖**旧隔离件（无版本管理——要保留 before 基线先手动移走）。
4. **隔离失败不砸批次**：rename 抛 OSError → 状态仍 qc_failed、stderr +
   `pipeline.log` 报 `quarantine_failed` 事件、文件留原位——缺陷可见而非静默。
5. 硬保证不变：`qc_failed/` 在 outputs/ 下整体 gitignore，生成物永不入库。

## 2. 隔离件用途（语义）

隔离件是**复核现场**，不是垃圾：

- 按 t_sec 回放定性（真漏 / 半糊 / 误检，§6.4 取证方法前提）；
- 人工验收门标的（可接受 → 移出转正；BUG-014 小脸残余在低清场景可放行）；
- 调参 before/after 对比基线。

生命周期：隔离 → 人工复核 → 转正 / 被重跑件覆盖 / 随会话目录整体删
（源片在 inputs/ 不受影响）。

## 3. 落点：两处 qc 判定各守一层

### 3.1 pipeline 模式（Python，orchestrator.py 收尾）

- 模块级 `_quarantine(out_path: str) -> str`：`mkdir qc_failed/` +
  `rename`，返回新路径；调用侧捕获 OSError 走容错分支。
- qc ❌ 分支：`final = _quarantine(video["output_path"])`；
  `cp.finish_video(vid, status, output_path=final)`；
  `PipelineResult.output_path = final`（JSON `output` 字段自动反映）。
- `finish_video` 加可选参数 `output_path: str | None = None`（缺省不动）：
  - [state.py](../../src/faceblur_lite/state.py) sqlite：
    `UPDATE videos SET status=?, finished_at=?, output_path=COALESCE(?, output_path) …`；
  - [remote.py](../../src/faceblur_lite/remote.py) `HttpControlPlane`：
    POST body 加可选字段；
  - [gocp/main.go](../../gocp/main.go) `handleFinish`：req 加
    `OutputPath *string`（指针区分缺省），UPDATE 加
    `output_path=COALESCE(?, output_path)`——两控制面行为一致（契约先行）。
- 事件：`prog.event("quarantine", video=…, to=…)`；stderr 行
  `qc <stem> ❌ N 违规（抽 M 帧）→ 已隔离 qc_failed/`。

### 3.2 single 模式 + 报告（shell，blur.sh）

single 子命令本身=纯处理无 qc 判定（qc/隔离在 blur.sh 编排层）：

- `qc_one` ❌ 分支：`mkdir -p "$OUTDIR/qc_failed"` + `mv`；**dirname 守卫**
  ——文件已在 qc_failed/ 内则跳过（`report_pipeline` 二次质检复用 qc_one
  时不重复移）；echo `↳ 已隔离：qc_failed/<name>`。
- `report_pipeline` 输入解析：先查通过层 → 缺则查隔离层 → 再缺才报"未生成"。
- `qc` 子命令保持纯检查器（无副作用）——隔离是编排层职责。
- bash 3.2 兼容；mv 同名覆盖语义即契约 3。

## 4. 测试

1. `test_pipeline_quarantine_on_qc_failed`：`run_pipeline(det_conf=1.1)`
   （强制零检测 → 真 qc_failed）五件套断言：原位不存在 / 隔离位存在 /
   `result.output_path` 新路径 / DB `get_video` 路径已更新 / log 含
   `quarantine` 事件 + stderr 含"已隔离"。
2. `test_quarantine_rename_failure_tolerated`：monkeypatch `Path.rename`
   抛 OSError → 仍 qc_failed、log 含 `quarantine_failed`、文件留原位。
3. 契约：`test_contract.py` 加 `finish_video` 可选 `output_path` 双跑
   （sqlite/http 参数化；现有 `go_cp_url` fixture 处理 go 缺失跳过）。
4. blur.sh：`bash -n` + selftest 回归（demo 全通过不触发隔离）；
   qc_one 守卫与 report_pipeline 解析的活体验证放对抗验证工作流。

## 5. 文档

- pipeline-guide §6.5 qc_failed 处置行重写（隔离 + 复核流 + 转正）、
  §0.1 输出侧、实操入口示例；
- outputs/README.md 目录约定加 `qc_failed/` 隔离层；
- blur.sh usage `--outdir` 说明提一句。

## 6. 非目标

- promote 子命令（转正=人工 mv）；
- 自动升级重跑（qc_failed → det1 escalation，与"不自动重跑"哲学冲突，未拍板）；
- 隔离区版本管理（同名覆盖）；
- resume（backlog #6）。
