# CLI 参考

`faceblur-lite` 共 **9 个子命令**，覆盖三种运行形态（single 单进程 / pipeline 编排 / worker 独立数据面）与四个独立算子。本文全部参数与默认值以 `src/faceblur_lite/cli.py` 为准。

```console
$ ./venv/bin/faceblur-lite --version
$ ./venv/bin/faceblur-lite <子命令> …
```

> 环境引导（venv、模型下载、输入校验）由 `scripts/blur.sh` 承担，见 how-to/run-batch.md。

## 退出码梯（全命令统一语义）

| 码 | 含义 | 说明 |
|---|---|---|
| 0 | 成功 | |
| 1 | 仅边缘违规，或批次存在失败件 | qc 只有 marginal；pipeline 有 failed/qc_failed |
| 2 | 环境/输入错误 | workdir 复用拒绝、worker 参数缺失、未知命令 |
| 3 | qc 含致命违规 | 编排层直接拦截 |

优先级 **2 > 3 > 1**（`scripts/blur.sh` 的 rc 链同语义）。未捕获异常由 Python 解释器以非零码退出，视同错误类。

| 子命令 | 可能退出码 |
|---|---|
| single | 0 |
| pipeline | 0 / 1 / 2（workdir 复用）/ 3（任一视频 qc 致命） |
| worker | 0 / 2（--cp-url 与 --db 均缺） |
| qc | 0 / 1 / 3 |
| demo-video | 0 / 1（缺资产） |
| split / merge / status / stats | 0 |

## single — 单进程内核

一个视频 → 一个脱敏视频（检测/跟踪/模糊/编码在一个进程内完成）。

```console
$ faceblur-lite single INPUT OUTPUT [--det-every 3] [--det-conf 0.3]
    [--margin 0.15] [--detector yunet|scrfd]
```

输出 `KernelReport` JSON（frames/detections/predicted_frames/track_count/segments/duration_s/wall_s）。

## pipeline — 批量分片并行

控制面 + N 个数据面 Worker，scatter-gather：切片 → 并行脱敏 → 原子 Join 合并 → 逐视频 qc。

```console
$ faceblur-lite pipeline INPUT… [--outdir outputs] [--workdir work-pipeline]
    [--workers 2] [--segment 30] [--det-every 3]
    [--detector yunet] [--qc-detector ?] [--cp-url ?]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--outdir` | `outputs` | 成品输出 `<stem>_blurred.mp4`；qc ❌ 件移入 `outdir/qc_failed/` |
| `--workdir` | `work-pipeline` | 一次性工件目录；sqlite 模式下已含历史记录则拒绝（rc 2） |
| `--workers` | 2 | Worker 进程数 |
| `--segment` | 30.0 | 切片秒数；时长 ≤ segment 的视频走快路径不切 |
| `--qc-detector` | 跟随 `--detector` | qc 复检检测器（交叉验收位：内核 yunet + 闸门 scrfd） |
| `--cp-url` | None | 如 `http://127.0.0.1:8080` 切 Go 控制面；默认 SQLite 内嵌 |

输出 `PipelineResult` JSON 列表（video_id/status/qc_passed/violations/chunk_count/wall_s/output/error）。**同 stem 输出碰撞**：后到者显式 failed，先到者正常处理，不静默重命名。

## worker — 独立数据面 Worker

```console
$ faceblur-lite worker (--cp-url URL | --db PATH) [--idle-exit-s 0]
    [--det-every 3] [--detector yunet]
```

- `--cp-url` 与 `--db` **二选一**（缺一 rc 2）：HTTP 控制面 / 直连 SQLite 库（WAL 跨进程安全）
- `--idle-exit-s 0`（默认）= 常驻永不退出（容器部署模式）；编排器内部起池用 3.0
- worker_id 形如 `w-{HOSTNAME|pid}`；认领→执行→事务回写循环，可随时 kill（lease 回收兜底）

## split / merge — 独立算子（DAG 节点模板用）

```console
$ faceblur-lite split INPUT OUTDIR [--segment 30]   # 打印切片路径列表（按 idx）
$ faceblur-lite merge OUTPUT CHUNK…                 # concat 流复制合并，打印成品路径
```

split：时长 ≤ segment 返回原文件不切；重跑同目录先清旧 chunk。merge：流复制失败降级重编码。

## qc — 输出域抽帧复检

```console
$ faceblur-lite qc VIDEO [--sample-fps 1.0|none|all] [--detector yunet]
```

- `--sample-fps none`（或 `all`）= 全密度逐帧复检（**最终验收用**）
- 输出：passed / sampled / fatal / marginal / violations（每条含 t_sec、conf、box、severity）
- 退出码 0/1/3 按致命/边缘分级

## demo-video / status / stats

```console
$ faceblur-lite demo-video OUTPUT [--scenario move|occlude] [--duration 6]  # 合成测试视频
$ faceblur-lite status DB          # 逐行打印 videos 表 JSONL
$ faceblur-lite stats DB [--json]  # 观测出口：排队/处理分位、吞吐、失败率
```

demo-video 需先跑 `scripts/download_assets.sh`（assets/lena.jpg），缺资产 rc 1。

---

**事实源锚点**：子命令与参数定义 [cli.py:29-98](../../src/faceblur_lite/cli.py#L29-L98)｜退出码 [cli.py:14-19](../../src/faceblur_lite/cli.py#L14-L19)、[cli.py:121-136](../../src/faceblur_lite/cli.py#L121-L136)｜碰撞隔离 [orchestrator.py:228-255](../../src/faceblur_lite/orchestrator.py#L228-L255)｜worker 参数裁决 [cli.py:138-154](../../src/faceblur_lite/cli.py#L138-L154)
