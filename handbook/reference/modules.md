# 模块地图

17 个 Python 模块 + 1 个 Go 服务 + 2 个脚本，合计约 3000 行。"职责即边界"——目录树注释即架构。

> 系统级结构全景图（控制面/数据面/观测/存储一图流）见 [../README.md](../README.md)。

## 目录树

```
face-blur-lite/
├─ src/faceblur_lite/
│  ├─ cli.py                 入口：9 子命令分发（参数/退出码的唯一权威）
│  │
│  ├─ 【控制面】
│  │  ├─ control.py          ControlPlane 协议——契约代码面（仅 29 行）
│  │  ├─ state.py            SqliteControlPlane：SQLite WAL 参考实现（默认）
│  │  └─ remote.py           HttpControlPlane：gocp 的 urllib 适配器（--cp-url）
│  │
│  ├─ 【编排（组装根）】
│  │  ├─ orchestrator.py     批量编排：切片提交→Worker 池→lease 巡检→qc→隔离
│  │  └─ progress.py         进度双出口：stderr 粗粒度 + pipeline.log 全量事件
│  │
│  ├─ 【数据面 Worker】
│  │  └─ worker.py           无状态认领循环（只依赖协议；原子 Join 触发合并）
│  │
│  ├─ 【算子（纯函数，文件→文件）】
│  │  ├─ kernel.py           内核：检测×跟踪×模糊逐帧合成（single 即它）
│  │  ├─ splitter.py         关键帧流复制切片（≤segment 快路径不切）
│  │  ├─ merger.py           concat 流复制合并（失败降级重编码）
│  │  └─ qc.py               输出域抽帧复检（分级+面积过滤）
│  │
│  ├─ 【内核部件】
│  │  ├─ detector.py         Detector 协议 + YuNet/SCRFD 实现 + 工厂
│  │  ├─ tracker.py          mini-SORT：恒速预测 + 三轮关联
│  │  └─ blur.py             框外扩 + 高斯模糊（大脸降采样快路径）
│  │
│  ├─ 【基础设施】
│  │  └─ ffmpeg_io.py        probe / FFmpegReader / FFmpegWriter（rawvideo pipe）
│  │
│  ├─ 【观测（只读旁路）】
│  │  └─ stats.py            排队/处理分位、吞吐、失败率（mode=ro 直读库）
│  │
│  └─ 【测试基建】
│     └─ testmedia.py        合成视频生成（move / occlude 场景）
│
├─ gocp/main.go              Go 控制面服务：同契约的 HTTP 形态（纯 Go 无 CGO）
├─ scripts/blur.sh           本地一键运行：环境引导/聚合报告/退出码梯
└─ scripts/download_assets.sh  模型+素材下载（sha256 pin）
```

## 依赖铁律（从 import 关系直接可验）

1. **Worker 只依赖协议**：`worker.py` 仅 import `control.ControlPlane`，实现经依赖注入——换 SQLite/HTTP/未来 Redis 控制面，worker.py 零改动
2. **orchestrator 是组装根**：由它选择控制面实现（sqlite / remote）注入 Worker 池与 qc；它也是唯一同时触碰"控制面实现 + 数据面"的模块
3. **算子纯函数**：kernel / splitter / merger / qc 不感知控制面与进程模型（文件→文件），因此可独立 CLI 调用、可上 DAG 引擎当节点模板
4. **观测不进契约**：stats / progress 以 `mode=ro` 只读旁路直读 SQLite（WAL〔Write-Ahead Logging，预写日志〕并发读安全），不往 ControlPlane 加读方法
5. **解码归 ffmpeg，检测归 ONNX（Open Neural Network Exchange），中间只有 numpy**：格式交接面全部显式（rawvideo BGR24 pipe ↔ `(H,W,3)` uint8）

## 模块 API 一表流

| 模块 | 公开 API | 一句话 |
|---|---|---|
| cli | `main(argv)` | 9 子命令分发与退出码 |
| control | `ControlPlane` | 12 方法协议 |
| state | `SqliteControlPlane` | SQLite WAL 实现（BEGIN IMMEDIATE） |
| remote | `HttpControlPlane(base_url, timeout_s=60)` | gocp 的 HTTP 适配 |
| orchestrator | `run_batch` `run_pipeline` `PipelineResult` `WorkdirReuseError` | 批量编排与失败隔离 |
| progress | `ProgressReporter` `progress_snapshot` `emit_chunk_events` | 双出口进度 |
| worker | `worker_loop(cp, worker_id, …)` | 认领→执行→回写循环 |
| kernel | `KernelParams` `process_video` `KernelReport` | 内核主循环 |
| splitter | `split_video(input, out_dir, segment_s=30)` | 流复制切片 |
| merger | `merge_chunks(paths, output)` | 流复制合并 |
| qc | `qc_video` `QCReport` `Violation` `severity_for` | 输出域复检 |
| detector | `Detector` `YuNetDetector` `SCRFDDetector` `make_detector` `Det` | 检测抽象与双实现 |
| tracker | `Tracker` `Track` `associate` `iou_matrix` | mini-SORT |
| blur | `blur_regions` `expand_box` | 区域模糊 |
| ffmpeg_io | `probe` `FFmpegReader` `FFmpegWriter` `VideoMeta` `sample_stride_for` | 流式 IO |
| stats | `collect_stats` `format_stats` | 观测出口 |
| testmedia | `build_video` `load_face` | 合成素材 |
| gocp | `gocp <db> <addr>` | Go 控制面（12 端点，见 contract.md） |

## 数据面如何消费控制面（调用链）

```
cli pipeline ─▶ orchestrator.run_batch
                 ├─ split_video（切片）─▶ cp.submit_video
                 ├─ mp.Process × N ─▶ worker_loop(cp, …)      # cp 经 pickle 传子进程
                 │                      ├─ cp.claim_chunk ─▶ process_video（kernel）
                 │                      ├─ cp.complete_chunk ─┬─ false: 继续 claim
                 │                      │                     └─ true: 原子 Join 认领
                 │                      │                        merge_chunks + cp.complete_merge
                 │                      └─ 异常 ─▶ cp.fail_chunk
                 ├─ 巡检: cp.reclaim_expired + progress_snapshot（ro 旁路）
                 └─ drain 后逐视频 qc_video ─▶ cp.finish_video（❌ 则 _quarantine 隔离）
```

---

**事实源锚点**：Worker 依赖注入 [worker.py:1-12](../../src/faceblur_lite/worker.py#L1-L12)｜组装根选实现 [orchestrator.py:82-96](../../src/faceblur_lite/orchestrator.py#L82-L96)｜观测只读旁路 [stats.py:22-32](../../src/faceblur_lite/stats.py#L22-L32)、[progress.py:37-64](../../src/faceblur_lite/progress.py#L37-L64)｜原子 Join 调用链 [worker.py:33-43](../../src/faceblur_lite/worker.py#L33-L43)｜模块行数以仓库实测为准（`wc -l src/faceblur_lite/*.py gocp/*.go`）
