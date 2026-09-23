# face-blur-lite MVP 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **本文档是"持续细化"的活方案**：需求结论与设计取舍在 Context 区，执行任务在 Task 区。细化 = 直接改这份文档。
>
> **配套设计文档**：`docs/design.md`（四大视图详细设计，对外交付版）——母本为本方案 Context 区"详细设计（四大视图）"，两处同步改。

**Goal:** 从 0-1 构建轻量视频人脸脱敏管线：输入一段视频（demo 5 分钟、架构支持长视频），输出人脸全部模糊的新视频；显式区分控制面/数据面；本地单机闭环 + 业界平台（百度胜算/火山 LAS/百舸）映射文档。

**Architecture:** 数据面 = 纯函数算子内核（ffmpeg 流式解码 → 隔帧 YuNet 检测 → mini-SORT 跟踪补帧/遮挡外推 → 外扩高斯模糊 → 流式编码）+ 切片/合并/质检算子；控制面 = SQLite(WAL) 两级状态机（videos/chunks）+ 调度函数库（claim / lease 回收 / 原子 Join）+ 主进程编排器。长视频走 scatter-gather：关键帧流复制切片 → N 个无状态 Worker 进程并行 → 最后完成者原子认领合并 → 编排器跑质检收尾。控制面**双形态**：默认 SQLite 内嵌（零额外进程，最轻）；`--cp-url` 切换 **Go 控制面服务**（`gocp/` 单二进制 + `remote.py` HTTP 适配）——复刻业界分工"平台控制面 Go + 算法数据面 Python"，两种实现过同一份契约测试，数据面零改动。

**Tech Stack:** Python ≥3.10，opencv-python（含 FaceDetectorYN/YuNet），numpy，scipy（匈牙利匹配），sqlite3（标准库），系统 ffmpeg/ffprobe。**不引入** Redis/Docker/任何云服务（MVP）。

## Global Constraints

- 项目根：`/Users/zhujiawei/Desktop/zte/backup/workbackup-main/face-blur-lite/`，独立 git 仓库（父目录不是 git 仓库，Task 0 里 `git init`）
- 依赖白名单：`opencv-python`、`numpy`、`scipy`、`pytest`（dev）。禁止引入其他运行时依赖
- Go 侧（仅 Task 12）：Go ≥1.22，唯一外部依赖 `modernc.org/sqlite`（纯 Go 无 CGO，`go build` 即出静态单二进制）。Python 侧不加任何依赖（HTTP 客户端用 stdlib `urllib`）。默认路径零 Go 依赖——没有 go 工具链时 Go 模式测试自动跳过，其余功能不受影响
- 系统依赖：ffmpeg/ffprobe 必须在 PATH（兜底：`brew install ffmpeg`）
- **控制面/数据面边界（契约先行）**：`control.py`（协议）+ `state.py`（SQLite 参考实现）+ `remote.py`（HTTP 适配）+ `gocp/`（Go 服务实现）+ `orchestrator.py` 是控制面；`kernel.py`/`splitter.py`/`merger.py`/`worker.py`/`qc.py` 及其依赖库是数据面。**数据面只准 import `control.py` 协议，不准 import 任何实现（state.py/remote.py）**；数据面不得绕过控制面改任务状态；控制面不得包含任何媒体处理逻辑。契约规范 `docs/control-plane-contract.md` 是跨语言事实标准，任何实现（Python SQLite / Go HTTP）必须通过 `tests/test_contract.py` 的同等场景（参数化双跑）
- 编码参数全局钉死：`libx264 / preset veryfast / crf 20 / pix_fmt yuv420p / movflags +faststart`；有音轨时 `-c:a copy` 绝不重编（concat 流复制合并的前提）
- **阈值分域声明**（上一项目的硬教训，必须遵守）：内核检测阈值 `det_conf=0.3` 作用在**源视频信号域**；质检阈值 `pass_conf=0.5` 作用在**重编码后的输出域**。两者不可比较、不可复用（有损重编码会改变置信度）
- 质检判定：输出域可检出（conf≥pass_conf 且面积过滤通过）即违规；MVP 不做包含率判定（记为细化项）
- qc_failed **不自动重跑**（确定性系统原参数重试无效），只报告违规帧清单；升级重处理闭环记为细化项
- 模型资产不入库：`models/*.onnx`、`assets/*.jpg` 由 `scripts/download_assets.sh` 下载并 gitignore；依赖资产的测试无资产时跳过，核心单测（tracker/blur/state/ffmpeg_io）不依赖资产
- 每个任务：TDD（先写失败测试 → 最小实现 → 通过）→ commit。commit 尾注 `Co-Authored-By: Claude Code <noreply@anthropic.com>`
- 所有命令在项目根 `face-blur-lite/` 下执行；Python 统一用 `./venv/bin/python`，测试 `./venv/bin/python -m pytest`

## Context（需求结论与设计取舍——"持续细化"改这里）

### 需求清单（场景原题 → 设计落点）

| # | 要求 | 方案落点 | 任务 |
|---|---|---|---|
| R1 | 一段视频（5min demo）→ 人脸全模糊 → 输出新视频 | `kernel.py` 单文件内核：流式解码→隔帧检测→跟踪补帧→外扩模糊→流式编码 | Task 5 |
| R2 | 手臂遮挡 | 跟踪器预测外推（遮挡期检测丢了、跟踪还在）+ max_age 容忍剪枝 + 低分框二轮回收（ByteTrack 思路：遮挡期置信度塌落时不断链） | Task 4/6 |
| R3 | 长视频 | 流式 IO 内存 O(1) + scatter-gather：关键帧流复制切片 → 多 Worker 并行 → concat 合并 | Task 8/10 |
| R4 | 以什么格式输入到模型 | ffmpeg `-f rawvideo -pix_fmt bgr24` pipe → `(H,W,3) BGR uint8` ndarray → 检测器内部预处理；音轨从原片流复制 | Task 1/3 |
| R5 | 主流模型选型 | YuNet（1MB，MVP 起步）；`Detector` 协议抽象，SCRFD 为升级位（换模型不改主流程） | Task 3 |
| R6 | 预测下一帧出现的坐标 | `Tracker.predict()`：恒速运动模型外推一帧——显式、可单测、可演示 | Task 4 |
| R7 | 控制面/数据面区分 + **控制面转 Go** | 契约先行：`control.py` 协议 + 契约文档 + 契约测试；**Go 实现已落地**（Task 12）：`gocp/` 单二进制 HTTP 服务 + `remote.py` 适配器，与 SQLite 实现过同一份契约测试（参数化双跑），数据面零改动；默认仍 SQLite 内嵌（最轻） | Task 7/10/12 |
| R8 | 业界工作流平台了解（百度胜算/火山 LAS/百舸工作流） | `docs/platform-mapping.md`：组件逐概念对照 + 控制面/数据面演进 + 具身数据链路定位 | Task 11 |
| R9 | 怎么证明没漏 | `qc.py`：输出域 1fps 抽帧复检，检出未脱敏人脸即违规 | Task 9 |
| R10 | 小 demo 支持一次处理多个视频 | `run_batch`：逐视频切片+提交（**失败隔离**）→ 共享 Worker 池消化交错 chunk → 逐视频合并+质检；Worker 零改动（队列模型免费获得两级并发） | Task 10 |
| R11 | **大量视频（可大可小）**批量吞吐 | 库即队列：提交廉价（切片=流复制秒级）可增量入库；大视频 scatter-gather、小视频快路径，同池交错；吞吐瓶颈在数据面→横向加 Worker 即可（控制面低频事务撑得住） | Task 8/10 |
| R12 | **上云容器编排**可轻易迁移 | Worker 独立入口（常驻模式 `idle_exit_s=0`）+ gocp 已是独立服务 + 路径契约配共享文件系统=算子零改动；迁移清单见"演进就绪设计"节 | Task 11/12 |
| R13 | **手写 DAG → 通用 DAG 引擎**直接切换 | 算子全部 CLI 子命令化（split/single/merge/qc——引擎节点模板原样引用）；原子 Join→fan-in 依赖边、attempt→引擎重试策略；切换=换调度层、算子层 100% 保留 | Task 11 |
| R14 | **观测出口**（排队/吞吐/失败率） | `stats` 命令**只读旁路**：时间戳/attempt 已入库 → 零 schema 变更；不进 ControlPlane 契约（观测读不污染任务状态契约）；血缘/时序化留开放细化项 9 | Task 11 |

### 控制面/数据面（本项目第一架构决策）

| | 控制面 | 数据面 |
|---|---|---|
| 职责 | 状态机、任务派发、lease 回收、原子 Join、编排（split→并行→merge→qc） | 真正干活：解码、检测、跟踪、模糊、编码、切片、合并、质检 |
| 实现形式（轻量版） | `control.py` 协议 + `state.py` 参考实现（SQLite WAL）+ `orchestrator.py` 编排器，主进程内嵌，**不是独立服务** | 无状态 Worker 进程（`multiprocessing` 拉起）+ 纯函数算子（kernel/splitter/merger/qc），可随时 kill；只依赖 `ControlPlane` 协议 |
| 交互契约 | `chunks` 表一行 = 一个任务（input/output/lease/attempt） | Worker：认领 → 执行算子 → 事务回写 |
| 关键性质 | 所有状态只在这里；SQLite 事务保证 claim 互斥与 Join 原子性 | 零状态，任意杀/扩——lease 与"最后完成者认领合并"因此成立 |
| 演进形态 | SQLite→Redis/MySQL；函数库→平台工作流引擎（百舸/LAS） | 子进程→Docker 任务容器（百舸"任务式"作业） |

#### 语言选型 ADR：控制面 MVP 用 Python，生产演进位是 Go（2026-09-23 细化）

**结论：控制面可以用 Go 实现（业界主流——K8s 全家桶、多数调度平台控制面都是 Go）；MVP 选 Python 是复杂度预算问题，不是能力问题。**

MVP 不拆语言的三个理由：

1. **轻量约束的直接推论**：单一 venv/依赖白名单/无外部服务。引入 Go = 双工具链 + 双构建 + 跨语言契约。控制面现仅约 300 行 Python，第二语言换来的收益为零。
2. **跨语言边界会改变控制面形态**：(a) 共享 SQLite → schema 与事务语义（claim 互斥/原子 Join/lease 判定）成为双语言逐位同步的契约，持续成本；(b) Go 起服务 + Worker 调 API → 控制面从"内嵌函数库"变"常驻服务"（生命周期/端口/健康检查/启动顺序），违背 MVP"非服务"决策——这是形态升级不是语言升级。
3. **负载画像用不到 Go 长处**：控制面是低频事务（个位数 Worker，QPS 个位），耗时在 SQLite 的 C 代码；数据面瓶颈在 ONNX 推理。Go 的优势（海量并发连接/低尾延迟/静态二进制）在此规模全部落空。

**Go 化触发条件**（满足其一即到切割点）：Worker 上到几十上百，心跳/续约/优先级调度变成高频网络问题；控制面长出 API/UI/多租户（变常驻服务的那一刻）；多机部署要静态二进制分发。业界分工参照：平台控制面 Go/Java 系，数据面算法容器 Python——百舸/LAS 上的作业镜像就是 Python 的地盘。

**本方案已留缝**：控制面/数据面的模块边界就是未来 Go 重写的切割线；任务 schema（chunks 表）是两边的中立契约。演进：同语言同进程（模块边界）→ 控制面 Go 服务 + 数据面 Python 容器（进程边界）→ 全托管平台（语言边界消失）。

**细化确认（2026-09-23，二次）**：控制面 Go 化从"触发条件后启动"**提前进 MVP**——原因：作业要求"实际上手业界形态的工作流 demo"，而**平台控制面 Go + 算法数据面 Python 正是业界标准分工**（K8s/Temporal/Argo 控制面皆 Go，算法容器皆 Python），demo 复刻这个形态本身就是要展示的认知。落地方式守住轻量约束：`gocp/` 单文件单二进制（stdlib net/http + 唯一依赖 modernc.org/sqlite，无 CGO/无框架/无 gRPC/无容器）；**默认路径仍是 SQLite 内嵌零额外进程**，`--cp-url` 显式切换才走 Go 服务；两实现过同一份契约测试（`tests/test_contract.py` 参数化双跑）。上文三个"不拆语言理由"对纯本地最小用法依然成立——本项目用**双形态并存**同时满足两边：要最轻有内嵌，要业界形态有 Go。

### 详细设计（四大视图）

> 核心设计叙述：**数据怎么流（视图1）→ 模型怎么选（视图2）→ 工作流怎么编排（视图3）→ 两个平面暴露什么算子（视图4）**。每节末尾标注与任务区的对应。对外交付文档 `docs/design.md`（Task 11）从本节提炼。
>
> **图示母本在 `docs/design.md`（Mermaid 渲染版，共 11 图）**：架构图 / 端到端数据流 / 模型输入链路 / DAG / 状态机×2 / 原子 Join 时序 / kill-9 恢复时序 / 契约类图＋数据面类图 / 演进路线图。两处内容不一致时以 design.md 为准。

#### 视图 1：数据处理管线设计（数据怎么流）

**端到端数据流**：

```
输入视频(.mp4)
  │ probe：分辨率/fps/时长/音轨
  ▼
[Split 切片] ──时长≤segment_s──► 快路径（不切，整片=1个chunk）
  │ 关键帧流复制 -c copy（秒级，不付编码成本）
  ▼
chunk_000.mp4 … chunk_N.mp4          ← 每个 chunk = 控制面一个任务(pending)
  │ 无状态 Worker 认领（N 个并行）
  ▼
[Blur 内核] ×N
  │ ffmpeg rawvideo pipe 解码 → (H,W,3) BGR uint8 帧
  │ 隔帧 YuNet 检测 → mini-SORT 跟踪补帧/遮挡外推
  │ → 框外扩高斯模糊 → pipe 编码（参数全局钉死）＋音轨 -c:a copy
  ▼
blurred_000.mp4 … blurred_N.mp4
  │ 全部 done → 原子 Join（最后完成者认领合并权）
  ▼
[Merge 合并] concat -c copy（同参数切片→秒级流复制；失败降级重编码）
  ▼
merged.mp4 → [QC 质检] 输出域 1fps 抽帧复检（pass_conf=0.5＋面积过滤）
  ▼
<stem>_blurred.mp4（通过=done；违规=qc_failed，只报告清单不自动重跑）
```

**阶段表**（唯一付代价的段是 Blur，其余全部流复制/抽样）：

| 阶段 | 算子 | 输入→输出 | 代价语义 | 失败语义 |
|---|---|---|---|---|
| 切片 | `splitter.split_video` | 文件→文件列表 | 流复制秒级，零编码 | 提交期失败→该视频 failed（批次隔离，其余视频继续） |
| 脱敏 | `kernel.process_video` | chunk→脱敏 chunk | **唯一编码+推理段** | attempt≤3 重试，耗尽→视频 failed |
| 合并 | `merger.merge_chunks` | 列表→完整视频 | 同参数→流复制秒级 | 降级重编码合并 |
| 质检 | `qc.qc_video` | 输出视频→报告 | 1fps 抽帧，成本可控 | 违规→qc_failed 报告违规帧清单 |

**关键设计点**：
- **内存 O(1)**：全程 pipe 流式，任意长视频不整载内存（R3 长视频的地基）
- **编码参数全局钉死**是 concat 流复制合并的前提——"合并零成本"是这个决定的回报
- **阈值分域**：内核 det_conf=0.3（源域）≠ qc pass_conf=0.5（重编码后输出域）——置信度不跨编码域守恒（老项目硬教训）
- **多视频批量**：N 个视频的 chunk 进同一队列、共享 Worker 池交错消费——两级并发（大视频多片并行＋多视频交错）由队列模型免费获得

任务对应：Task 1（流式 IO）/ Task 5（内核）/ Task 8（切片合并）/ Task 9（质检）/ Task 10（并行编排）。

#### 视图 2：模型选择（用什么、为什么、怎么喂）

**选型标准**：CPU 可跑（demo 无 GPU）、许可宽松、体积小、对"糊脸"够用——脱敏要 **recall 不要 precision（宁可多糊）**。

**候选对比**：

| 候选 | 体积 | CPU | 精度 | 结论 |
|---|---|---|---|---|
| OpenCV YuNet（FaceDetectorYN） | ~1MB | 快 | 正/侧脸够用，遮挡一般 | ✅ MVP 选定 |
| InsightFace SCRFD | 数 MB 级 | 中 | 遮挡/小脸 recall 更强 | 升级位（协议已留） |
| 云端人脸 API | — | — | 高 | ❌ 隐私悖论（未脱敏人脸出域违背脱敏初衷）＋万帧级调用成本 |

**决策**：MVP 用 YuNet，检测藏在 `Detector` 协议后（`detect(frame_bgr) -> list[Det]`）；SCRFD 是声明式升级——换实现类，管线零改动。预处理（缩放/归一化）在检测器内部完成（`setInputSize`），管线只递原始 BGR 帧。

**模型输入格式链路（R4——模型吃帧 tensor，不吃视频文件）**：

```
.mp4 容器 --ffmpeg -f rawvideo -pix_fmt bgr24 pipe--> (H, W, 3) BGR uint8 ndarray
    --> cv2.FaceDetectorYN.setInputSize((w,h))：检测器内部完成缩放/归一化
    --> 输出 (N,15) [x,y,w,h,...,score] --> Det(x1,y1,x2,y2,conf)（像素坐标，原分辨率）
```

解码归 ffmpeg，检测归 ONNX，中间只有 numpy；输出侧对称——处理帧 pipe 给 ffmpeg 编码，音轨 `-map 1:a -c:a copy` 从原片复制。

**推理成本策略**：隔帧检测（det_every=3）＋跟踪补帧 = 推理量降 2/3；低分框二轮回收（ByteTrack 思路）保证遮挡期不断链。**质检复用同款检测器、不同阈值域**——一套模型资产两处用。

任务对应：Task 3（Detector 协议+YuNet）/ Task 4（跟踪=下一帧预测 R6＋遮挡外推 R2）。

#### 视图 3：工作流的编排（任务怎么组织、调度、容错）

**DAG 形态**——split→blur×N→merge→qc 就是所有工作流引擎的 fan-out/fan-in 最小子图：

```
        ┌─ blur(chunk_0) ─┐
split ──┼─ blur(chunk_1) ─┼── merge ── qc ── done
        └─ blur(chunk_N) ─┘
```

**任务模型**：两级——`videos`（processing→merging→qc_wait→done|qc_failed|failed）＋ `chunks`（pending→running→done|failed）；`chunks` 表一行=一个任务（input/output/lease/attempt）。完整规范见 `docs/control-plane-contract.md`。

**调度语义**（每个控制面操作=一个事务）：
- **claim 互斥**：`BEGIN IMMEDIATE` 事务内"取最早 pending→置 running＋lease"，多 Worker 无冲突
- **原子 Join**：complete_chunk 事务内置 done 并判断"全 done→认领 merge"——并发下恰好一个 Worker 拿到合并权
- **lease 回收**：TTL 过期的 running→pending 重派（attempt+1），耗尽→视频 failed——Worker 暴死的兜底
- **失败隔离**：提交期 per-video try/except＋巡检不 fast-fail——单视频失败不终止批次

**执行拓扑**：编排器（控制面）逐视频 split＋submit → 拉起共享 Worker 池（无状态进程，消化全部视频的 chunk）→ 巡检 lease 回收 → drain 后逐视频 qc 收尾。Worker 不感知视频边界。

**控制面双形态**：默认 SQLite 内嵌（同进程，最轻）；`--cp-url` 切 Go 服务 `gocp`（HTTP API）——业界分工"平台控制面 Go＋算法容器 Python"，契约测试参数化双跑保证行为一致。上演化平台后，状态机/队列/Join/lease 全部被工作流引擎收编（对照见 `docs/platform-mapping.md` 与下方审查表）。

任务对应：Task 7（契约）/ Task 10（编排+批量）/ Task 12（Go 形态）。

#### 视图 4：数据面/控制面算子设计（暴露什么、契约是什么）

两平面职责总表见上"控制面/数据面"节；本视图回答**每个平面暴露哪些算子、什么契约**。

**算子清单**：

| 算子 | 平面 | 接口 | 性质 |
|---|---|---|---|
| `process_video` | 数据面 | `(in, out, params) -> KernelReport` | 纯函数；参数钉死→确定性；同入同出→幂等 |
| `split_video` | 数据面 | `(in, dir, segment_s) -> [paths]` | 纯函数、幂等 |
| `merge_chunks` | 数据面 | `([paths], out) -> path` | 纯函数、幂等（含降级路径） |
| `qc_video` | 数据面 | `(path, sample_fps, pass_conf) -> QCReport` | 纯函数、只读 |
| `submit/claim/complete/fail/reclaim/merge/finish` | 控制面 | `ControlPlane` 协议 11 方法 | 每方法一个事务；任务状态的唯一写入口 |
| `worker_loop` | 数据面 | `(cp: ControlPlane, worker_id, params) -> stats` | 通用执行循环：认领→执行算子→回写；只依赖协议 |

**跨平面契约（纪律）**：
- 数据面算子=**文件→文件纯函数**：无全局状态、可重入、失败留半成品无害（幂等重跑覆盖）
- 控制面"算子"=**状态迁移操作**：所有状态变更唯一入口，事务保证互斥/原子性——不变量见契约文档 §4
- 数据面只 import `control.py` 协议、不 import 任何实现——换 SQLite/Go/Redis 实现数据面零改动
- Worker 目前直接绑定 face-blur 内核（轻量取舍）；**算子通用化**（任务声明算子名＋注册表、Worker 变通用执行器）为演进项——加新算子（车牌/人体脱敏）时控制面不动

任务对应：Task 5/8/9（数据面算子）/ Task 7（控制面协议）/ Task 10（Worker）。

### 设计决策 × 业界通用工作流对照（2026-09-23 全方案审查）

逐项把本项目设计对到业界通用形态（通用引擎/平台与中间件的公共模式），判定分三档：✅ 同构、⚠️ 合法简化（差距记开放细化项）、❌ 缺口（本项目无）。

| # | 本项目设计 | 业界通用形态 | 判定 |
|---|---|---|---|
| 1 | 两级任务 video→chunk + 状态机 | 工作流引擎的任务/DAG 模型（Airflow Task、Argo DAG、Temporal Workflow）；专用媒体管线（MediaConvert 等）用固定拓扑 | ✅ 专用管线固定拓扑是主流，任意 DAG 是引擎的卖点而非用户的需要 |
| 2 | 控制面/数据面分离 | K8s（API Server vs kubelet）、SDN（控制器/转发面）、一切工作流引擎 | ✅ 标准分层 |
| 3 | 控制面 Go 服务 + 数据面 Python 算子（Task 12） | 平台控制面 Go/Java 系，算法容器 Python——K8s/Temporal/Argo/Kubeflow 全是这个分工 | ✅ 业界分工原样复刻 |
| 4 | 事务化调度语义（claim 互斥 / 原子 Join / lease 回收） | 引擎用 etcd/DB 事务；SQS visibility timeout；Redis Lua 原子脚本——语义相同，存储可换件 | ✅ 同构；`BEGIN IMMEDIATE` ≈ 单写者串行化 |
| 5 | scatter-gather 切片并行 | MapReduce fan-out/fan-in；媒体管线同形态（Netflix/MediaConvert） | ✅ |
| 6 | 关键帧流复制切片 + concat 合并（只有处理段编码） | 有损媒体处理管线的标准成本做法 | ✅ |
| 7 | 无状态 Worker + at-least-once + 幂等输出 | 任务队列标准可靠性模型（Celery/SQS/Temporal） | ✅ |
| 8 | lease TTL 无心跳 | SQS 同款（无心跳、超时重派）；K8s Lease/Temporal Activity 带心跳续约 | ⚠️ 简化合法；心跳续约记细化项 |
| 9 | attempt 上限 + qc_failed 不自动重跑 | 重试预算 + 死信队列（DLQ）+ 人工介入 | ✅ 语义对齐；DLQ 形态记细化项 |
| 10 | 质检抽帧复检闸门 + 阈值分域 | 数据验证闸门（Great Expectations/Deequ/TFDV）；"阈值声明信号域"是跨编码域数据管的通则 | ✅ 抽样复检=成本折中的标准解 |
| 11 | 均匀 1fps 抽样 | 生产质检分层抽样（风险区必查） | ⚠️ 分片边界必查记细化项（跟踪重置点=漏检风险最高） |
| 12 | stats 命令：排队/处理 P50/P95、吞吐、失败率（只读旁路） | 平台标配可观测（Prometheus/大盘的前置） | ✅ 最小出口进 MVP（Task 11，R14）；时序库/大盘/告警留平台期 |
| 13 | 无血缘记录 | 数据治理标配：输入→算子版本+参数→输出 | ⚠️ 记细化项（老项目已验证最小方案） |
| 14 | 单机多进程 Worker | 容器编排（Compose `--scale` → K8s Job/百舸任务式） | ✅ 形态兼容：Worker 无状态，换壳即可 |

**审查结论**：骨架（分层/任务模型/调度语义/容错/质检）与业界同构，无 ❌ 级缺口；差距集中在"平台配套"（心跳、DLQ、血缘、分层质检、容器化）——观测出口已以最小 stats 命令收口（Task 11，R14）。这正是"轻量 demo → 业界平台"的距离清单，也是 `docs/platform-mapping.md` 的价值所在。其余不进 MVP，逐条记入开放细化项。

### 演进就绪设计（R11-R13：大量视频 / 容器编排 / 通用 DAG，2026-09-24）

> 三个演进诉求的设计回答。共同原则：**迁移时换的是调度层，保留的是算子层**——这正是控制面/数据面分离（ADR-1）+ 契约先行（ADR-2）的回报。

#### ① 大量视频（可大可小）吞吐（R11）

- **库即队列**：`submit_video` 廉价（切片=流复制秒级），千级视频可一次性或分批**增量入库**到同一个 pipeline.db，共享 Worker 池 drain——不需要一次喂完
- **大小混合零特判**：大视频切片并行、小视频快路径单任务，同队列交错消费；小视频开销≈一个任务
- **扩容方向明确**：吞吐瓶颈在数据面推理（CPU/GPU）→ 横向加 Worker 进程/容器近线性扩展；控制面是低频事务（QPS 个位数），单实例撑得住——分层设计让"加机器"只发生在需要的地方
- 已知限制：重复提交不去重（幂等提交键 = input+params hash，记开放细化项 12）

#### ② 上云容器编排迁移清单（R12）

| 步骤 | 迁移动作 | MVP 已预留 |
|---|---|---|
| 1 | 控制面：gocp 静态单二进制部署到云主机/容器 | ✅ 已是独立 HTTP 服务，跨机就绪 |
| 2 | 数据面：Worker 打镜像，常驻容器 `faceblur-lite worker --cp-url …`，Compose `--scale` / K8s Deployment / 百舸任务式 | ✅ Task 11 `worker` 子命令 + 常驻模式（`idle_exit_s=0` 永不退出） |
| 3 | 存储：接共享并行文件系统（百舸 PFS / BOS 挂载 / NFS） | ✅ 路径契约：全链路传 POSIX 路径字符串，算子 `open(path)` 零改动 |
| 4 | 模型资产：随镜像分发 | ✅ `FACEBLUR_MODEL` 环境变量已预留 |
| 5 | 编排器退化为提交客户端 | ✅ `run_batch` 只依赖 ControlPlane 协议，`--cp-url` 即切 |

迁移保留/替换：**保留** 算子镜像、契约语义、质检判定、阈值策略；**替换** Worker 拉起方式（multiprocessing→容器编排）；gocp 可保留（自管模式）或由平台调度器替代（全托管模式，见③）。

#### ③ 手写 DAG → 通用 DAG 引擎切换（R13）

| 手写组件 | 通用引擎对应物（Argo/Kubeflow/百舸工作流） | 迁移时 |
|---|---|---|
| split / blur / merge / qc 算子 | DAG 节点模板（容器 command） | **保留**：算子已全部 CLI 子命令化，模板原样引用 |
| 原子 Join（最后完成者认领合并） | fan-in 依赖边（引擎原生保证） | **替换**：merge 节点由引擎喂入全部 chunk 输出，`merge_chunks(paths, out)` 签名不变 |
| chunks 队列 + claim 互斥 | 引擎调度器 | 替换（被收编） |
| lease 回收 + attempt 上限 | 引擎任务活性管理 + 重试策略 | 替换（被收编） |
| qc 闸门 + 阈值分域 | DAG 内质检节点 + 节点参数 | **保留**（判定逻辑与策略是资产） |

- **动态 fan-out**：split 节点输出 chunk 清单 → 引擎动态展开 blur×N（Argo `withParam` / 百舸组件化编排）
- **语义对齐**：本项目的 at-least-once + 幂等算子 + attempt 预算，与 DAG 引擎的重试语义一一对应——迁移无语义坑
- 结论：**切换 = 换调度层，算子层与质检/阈值策略 100% 保留**。手写窄版不是浪费——它是"理解工作流引擎在解决什么问题"的证据，且算子层投资全部延续

### 文件结构（职责即边界）

```
face-blur-lite/
├── README.md                      # 快速开始 + 架构图 + 设计决策
├── pyproject.toml
├── scripts/download_assets.sh     # 模型/测试资产下载（产物 gitignore）
├── docs/
│   ├── superpowers/plans/…        # 本方案
│   ├── control-plane-contract.md  # 控制面契约规范（Task 7，Go 实现依据）
│   ├── design.md                  # 详细设计·四大视图（Task 11，对外交付版）
│   └── platform-mapping.md        # 胜算/LAS/百舸对照＋通用引擎（Task 11）
├── src/faceblur_lite/
│   ├── __init__.py
│   ├── ffmpeg_io.py               # [数据面] probe / FFmpegReader / FFmpegWriter
│   ├── detector.py                # [数据面] Detector 协议 + YuNet 实现
│   ├── tracker.py                 # [数据面] mini-SORT：预测/关联/遮挡/低分回收
│   ├── blur.py                    # [数据面] 外扩 + 高斯模糊
│   ├── kernel.py                  # [数据面] 算子内核：视频→脱敏视频 + 报告
│   ├── testmedia.py               # [数据面测试基建] 合成视频（move/occlude）
│   ├── splitter.py                # [数据面] 关键帧流复制切片 + 短视频快路径
│   ├── merger.py                  # [数据面] concat 流复制合并 + 降级重编码
│   ├── qc.py                      # [数据面] 输出域抽帧复检算子
│   ├── control.py                 # [控制面] ControlPlane 协议（跨语言契约）
│   ├── state.py                   # [控制面] SqliteControlPlane：契约的参考实现
│   ├── remote.py                  # [控制面] HttpControlPlane：契约的 HTTP 客户端（对接 gocp）
│   ├── worker.py                  # [数据面] Worker 认领-执行-回写循环
│   ├── orchestrator.py            # [控制面] 编排：split→并行→merge→qc
│   ├── stats.py                   # [观测] 最小出口：排队/处理分位、吞吐、失败率（只读旁路，R14）
│   └── cli.py                     # single/pipeline/worker/split/merge/qc/stats/demo-video/status
├── gocp/                          # [控制面-Go] Go 控制面服务（Task 12，业界形态）
│   ├── go.mod                     # 唯一外部依赖 modernc.org/sqlite（纯 Go）
│   └── main.go                    # 单文件：HTTP API + SQLite，同 schema 同事务语义
└── tests/
    ├── conftest.py                # 资产 skip 标记 + 共享 session fixture
    └── test_*.py                  # 每模块一个测试文件
```

---

### Task 0: 项目脚手架

**Files:**
- Create: `.gitignore`、`pyproject.toml`、`src/faceblur_lite/__init__.py`、`scripts/download_assets.sh`、`tests/test_smoke.py`

**Interfaces:**
- Produces: 可安装的 `faceblur_lite` 包（后续所有任务 import 它）；`scripts/download_assets.sh` 产出 `models/face_detection_yunet_2023mar.onnx` 与 `assets/lena.jpg`

- [ ] **Step 1: 建目录 + git init + 写配置文件**

```bash
cd /Users/zhujiawei/Desktop/zte/backup/workbackup-main/face-blur-lite
git init
mkdir -p src/faceblur_lite scripts tests docs
```

`.gitignore`：

```
venv/
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
dist/
outputs/
work-*/
models/*.onnx
assets/*.jpg
```

`pyproject.toml`：

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "faceblur-lite"
version = "0.1.0"
description = "轻量视频人脸脱敏管线（控制面/数据面分离）"
requires-python = ">=3.10"
dependencies = ["opencv-python>=4.8", "numpy>=1.24", "scipy>=1.10"]

[project.optional-dependencies]
dev = ["pytest>=7.4"]

[project.scripts]
faceblur-lite = "faceblur_lite.cli:main"

[tool.setuptools.packages.find]
where = ["src"]
```

`src/faceblur_lite/__init__.py`：

```python
__version__ = "0.1.0"
```

`scripts/download_assets.sh`（模型与测试资产，产物不入 git）：

```bash
#!/usr/bin/env bash
# 下载模型与测试资产。产物已被 .gitignore 排除。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/models" "$ROOT/assets"

curl -L --fail -o "$ROOT/models/face_detection_yunet_2023mar.onnx" \
  "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

curl -L --fail -o "$ROOT/assets/lena.jpg" \
  "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/lena.jpg"

ls -la "$ROOT/models" "$ROOT/assets"
```

`tests/test_smoke.py`：

```python
import faceblur_lite


def test_version():
    assert faceblur_lite.__version__ == "0.1.0"
```

- [ ] **Step 2: 建 venv 并可编辑安装**

```bash
chmod +x scripts/download_assets.sh
python3 -m venv venv
./venv/bin/pip install -e ".[dev]"
```

Expected: 安装成功，无依赖冲突。

- [ ] **Step 3: 环境检查 + 下载资产**

```bash
ffmpeg -version | head -1        # 有版本行即 OK；没有则 brew install ffmpeg
bash scripts/download_assets.sh  # 两个文件下载成功
```

- [ ] **Step 4: 跑第一个测试**

```bash
./venv/bin/python -m pytest -v
```

Expected: `1 passed`。

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: 项目脚手架（pyproject/venv/资产下载/冒烟测试）" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 1: ffmpeg_io.py — 流式解码/编码/probe

**Files:**
- Create: `src/faceblur_lite/ffmpeg_io.py`
- Test: `tests/test_ffmpeg_io.py`

**Interfaces:**
- Produces:
  - `probe(path: str) -> VideoMeta`，`VideoMeta(width, height, fps, fps_str, nb_frames=None, has_audio=False, duration_s=0.0)`（frozen dataclass）
  - `FFmpegReader(path, sample_fps: float | None = None)`：可迭代出 `(H,W,3) BGR uint8` ndarray；上下文管理器；`sample_fps` 用于质检抽帧（ffmpeg `fps` 滤镜）
  - `FFmpegWriter(path, meta: VideoMeta, crf=20, audio_from: str | None = None, gop: int | None = None)`：`.write(frame)`；`audio_from` 给定时从原片复制音轨；`gop` 强制关键帧间隔（切片测试需要）

- [ ] **Step 1: 写失败测试**

`tests/test_ffmpeg_io.py`：

```python
import shutil

import numpy as np
import pytest

from faceblur_lite.ffmpeg_io import FFmpegReader, FFmpegWriter, VideoMeta, probe

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="需要系统 ffmpeg")


def test_writer_reader_roundtrip(tmp_path):
    meta = VideoMeta(64, 48, 10.0, "10/1")
    out = str(tmp_path / "rt.mp4")
    with FFmpegWriter(out, meta) as w:
        for i in range(5):
            # (i+1)*40：帧值 40..200（原 i*40 首帧=0 与下方 |got[0]-40|<6 断言矛盾，2026-09-24 执行期修正）
            w.write(np.full((48, 64, 3), ((i + 1) * 40) % 255, dtype=np.uint8))

    m = probe(out)
    assert (m.width, m.height) == (64, 48)
    assert abs(m.fps - 10.0) < 0.1
    assert abs(m.duration_s - 0.5) < 0.1
    assert not m.has_audio

    with FFmpegReader(out) as rd:
        got = list(rd)
    assert len(got) == 5
    assert got[0].shape == (48, 64, 3)
    # 纯色帧近似无损：编码误差远小于帧间差
    assert np.mean(np.abs(got[0].astype(int) - got[4].astype(int))) > 100
    assert np.mean(np.abs(got[0].astype(int) - 40)) < 6


def test_sample_fps_reader(tmp_path):
    meta = VideoMeta(64, 48, 10.0, "10/1")
    out = str(tmp_path / "s.mp4")
    with FFmpegWriter(out, meta) as w:
        for i in range(20):
            w.write(np.full((48, 64, 3), (i * 10) % 255, dtype=np.uint8))
    with FFmpegReader(out, sample_fps=1.0) as rd:
        got = list(rd)
    assert len(got) == 2   # 20 帧 @10fps = 2s，1fps 抽 2 帧
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_ffmpeg_io.py -v
```

Expected: FAIL（`ModuleNotFoundError: No module named 'faceblur_lite.ffmpeg_io'`）。

- [ ] **Step 3: 最小实现**

`src/faceblur_lite/ffmpeg_io.py`：

```python
"""ffmpeg/ffprobe 流式 IO（数据面基础设施）。

R4 输入格式链路：视频文件 --ffmpeg -f rawvideo -pix_fmt bgr24 pipe-->
(H,W,3) BGR uint8 ndarray。解码归 ffmpeg，检测归 ONNX，中间只有 numpy。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class VideoMeta:
    width: int
    height: int
    fps: float
    fps_str: str            # 精确分数表示，如 "30000/1001"
    nb_frames: int | None = None
    has_audio: bool = False
    duration_s: float = 0.0


def probe(path: str) -> VideoMeta:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", path],
        capture_output=True, text=True, check=True)
    data = json.loads(r.stdout)
    streams = data["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    num, _, den = v["r_frame_rate"].partition("/")
    fps = float(num) / float(den) if den and float(den) != 0 else 30.0
    raw_nb = str(v.get("nb_frames", ""))
    nb = int(raw_nb) if raw_nb.isdigit() else None
    dur = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    return VideoMeta(
        width=int(v["width"]), height=int(v["height"]),
        fps=fps, fps_str=v["r_frame_rate"], nb_frames=nb,
        has_audio=any(s["codec_type"] == "audio" for s in streams),
        duration_s=dur,
    )


class FFmpegReader:
    """逐帧读取。sample_fps 不为 None 时经 fps 滤镜抽帧（质检用）。"""

    def __init__(self, path: str, sample_fps: float | None = None):
        self.path = path
        self.meta = probe(path)
        cmd = ["ffmpeg", "-v", "error", "-i", path]
        if sample_fps is not None:
            cmd += ["-vf", f"fps={sample_fps}"]
        cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    def __enter__(self) -> "FFmpegReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __iter__(self):
        fs = self.meta.width * self.meta.height * 3
        while True:
            buf = self.proc.stdout.read(fs)
            if not buf or len(buf) < fs:
                break
            yield np.frombuffer(buf, np.uint8).reshape(
                self.meta.height, self.meta.width, 3)

    def close(self) -> None:
        if self.proc.stdout:
            self.proc.stdout.close()
        self.proc.wait()


class FFmpegWriter:
    """帧 pipe 给 ffmpeg 编码（全局钉死编码参数）。audio_from 给定时
    从原片流复制音轨（绝不重编）。gop 强制关键帧间隔（切片前提）。"""

    def __init__(self, path: str, meta: VideoMeta, crf: int = 20,
                 audio_from: str | None = None, gop: int | None = None):
        self.path = path
        self.meta = meta
        cmd = ["ffmpeg", "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{meta.width}x{meta.height}",
               "-r", meta.fps_str, "-i", "-"]
        if audio_from is not None:
            cmd += ["-i", audio_from, "-map", "0:v:0", "-map", "1:a:0?",
                    "-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        if gop is not None:
            cmd += ["-g", str(gop)]
        cmd.append(path)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        self.proc.stdin.write(np.ascontiguousarray(frame, np.uint8).tobytes())

    def __enter__(self) -> "FFmpegWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.proc.stdin.close()
        ret = self.proc.wait()
        if ret != 0:
            raise RuntimeError(f"ffmpeg 编码失败 rc={ret}: {self.path}")
```

- [ ] **Step 4: 运行确认通过**

```bash
./venv/bin/python -m pytest tests/test_ffmpeg_io.py -v
```

Expected: `2 passed`。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/ffmpeg_io.py tests/test_ffmpeg_io.py
git commit -m "feat: ffmpeg 流式解码/编码/probe（含 fps 抽样读取）" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: blur.py — 外扩 + 高斯模糊

**Files:**
- Create: `src/faceblur_lite/blur.py`
- Test: `tests/test_blur.py`

**Interfaces:**
- Produces:
  - `expand_box(box: np.ndarray(xyxy), frame_w, frame_h, margin=0.15) -> tuple[int,int,int,int]`（外扩+clip 到帧内）
  - `blur_regions(frame, boxes: list[np.ndarray(xyxy)], margin=0.15) -> np.ndarray`（返回新帧；空列表返回副本）

- [ ] **Step 1: 写失败测试**

`tests/test_blur.py`：

```python
import numpy as np

from faceblur_lite.blur import blur_regions, expand_box


def test_blur_changes_only_region():
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    box = np.array([200.0, 200.0, 300.0, 300.0])
    out = blur_regions(frame, [box], margin=0.0)
    # 框内被模糊（变化）
    assert not np.array_equal(out[210:290, 210:290], frame[210:290, 210:290])
    # 框外原样
    assert np.array_equal(out[:100, :100], frame[:100, :100])
    assert np.array_equal(out[400:, 400:], frame[400:, 400:])


def test_blur_empty_boxes_noop():
    frame = np.zeros((48, 64, 3), np.uint8)
    out = blur_regions(frame, [])
    assert np.array_equal(out, frame)


def test_expand_clips_to_frame():
    x1, y1, x2, y2 = expand_box(np.array([5.0, 5.0, 60.0, 60.0]), 64, 48, 0.5)
    assert x1 == 0 and y1 == 0
    assert 0 < x2 <= 64 and 0 < y2 <= 48
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_blur.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 最小实现**

`src/faceblur_lite/blur.py`：

```python
"""人脸区域模糊（数据面算子库）。脱敏代价不对称：宁可多糊。"""
from __future__ import annotations

import cv2
import numpy as np


def expand_box(box: np.ndarray, frame_w: int, frame_h: int,
               margin: float = 0.15) -> tuple[int, int, int, int]:
    """框四边外扩 margin 比例（按宽高较大者），clip 到帧内，返回整数像素。"""
    x1, y1, x2, y2 = (float(v) for v in box)
    m = margin * max(x2 - x1, y2 - y1)
    x1 = max(0, int(round(x1 - m)))
    y1 = max(0, int(round(y1 - m)))
    x2 = min(frame_w, int(round(x2 + m)))
    y2 = min(frame_h, int(round(y2 + m)))
    return x1, y1, max(x2, x1 + 1), max(y2, y1 + 1)


def blur_regions(frame: np.ndarray, boxes: list[np.ndarray],
                 margin: float = 0.15) -> np.ndarray:
    """对每个 xyxy 框外扩后做高斯模糊，返回新帧。"""
    out = frame.copy()
    h, w = frame.shape[:2]
    for box in boxes:
        x1, y1, x2, y2 = expand_box(box, w, h, margin)
        crop = out[y1:y2, x1:x2]
        # 核比例 0.75（2026-09-24 执行期修正，原 0.15）：0.15 只抹高频，
        # 低频对比（发色 vs 肤色）存活——隐私脱敏语义要求"宁可多糊"，
        # 大核直接摧毁低频结构；实测 std 比 0.15→0.884 / 0.75→0.693
        k = max(3, int(min(crop.shape[:2]) * 0.75) // 2 * 2 + 1)  # 奇数核
        out[y1:y2, x1:x2] = cv2.GaussianBlur(crop, (k, k), 0)
    return out
```

- [ ] **Step 4: 运行确认通过**

```bash
./venv/bin/python -m pytest tests/test_blur.py -v
```

Expected: `3 passed`。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/blur.py tests/test_blur.py
git commit -m "feat: 人脸框外扩+高斯模糊" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: detector.py — 检测器抽象 + YuNet

**Files:**
- Create: `src/faceblur_lite/detector.py`
- Test: `tests/test_detector.py`

**Interfaces:**
- Produces:
  - `Det(x1, y1, x2, y2, conf)` frozen dataclass，`.xyxy() -> np.ndarray(4,)`
  - `Detector` Protocol：`detect(frame_bgr: np.ndarray) -> list[Det]`（像素坐标、原帧分辨率）
  - `YuNetDetector(model_path: str | None = None, conf_thr: float = 0.3, top_k: int = 50)`
  - `resolve_model_path() -> str`：读 `FACEBLUR_MODEL` 环境变量，否则项目 `models/`，都没有则报错并提示下载脚本

- [ ] **Step 1: 写失败测试**

`tests/test_detector.py`：

```python
from pathlib import Path

import cv2
import numpy as np
import pytest

from faceblur_lite.detector import YuNetDetector

ROOT = Path(__file__).resolve().parents[1]   # tests/ 上一级=项目根（parents[2] 少一层，2026-09-24 执行期修正）
MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
LENA = ROOT / "assets" / "lena.jpg"

pytestmark = pytest.mark.skipif(
    not MODEL.exists() or not LENA.exists(),
    reason="先运行 scripts/download_assets.sh")


def test_detects_real_face():
    img = cv2.imread(str(LENA))
    dets = YuNetDetector(str(MODEL), conf_thr=0.6).detect(img)
    assert len(dets) >= 1
    d = dets[0]
    assert 0 < d.x2 - d.x1 < img.shape[1]
    assert 0 < d.y2 - d.y1 < img.shape[0]
    assert 0.6 <= d.conf <= 1.0


def test_no_face_on_blank():
    blank = np.full((480, 640, 3), 128, dtype=np.uint8)
    assert YuNetDetector(str(MODEL), conf_thr=0.6).detect(blank) == []
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_detector.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 最小实现**

`src/faceblur_lite/detector.py`：

```python
"""人脸检测器抽象 + YuNet 实现（数据面）。R5：换 SCRFD 只加实现类。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = PROJECT_ROOT / "models" / "face_detection_yunet_2023mar.onnx"


def resolve_model_path() -> str:
    env = os.environ.get("FACEBLUR_MODEL")
    if env:
        return env
    if DEFAULT_MODEL.exists():
        return str(DEFAULT_MODEL)
    raise FileNotFoundError(
        f"未找到模型 {DEFAULT_MODEL}；先运行 scripts/download_assets.sh"
        " 或设置 FACEBLUR_MODEL")


@dataclass(frozen=True)
class Det:
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float

    def xyxy(self) -> np.ndarray:
        return np.array([self.x1, self.y1, self.x2, self.y2])


class Detector(Protocol):
    def detect(self, frame_bgr: np.ndarray) -> list[Det]: ...


class YuNetDetector:
    """opencv_zoo YuNet（约 1MB）。预处理（缩放/归一化）在检测器内部完成，
    输入原始 BGR uint8 帧；setInputSize 按帧分辨率设置（R4）。"""

    def __init__(self, model_path: str | None = None, conf_thr: float = 0.3,
                 top_k: int = 50):
        path = model_path or resolve_model_path()
        self._det = cv2.FaceDetectorYN.create(
            str(path), "", (320, 320), score_threshold=conf_thr, top_k=top_k)

    def detect(self, frame_bgr: np.ndarray) -> list[Det]:
        h, w = frame_bgr.shape[:2]
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(frame_bgr)
        if faces is None:
            return []
        out = []
        for f in faces:
            x, y, bw, bh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
            out.append(Det(x, y, x + bw, y + bh, float(f[-1])))
        return out
```

- [ ] **Step 4: 运行确认通过**

```bash
./venv/bin/python -m pytest tests/test_detector.py -v
```

Expected: `2 passed`。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/detector.py tests/test_detector.py
git commit -m "feat: Detector 协议 + YuNet 实现" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: tracker.py — mini-SORT（R2 遮挡 + R6 下一帧预测）

**Files:**
- Create: `src/faceblur_lite/tracker.py`
- Test: `tests/test_tracker.py`

**Interfaces:**
- Consumes: `Det`（Task 3）
- Produces:
  - `Track(id, box: np.ndarray(xyxy), last_box: np.ndarray, vel: np.ndarray(4,), hits, misses)` dataclass
  - `Tracker(high_thr=0.5, low_thr=0.3, iou_thr=0.1, max_age=3, alpha=0.6)`（iou_thr 0.3→0.1，2026-09-24 执行期修正：出生首跳 vel=0，40px 框跳 30px 的 IoU=0.143 < 0.3 永不匹配→连锁重生；0.1 对 demo 几何（120px 脸约 6.5px/帧）无影响）
    - `.predict() -> list[Track]`：恒速模型外推一帧（**R6 下一帧坐标预测**）
    - `.update(dets: list[Det], gap: int = 1) -> list[Track]`：高分框匈牙利匹配校正；未匹配 track 用低分框二轮救活；`max_age` 个检测轮无匹配才淘汰
    - `.total_tracks -> int` property：累计出生过的 track 数
  - `iou_matrix(a: np.ndarray(n,4), b: np.ndarray(m,4)) -> np.ndarray(n,m)`
  - `associate(tracks_boxes, det_boxes, thr) -> (pairs, 未匹配track下标, 未匹配det下标)`

- [ ] **Step 1: 写失败测试**

`tests/test_tracker.py`：

```python
from faceblur_lite.detector import Det
from faceblur_lite.tracker import Tracker


def d(x, y, s=40, conf=0.9):
    return Det(x, y, x + s, y + s, conf)


def test_static_target_predict_holds_position():
    tr = Tracker()
    tr.update([d(100, 100)], gap=3)
    [t] = tr.predict()
    assert abs(t.box[0] - 100) < 1


def test_constant_velocity_estimated():
    tr = Tracker()
    for k in range(6):                       # 30px/3帧 = 10px/帧
        tr.update([d(100 + 30 * k, 100)], gap=3)
        tr.predict(); tr.predict()           # 模拟 det_every=3 的中间帧
    assert 7 <= tr.tracks[0].vel[0] <= 11    # 速度收敛到 10px/帧
    before = tr.tracks[0].box[0]
    tr.predict()                             # 预测下一帧坐标
    assert 5 <= tr.tracks[0].box[0] - before <= 12


def test_occlusion_prediction_survival_then_prune():
    tr = Tracker(max_age=2)
    tr.update([d(100, 100)], gap=3)
    tr.update([d(130, 100)], gap=3)
    for _ in range(2):                       # 遮挡 2 个检测轮：预测外推维持
        tr.predict()
        assert len(tr.tracks) == 1
        tr.update([], gap=3)
    assert 130 <= tr.tracks[0].box[0] <= 260  # 按速度外推，在合理区间
    tr.predict()
    tr.update([], gap=3)                     # 第 3 轮无匹配 → 剪枝（ghost 防护）
    assert tr.tracks == []


def test_low_conf_second_pass_recovery():
    tr = Tracker()
    tr.update([d(100, 100)], gap=1)
    tr.update([d(112, 100, conf=0.35)], gap=1)   # 遮挡期低分框
    t = tr.tracks[0]
    assert t.hits == 2                        # 被二轮关联救活并校正
    assert abs(t.box[0] - 112) < 1


def test_unmatched_high_conf_births_new_track():
    tr = Tracker()
    tr.update([d(100, 100)], gap=1)
    tr.update([d(100, 100), d(400, 400)], gap=1)
    assert len(tr.tracks) == 2
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_tracker.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 最小实现**

`src/faceblur_lite/tracker.py`：

```python
"""mini-SORT 跟踪器（数据面核心，R2 遮挡 + R6 下一帧坐标预测）。

- 恒速运动模型：predict() 把每个 track 按速度外推一帧（= 预测下一帧坐标）
- update()：高分框匈牙利匹配校正；未匹配 track 用低分框二轮救活
  （ByteTrack 思路——遮挡期置信度塌落时不断链）；max_age 个检测轮无匹配才
  淘汰（ghost 剪枝：预测框不能无限存活）
- 速度估计按"距上次检测的帧数 gap"归一，det_every>1 时依然正确
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from faceblur_lite.detector import Det


@dataclass
class Track:
    id: int
    box: np.ndarray                  # xyxy，当前（可能为预测的）位置
    last_box: np.ndarray             # 上一次被检测框校正的位置
    vel: np.ndarray = field(default_factory=lambda: np.zeros(4))  # px/帧
    hits: int = 0
    misses: int = 0                  # 连续未匹配的检测轮数


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a:(n,4) b:(m,4) xyxy → (n,m) IoU。"""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    ax1, ay1, ax2, ay2 = (a[:, i:i + 1] for i in range(4))
    bx1, by1, bx2, by2 = (b[None, :, i] for i in range(4))
    inter = (np.maximum(0, np.minimum(ax2, bx2) - np.maximum(ax1, bx1))
             * np.maximum(0, np.minimum(ay2, by2) - np.maximum(ay1, by1)))
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / (ua + 1e-9)


def associate(tracks_boxes: list, det_boxes: list,
              thr: float) -> tuple[list, list, list]:
    """返回 (匹配对, 未匹配 track 下标, 未匹配 det 下标)。"""
    if not tracks_boxes or not det_boxes:
        return [], list(range(len(tracks_boxes))), list(range(len(det_boxes)))
    m = iou_matrix(np.stack(tracks_boxes), np.stack(det_boxes))
    rows, cols = linear_sum_assignment(1.0 - m)
    pairs = [(int(r), int(c)) for r, c in zip(rows, cols) if m[r, c] >= thr]
    ta = {a for a, _ in pairs}
    db = {b for _, b in pairs}
    return (pairs,
            [i for i in range(len(tracks_boxes)) if i not in ta],
            [j for j in range(len(det_boxes)) if j not in db])


class Tracker:
    def __init__(self, high_thr: float = 0.5, low_thr: float = 0.3,
                 iou_thr: float = 0.1, max_age: int = 3, alpha: float = 0.6):
        self.high_thr = high_thr
        self.low_thr = low_thr
        self.iou_thr = iou_thr
        self.max_age = max_age
        self.alpha = alpha
        self.tracks: list[Track] = []
        self._next_id = 1

    @property
    def total_tracks(self) -> int:
        """累计出生过的 track 数（≈出现人脸次数；遮挡断链重检出会 +1）。"""
        return self._next_id - 1

    # ---- R6：预测下一帧坐标 ----
    def predict(self) -> list[Track]:
        for t in self.tracks:
            t.box = t.box + t.vel
        return self.tracks

    def update(self, dets: list[Det], gap: int = 1) -> list[Track]:
        hi = [d for d in dets if d.conf >= self.high_thr]
        lo = [d for d in dets if self.low_thr <= d.conf < self.high_thr]

        tboxes = [t.box for t in self.tracks]
        pool = hi + lo                      # 全局 det 下标：前 len(hi) 为高分
        p1, um_t, um_hi = associate(
            tboxes, [d.xyxy() for d in hi], self.iou_thr)
        pairs = p1
        if lo and um_t:                     # 二轮：低分框只救活，不出生
            p2, um_sub, _ = associate(
                [tboxes[i] for i in um_t],
                [d.xyxy() for d in lo], self.iou_thr)
            pairs += [(um_t[a], len(hi) + b) for a, b in p2]
            um_t = [um_t[i] for i in um_sub]

        for ti, dj in pairs:
            t, d = self.tracks[ti], pool[dj]
            inst_v = (d.xyxy() - t.last_box) / max(1, gap)
            t.vel = (inst_v if t.hits == 0
                     else self.alpha * t.vel + (1 - self.alpha) * inst_v)
            t.box = t.last_box = d.xyxy()
            t.hits += 1
            t.misses = 0

        for ti in um_t:
            self.tracks[ti].misses += 1
        self.tracks = [t for t in self.tracks if t.misses <= self.max_age]

        for j in um_hi:                     # 只有高分未匹配框允许出生
            self.tracks.append(              # hits=1：出生检测计入 hit（SORT 语义）
                Track(self._next_id, hi[j].xyxy(), hi[j].xyxy(), hits=1))
            self._next_id += 1
        return self.tracks
```

- [ ] **Step 4: 运行确认通过**

```bash
./venv/bin/python -m pytest tests/test_tracker.py -v
```

Expected: `5 passed`。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/tracker.py tests/test_tracker.py
git commit -m "feat: mini-SORT 跟踪器（恒速预测/遮挡外推/低分二轮回收）" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: testmedia.py + kernel.py — 算子内核（R1）

**Files:**
- Create: `src/faceblur_lite/testmedia.py`、`src/faceblur_lite/kernel.py`、`tests/conftest.py`
- Test: `tests/test_kernel.py`

**Interfaces:**
- Consumes: `FFmpegReader/FFmpegWriter/probe`（Task 1）、`YuNetDetector`（Task 3）、`Tracker`（Task 4）、`blur_regions`（Task 2）
- Produces:
  - `testmedia.load_face(path) -> np.ndarray`；`testmedia.build_video(out_path, face_img, scenario="move"|"occlude", duration_s=6.0, fps=10, size=(640,480), face_w=120) -> (frames: list, occluded: list[bool])`
  - `KernelParams(det_every=3, det_conf=0.3, margin=0.15, crf=20, high_thr=0.5, low_thr=0.3, max_age=3)`
  - `KernelReport(frames, detections, predicted_frames, track_count, segments: list[(起秒,止秒)], duration_s, wall_s)`
  - `process_video(input_path, output_path, params=None, model_path=None) -> KernelReport`
  - conftest：`requires_assets`（pytest 标记）+ session fixture `face_img`、`move_video -> (path, frames)`

- [ ] **Step 1: 写 conftest + 失败测试**

`tests/conftest.py`：

```python
import shutil
from pathlib import Path

import pytest

from faceblur_lite.testmedia import build_video, load_face

ROOT = Path(__file__).resolve().parents[1]   # tests/ 上一级=项目根（同 Task 3 修正）


def assets_ready() -> bool:
    return (shutil.which("ffmpeg") is not None
            and (ROOT / "assets" / "lena.jpg").exists()
            and (ROOT / "models" / "face_detection_yunet_2023mar.onnx").exists())


requires_assets = pytest.mark.skipif(
    not assets_ready(), reason="需要 ffmpeg 与下载资产（scripts/download_assets.sh）")


@pytest.fixture(scope="session")
def face_img():
    return load_face(str(ROOT / "assets" / "lena.jpg"))


@pytest.fixture(scope="session")
def move_video(tmp_path_factory, face_img):
    """6s@10fps，人脸匀速从左到右。返回 (视频路径, 帧列表)。"""
    path = str(tmp_path_factory.mktemp("media") / "move.mp4")
    frames, _ = build_video(path, face_img, duration_s=6.0)
    return path, frames
```

`tests/test_kernel.py`：

```python
import numpy as np
from conftest import requires_assets

from faceblur_lite.ffmpeg_io import FFmpegReader, probe
from faceblur_lite.kernel import KernelParams, process_video

pytestmark = requires_assets


def test_process_video_blurs_and_keeps_duration(move_video, tmp_path):
    src, frames = move_video
    out = str(tmp_path / "out.mp4")
    rep = process_video(src, out, KernelParams(det_every=2))

    # 报告：60 帧全处理；隔帧检测 → 30 检测帧 + 30 预测帧
    assert rep.frames == 60
    assert rep.predicted_frames == 30
    assert rep.detections >= 30
    assert rep.track_count >= 1
    assert rep.segments and rep.segments[0][0] <= 0.2

    # 输出时长保持
    m = probe(out)
    assert abs(m.duration_s - 6.0) < 0.4

    # 模糊生效：人脸区域方差显著下降
    with FFmpegReader(out) as rd:
        got = list(rd)
    i = 30
    x0, x1 = 640 // 5, 640 * 4 // 5
    x = int(x0 + (x1 - x0) * i / 59)
    crop_in = frames[i][170:270, x + 10:x + 110]
    crop_out = got[i][170:270, x + 10:x + 110]
    assert crop_out.std() < crop_in.std() * 0.8
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_kernel.py -v
```

Expected: FAIL（`faceblur_lite.testmedia` 不存在）。

- [ ] **Step 3: 实现 testmedia + kernel**

`src/faceblur_lite/testmedia.py`：

```python
"""合成测试视频（数据面测试基建）：人脸图贴到移动轨迹，可选中段遮挡。"""
from __future__ import annotations

import cv2
import numpy as np

from faceblur_lite.ffmpeg_io import FFmpegWriter, VideoMeta


def load_face(path: str) -> np.ndarray:
    """读入人脸图并裁成特写（2026-09-24 执行期修正）。

    YuNet 只检出 lena 的内脸（512 图坐标约 (201,186)-(355,388)，实测）；
    不裁的话贴图大部分是帽/肩/背景，检测框+margin 足迹只覆盖贴图约 1/3，
    模糊覆盖率断言（std 对比窗口≈整个贴图）数学上不可满足（下限 0.877）。
    裁成特写让 贴图≈检测框，"人脸区域方差显著下降"的断言语义才成立。
    """
    img = cv2.imread(path)
    if img is None:
        raise FileNotFoundError(path)
    return img[170:400, 185:385]


def build_video(out_path: str, face_img: np.ndarray, scenario: str = "move",
                duration_s: float = 6.0, fps: int = 10,
                size: tuple[int, int] = (640, 480),
                face_w: int = 120) -> tuple[list[np.ndarray], list[bool]]:
    """生成合成视频，返回 (帧列表, 每帧人脸是否被遮挡)。

    move:    人脸从左 1/5 匀速移到右 4/5
    occlude: 同上，中段 1/3 时段画灰矩形盖住人脸（模拟手臂遮挡，R2）
    """
    w, h = size
    n = int(duration_s * fps)
    fh = int(face_w * face_img.shape[0] / face_img.shape[1])
    face = cv2.resize(face_img, (face_w, fh))
    x0, x1 = w // 5, w * 4 // 5
    step = (x1 - x0) / max(1, n - 1)
    occl_lo, occl_hi = n // 3, n // 3 * 2

    frames: list[np.ndarray] = []
    occluded: list[bool] = []
    for i in range(n):
        frame = np.full((h, w, 3), 60, np.uint8)
        x, y = int(x0 + step * i), h // 3
        frame[y:y + fh, x:x + face_w] = face
        occ = scenario == "occlude" and occl_lo <= i < occl_hi
        if occ:
            frame[y - 10:y + fh + 10, x - 10:x + face_w + 10] = (120, 120, 120)
        frames.append(frame)
        occluded.append(occ)

    meta = VideoMeta(w, h, float(fps), f"{fps}/1")
    with FFmpegWriter(out_path, meta, gop=25) as vw:   # 密集关键帧，供切片测试
        for f in frames:
            vw.write(f)
    return frames, occluded
```

`src/faceblur_lite/kernel.py`：

```python
"""face-blur 算子内核（数据面）：一个视频文件 → 一个脱敏视频文件。R1。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from faceblur_lite.blur import blur_regions
from faceblur_lite.detector import YuNetDetector
from faceblur_lite.ffmpeg_io import FFmpegReader, FFmpegWriter, probe
from faceblur_lite.tracker import Tracker


@dataclass
class KernelParams:
    det_every: int = 3          # 每 N 帧检测一次，中间帧用跟踪预测补位
    det_conf: float = 0.3       # 源域检测阈值（宁可多糊）
    margin: float = 0.15        # 模糊框外扩比例
    crf: int = 20
    high_thr: float = 0.5       # 跟踪高分阈值
    low_thr: float = 0.3        # 跟踪低分阈值（二轮救活下限）
    max_age: int = 3            # 遮挡容忍的检测轮数


@dataclass
class KernelReport:
    frames: int = 0
    detections: int = 0             # 检测帧出框总数
    predicted_frames: int = 0       # 纯预测帧数（无检测的那些帧）
    track_count: int = 0            # 累计 track 数
    segments: list[tuple[float, float]] = field(default_factory=list)
    duration_s: float = 0.0
    wall_s: float = 0.0


def process_video(input_path: str, output_path: str,
                  params: KernelParams | None = None,
                  model_path: str | None = None) -> KernelReport:
    p = params or KernelParams()
    rep = KernelReport()
    t0 = time.time()
    meta = probe(input_path)
    detector = YuNetDetector(model_path, conf_thr=p.det_conf)
    tracker = Tracker(high_thr=p.high_thr, low_thr=p.low_thr, max_age=p.max_age)

    face_frames: list[bool] = []
    rd = FFmpegReader(input_path)
    wr = FFmpegWriter(output_path, meta, crf=p.crf,
                      audio_from=input_path if meta.has_audio else None)
    with rd, wr:
        for i, frame in enumerate(rd):
            if i % p.det_every == 0:
                dets = detector.detect(frame)
                rep.detections += len(dets)
                active = tracker.update(dets, gap=p.det_every)
            else:
                active = tracker.predict()
                rep.predicted_frames += 1
            boxes = [t.box for t in active]
            face_frames.append(bool(boxes))
            wr.write(blur_regions(frame, boxes, margin=p.margin))

    rep.frames = len(face_frames)
    rep.track_count = tracker.total_tracks
    rep.segments = _segments(face_frames, meta.fps)
    rep.duration_s = rep.frames / meta.fps if meta.fps else 0.0
    rep.wall_s = time.time() - t0
    return rep


def _segments(face_frames: list[bool],
              fps: float) -> list[tuple[float, float]]:
    """连续有人脸的帧段 → [(起秒, 止秒)]。"""
    segs: list[tuple[float, float]] = []
    start = None
    for i, has in enumerate(list(face_frames) + [False]):   # 哨兵收尾
        if has and start is None:
            start = i
        elif not has and start is not None:
            segs.append((round(start / fps, 2), round(i / fps, 2)))
            start = None
    return segs
```

注意：`kernel.py` 里 `import numpy as np` 未直接使用则删掉（保持零死代码）。

- [ ] **Step 4: 运行确认通过**

```bash
./venv/bin/python -m pytest tests/test_kernel.py -v
```

Expected: `1 passed`（若人脸区域 std 断言偶发不达标，先人工看输出帧确认模糊强度，再调 `blur_regions` 核大小，不许放宽断言到无意义）。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/testmedia.py src/faceblur_lite/kernel.py tests/conftest.py tests/test_kernel.py
git commit -m "feat: 脱敏内核（隔帧检测+跟踪补帧+外扩模糊+音轨复制）+ 合成视频基建" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: 遮挡场景 E2E（R2 验收）

**Files:**
- Test: `tests/test_occlusion.py`

**Interfaces:**
- Consumes: `build_video(scenario="occlude")`、`process_video`（Task 5）

- [ ] **Step 1: 写测试（预期直接通过——这是行为验收不是新代码）**

`tests/test_occlusion.py`：

```python
from conftest import requires_assets, face_img

from faceblur_lite.kernel import KernelParams, process_video
from faceblur_lite.testmedia import build_video

pytestmark = requires_assets


def test_occlusion_scenario(tmp_path, face_img):
    src = str(tmp_path / "occ.mp4")
    build_video(src, face_img, scenario="occlude", duration_s=6.0)
    out = str(tmp_path / "occ_out.mp4")
    rep = process_video(src, out, KernelParams(det_every=2))

    # 开局即检出；遮挡中段跟踪可能断链（segments 可能 1~3 段）；
    # 断链后重检出会增加 track 数——对"糊脸"而言 ID 切换不是错误，
    # 每一帧的脸都被糊才是安全底线
    assert rep.segments[0][0] <= 0.2
    assert 1 <= len(rep.segments) <= 3
    assert rep.track_count >= 1
    assert abs(rep.duration_s - 6.0) < 0.4
```

- [ ] **Step 2: 运行**

```bash
./venv/bin/python -m pytest tests/test_occlusion.py -v
```

Expected: `1 passed`。若失败（如遮挡中段预测框漂移导致 segments 碎片超 3 段），先调 `KernelParams.max_age`（默认 3）再查 tracker 单测，不改断言语义。

- [ ] **Step 3: Commit**

```bash
git add tests/test_occlusion.py
git commit -m "test: 遮挡场景 E2E 验收（预测外推维持模糊覆盖）" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: 控制面契约 — ControlPlane 协议 + SQLite 参考实现（Go 可换）

**Files:**
- Create: `src/faceblur_lite/control.py`、`src/faceblur_lite/state.py`、`docs/control-plane-contract.md`
- Test: `tests/test_contract.py`

**Interfaces:**
- Produces:
  - `control.py`：`ControlPlane` Protocol——**跨语言契约的代码面**，数据面唯一可依赖的控制面入口（数据面禁止 import `state.py`）
  - `state.py`：`SqliteControlPlane(db_path)`——契约的参考实现；每方法短连接+事务；实例只含 `db_path` 字符串（可 pickle，供 `multiprocessing` spawn 传参）
  - 方法集（协议与实现签名一致）：`init_db()` / `submit_video(input_path, final_output, chunk_inputs) -> video_id` / `claim_chunk(worker_id, ttl_s) -> dict|None`（并发互斥；只派发 video 为 processing 的 chunk）/ `complete_chunk(chunk_id, metrics) -> bool`（原子 Join：恰好一个调用者拿到 True）/ `complete_merge(video_id, merged_path)` / `fail_chunk(chunk_id, error, max_attempts=3)` / `reclaim_expired(max_attempts=3) -> int` / `get_video(video_id) -> dict` / `chunk_outputs(video_id) -> list[str]` / `video_overview() -> list[dict]` / `finish_video(video_id, status)`
  - 状态机：videos `processing→merging→qc_wait→done|qc_failed|failed`；chunks `pending→running→done|failed`
  - `docs/control-plane-contract.md`：schema/状态机/操作事务语义/不变量/Go 实现指引——**未来 Go 重写的唯一依据**

- [ ] **Step 1: 写失败测试（契约场景，任何实现都必须通过）**

`tests/test_contract.py`：

```python
import pytest

from faceblur_lite.state import SqliteControlPlane


@pytest.fixture()
def cp(tmp_path):
    cp_ = SqliteControlPlane(str(tmp_path / "s.db"))
    cp_.init_db()
    return cp_


def _mk(cp, n=3):
    return cp.submit_video(
        "in.mp4", "out.mp4",
        [(i, f"c{i}.mp4", f"o{i}.mp4") for i in range(n)])


def test_claim_exclusive(cp):
    _mk(cp, n=3)
    a = cp.claim_chunk("w1", ttl_s=60)
    b = cp.claim_chunk("w2", ttl_s=60)
    assert a["id"] != b["id"]
    assert a["status"] == "running"


def test_atomic_join_single_claim(cp):
    _mk(cp, n=2)
    t1 = cp.claim_chunk("w1", 60)
    t2 = cp.claim_chunk("w2", 60)
    assert cp.complete_chunk(t1["id"], {}) is False
    assert cp.complete_chunk(t2["id"], {}) is True   # 最后完成者认领 merge
    assert cp.video_overview()[0]["status"] == "merging"
    cp.complete_merge(t2["video_id"], "out.mp4")
    assert cp.video_overview()[0]["status"] == "qc_wait"


def test_lease_reclaim_and_attempt(cp):
    _mk(cp, n=1)
    cp.claim_chunk("w1", ttl_s=-1)          # 立即过期（模拟 Worker 暴死）
    assert cp.reclaim_expired() == 1
    t2 = cp.claim_chunk("w2", 60)           # 可重新认领且 attempt+1
    assert t2["attempt"] == 1


def test_fail_exhausts_to_video_failed(cp):
    _mk(cp, n=1)
    t = cp.claim_chunk("w1", 60)
    for _ in range(3):
        cp.fail_chunk(t["id"], "boom", max_attempts=3)
    assert cp.video_overview()[0]["status"] == "failed"
    assert cp.claim_chunk("w2", 60) is None  # 失败视频不再派发


def test_multi_video_shared_queue(cp):
    v1 = _mk(cp, n=2)
    v2 = _mk(cp, n=2)
    got = [cp.claim_chunk("w", 60) for _ in range(4)]
    assert len({t["id"] for t in got}) == 4          # 互斥：4 个不同 chunk
    assert {t["video_id"] for t in got} == {v1, v2}  # 多视频交错同池（R10）
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_contract.py -v
```

Expected: FAIL（`ModuleNotFoundError: No module named 'faceblur_lite.state'`）。

- [ ] **Step 3: 实现协议 + 参考实现 + 契约文档**

`src/faceblur_lite/control.py`：

```python
"""ControlPlane 协议——控制面跨语言契约的代码面。

数据面只准 import 本协议，不准 import 任何实现（如 state.py）。
契约完整规范见 docs/control-plane-contract.md；Go 等其他语言实现
必须满足同一契约并通过 tests/test_contract.py 的同等场景。
"""
from __future__ import annotations

from typing import Protocol


class ControlPlane(Protocol):
    def init_db(self) -> None: ...
    def submit_video(self, input_path: str, final_output: str,
                     chunk_inputs: list[tuple[int, str, str]]) -> str: ...
    def claim_chunk(self, worker_id: str, ttl_s: float) -> dict | None: ...
    def complete_chunk(self, chunk_id: str, metrics: dict) -> bool: ...
    def complete_merge(self, video_id: str, merged_path: str) -> None: ...
    def fail_chunk(self, chunk_id: str, error: str,
                   max_attempts: int = 3) -> None: ...
    def reclaim_expired(self, max_attempts: int = 3) -> int: ...
    def get_video(self, video_id: str) -> dict: ...
    def chunk_outputs(self, video_id: str) -> list[str]: ...
    def video_overview(self) -> list[dict]: ...
    def finish_video(self, video_id: str, status: str) -> None: ...
```

`src/faceblur_lite/state.py`：

```python
"""SqliteControlPlane——控制面契约的参考实现（SQLite WAL）。

方法语义与 docs/control-plane-contract.md 一一对应；每方法短连接+事务，
实例只含 db_path（可 pickle，spawn 传参安全）。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class SqliteControlPlane:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS videos(
                id TEXT PRIMARY KEY,
                input_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                merged_path TEXT,
                status TEXT NOT NULL,
                error TEXT,
                created_at REAL NOT NULL,
                finished_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS chunks(
                id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(id),
                idx INTEGER NOT NULL,
                input_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT,
                lease_until REAL,
                metrics TEXT,
                enqueued_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                error TEXT,
                UNIQUE(video_id, idx))""")

    def submit_video(self, input_path: str, final_output: str,
                     chunk_inputs: list[tuple[int, str, str]]) -> str:
        """chunk_inputs: [(idx, chunk输入, chunk输出)]。返回 video_id。"""
        vid = uuid.uuid4().hex[:12]
        now = time.time()
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO videos(id,input_path,output_path,merged_path,"
                "status,error,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                (vid, input_path, final_output, None, "processing",
                 None, now, None))
            c.executemany(
                "INSERT INTO chunks(id,video_id,idx,input_path,output_path,"
                "status,attempt,worker_id,lease_until,metrics,enqueued_at,"
                "started_at,finished_at,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(uuid.uuid4().hex[:12], vid, i, cin, cout, "pending", 0,
                  None, None, None, now, None, None, None)
                 for i, cin, cout in chunk_inputs])
        return vid

    def claim_chunk(self, worker_id: str, ttl_s: float) -> dict | None:
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                """SELECT c.* FROM chunks c JOIN videos v ON c.video_id = v.id
                   WHERE c.status='pending' AND v.status='processing'
                   ORDER BY v.created_at, c.idx LIMIT 1""").fetchone()
            if row is None:
                return None
            now = time.time()
            c.execute(
                """UPDATE chunks SET status='running', worker_id=?,
                   lease_until=?, started_at=? WHERE id=? AND status='pending'""",
                (worker_id, now + ttl_s, now, row["id"]))
            out = dict(row)
            out.update(status="running", worker_id=worker_id,
                       lease_until=now + ttl_s)
            return out

    def complete_chunk(self, chunk_id: str, metrics: dict) -> bool:
        """置 done；若该 video 全部 done，同一事务内认领 merge。

        并发下恰好一个调用者拿到 True——原子 Join（scatter-gather 门控）。
        """
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM chunks WHERE id=?",
                            (chunk_id,)).fetchone()
            c.execute(
                "UPDATE chunks SET status='done', metrics=?, finished_at=? "
                "WHERE id=?", (json.dumps(metrics), time.time(), chunk_id))
            left = c.execute(
                "SELECT COUNT(*) FROM chunks WHERE video_id=? AND status!='done'",
                (row["video_id"],)).fetchone()[0]
            if left == 0:
                cur = c.execute(
                    "UPDATE videos SET status='merging' "
                    "WHERE id=? AND status='processing'",
                    (row["video_id"],))
                return cur.rowcount == 1
            return False

    def complete_merge(self, video_id: str, merged_path: str) -> None:
        with _connect(self.db_path) as c:
            c.execute(
                "UPDATE videos SET status='qc_wait', merged_path=? WHERE id=?",
                (merged_path, video_id))

    def fail_chunk(self, chunk_id: str, error: str,
                   max_attempts: int = 3) -> None:
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM chunks WHERE id=?",
                            (chunk_id,)).fetchone()
            if row["attempt"] + 1 >= max_attempts:
                now = time.time()
                c.execute(
                    "UPDATE chunks SET status='failed', error=?, "
                    "finished_at=? WHERE id=?", (error, now, chunk_id))
                c.execute(
                    "UPDATE videos SET status='failed', error=?, "
                    "finished_at=? WHERE id=?",
                    (error, now, row["video_id"]))
            else:
                c.execute(
                    """UPDATE chunks SET status='pending', attempt=attempt+1,
                       worker_id=NULL, lease_until=NULL, error=? WHERE id=?""",
                    (error, chunk_id))

    def reclaim_expired(self, max_attempts: int = 3) -> int:
        """lease 过期的 running chunk → pending 重派；耗尽 → video failed。"""
        now = time.time()
        with _connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT * FROM chunks WHERE status='running' "
                "AND lease_until < ?", (now,)).fetchall()
            for r in rows:
                if r["attempt"] + 1 >= max_attempts:
                    c.execute(
                        "UPDATE chunks SET status='failed', attempt=attempt+1 "
                        "WHERE id=?", (r["id"],))
                    c.execute(
                        "UPDATE videos SET status='failed', "
                        "error='lease 过期且重试耗尽', finished_at=? "
                        "WHERE id=?", (now, r["video_id"]))
                else:
                    c.execute(
                        """UPDATE chunks SET status='pending',
                           attempt=attempt+1, worker_id=NULL,
                           lease_until=NULL WHERE id=?""", (r["id"],))
            return len(rows)

    def get_video(self, video_id: str) -> dict:
        with _connect(self.db_path) as c:
            row = c.execute("SELECT * FROM videos WHERE id=?",
                            (video_id,)).fetchone()
            if row is None:
                raise KeyError(video_id)
            return dict(row)

    def chunk_outputs(self, video_id: str) -> list[str]:
        with _connect(self.db_path) as c:
            rows = c.execute(
                "SELECT output_path FROM chunks WHERE video_id=? ORDER BY idx",
                (video_id,)).fetchall()
            return [r["output_path"] for r in rows]

    def video_overview(self) -> list[dict]:
        with _connect(self.db_path) as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM videos ORDER BY created_at").fetchall()]

    def finish_video(self, video_id: str, status: str) -> None:
        with _connect(self.db_path) as c:
            c.execute("UPDATE videos SET status=?, finished_at=? WHERE id=?",
                      (status, time.time(), video_id))
```

`docs/control-plane-contract.md`：

```markdown
# 控制面契约规范（control-plane-contract）

控制面 = 任务状态机 + 调度决策的唯一拥有者。数据面（Worker/算子）只通过
本契约定义的操作与控制面交互，不得直接读写存储。任何语言的实现
（当前 Python `SqliteControlPlane`，规划中 Go）必须满足本规范，
并通过 `tests/test_contract.py` 的同等场景测试。

## 1. 存储 schema
（`videos` / `chunks` 两表 DDL，与 `state.py` 逐字一致；Go 实现建同构表。
时戳为 Unix epoch 秒，浮点）

## 2. 状态机
videos: processing → merging → qc_wait → done | qc_failed | failed
chunks: pending → running → done | failed
合法迁移仅由第 3 节操作触发；状态只能经控制面操作变更（单写入口）。

## 3. 操作语义（每个操作 = 一个事务）
| 操作 | 事务内容 | 保证 |
|---|---|---|
| submit_video | 建 video(processing) + N 个 chunk(pending) | 原子出现 |
| claim_chunk | 取最早 pending（video 须 processing，按 created_at,idx 序）→ running + lease_until | 并发互斥；失败视频自动出局 |
| complete_chunk | 置 done+metrics；若该 video 全 done → video merging | **恰好一个调用者拿到 merge 认领（原子 Join）** |
| complete_merge | video → qc_wait（记 merged_path） | — |
| fail_chunk | attempt+1：未耗尽 → pending 重派；耗尽 → chunk+video failed | attempt 单调递增 |
| reclaim_expired | running 且 lease_until 已过期 → 同 fail_chunk 语义 | 僵尸回收（Worker 暴死） |
| finish_video | video → done / qc_failed | 仅编排器调用 |

## 4. 不变量
- 每 chunk 至多一次成功执行（幂等输出由数据面保证，attempt 上限兜底）
- merge 至多被认领一次
- lease_until 到期前他人不可重派该 chunk
- 所有状态变更只经控制面操作

## 5. 并发实现要点（Go 实现指引）
- SQLite：WAL + busy_timeout=10000；写事务用 BEGIN IMMEDIATE 取代行锁
  （Go：database/sql + modernc.org/sqlite 或 mattn/go-sqlite3，
  `conn.BeginTx` 起手先执行 `BEGIN IMMEDIATE`）
- 事务内"读-判-写"（如 claim/complete_chunk）必须整体在 BEGIN IMMEDIATE
  内完成，不得拆两个事务
- 演进：存储换 Redis 时，原子 Join 须用 Lua 脚本等价实现
```

- [ ] **Step 4: 运行确认通过**

```bash
./venv/bin/python -m pytest tests/test_contract.py -v
```

Expected: `5 passed`。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/control.py src/faceblur_lite/state.py docs/control-plane-contract.md tests/test_contract.py
git commit -m "feat: 控制面契约——ControlPlane 协议 + SQLite 参考实现 + 契约规范文档" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: splitter.py + merger.py — 切片与合并（R3）

**Files:**
- Create: `src/faceblur_lite/splitter.py`、`src/faceblur_lite/merger.py`
- Test: `tests/test_split_merge.py`

**Interfaces:**
- Consumes: `probe`（Task 1）、`build_video`（Task 5）
- Produces:
  - `split_video(input_path, out_dir, segment_s=30.0) -> list[str]`：关键帧流复制切片；时长 ≤ segment_s 走快路径返回 `[input_path]`
  - `merge_chunks(chunk_paths: list[str], output_path: str) -> str`：concat demuxer `-c copy`；失败降级重编码合并

- [ ] **Step 1: 写失败测试**

`tests/test_split_merge.py`：

```python
from conftest import requires_assets

from faceblur_lite.ffmpeg_io import probe
from faceblur_lite.merger import merge_chunks
from faceblur_lite.splitter import split_video

pytestmark = requires_assets


def test_split_fast_path(move_video, tmp_path):
    # move_video 是 6s，segment 30s → 不切，原样返回
    assert split_video(move_video[0], str(tmp_path / "s"),
                       segment_s=30.0) == [move_video[0]]


def test_split_and_merge_roundtrip(move_video, tmp_path):
    chunks = split_video(move_video[0], str(tmp_path / "c"), segment_s=2.0)
    assert len(chunks) >= 2                       # 6s/2s，关键帧对齐
    out = str(tmp_path / "merged.mp4")
    merge_chunks(chunks, out)
    assert abs(probe(out).duration_s - 6.0) < 1.0
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_split_merge.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 最小实现**

`src/faceblur_lite/splitter.py`：

```python
"""关键帧流复制切片 + 短视频快路径（数据面）。R3。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from faceblur_lite.ffmpeg_io import probe


def split_video(input_path: str, out_dir: str,
                segment_s: float = 30.0) -> list[str]:
    """返回切片路径列表（按 idx 排序）。时长 ≤ segment_s 时快路径不切。"""
    if probe(input_path).duration_s <= segment_s:
        return [input_path]
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", input_path,
         "-c", "copy", "-map", "0", "-f", "segment",
         "-segment_time", str(segment_s), "-reset_timestamps", "1",
         str(Path(out_dir) / "chunk_%03d.mp4")],
        check=True)
    return sorted(str(p) for p in Path(out_dir).glob("chunk_*.mp4"))
```

`src/faceblur_lite/merger.py`：

```python
"""concat 流复制合并，失败降级重编码（数据面）。"""
from __future__ import annotations

import subprocess
from pathlib import Path


def merge_chunks(chunk_paths: list[str], output_path: str) -> str:
    """所有切片由同一套钉死参数编码 → concat + -c copy 秒级合并；
    流复制失败（参数不一致等边界）降级为重编码合并。"""
    lst = Path(output_path).with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{p}'\n" for p in chunk_paths),
                   encoding="utf-8")
    base = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(lst)]
    try:
        subprocess.run(base + ["-c", "copy", output_path], check=True)
    except subprocess.CalledProcessError:
        subprocess.run(
            base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "copy", output_path],
            check=True)
    finally:
        lst.unlink(missing_ok=True)
    return output_path
```

- [ ] **Step 4: 运行确认通过**

```bash
./venv/bin/python -m pytest tests/test_split_merge.py -v
```

Expected: `2 passed`。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/splitter.py src/faceblur_lite/merger.py tests/test_split_merge.py
git commit -m "feat: 关键帧流复制切片 + concat 合并（降级重编码）" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: qc.py — 输出域抽帧复检（R9）

**Files:**
- Create: `src/faceblur_lite/qc.py`
- Test: `tests/test_qc.py`

**Interfaces:**
- Consumes: `YuNetDetector`（Task 3）、`FFmpegReader(sample_fps=...)`（Task 1）、`process_video`（Task 5，仅测试用）
- Produces:
  - `Violation(t_sec, conf, box: tuple[int,int,int,int])`
  - `QCReport(sampled_frames, violations)`，`.passed -> bool` property
  - `qc_video(path, sample_fps=1.0, pass_conf=0.5, model_path=None) -> QCReport`：检出即违规（面积过滤：边长<8px 或面积>40% 帧的框不算——糊纹理噪声地板）

- [ ] **Step 1: 写失败测试**

`tests/test_qc.py`：

```python
from conftest import requires_assets

from faceblur_lite.kernel import KernelParams, process_video
from faceblur_lite.qc import qc_video

pytestmark = requires_assets


def test_qc_passes_fully_blurred(move_video, tmp_path):
    out = str(tmp_path / "ok.mp4")
    process_video(move_video[0], out, KernelParams(det_every=2))
    rep = qc_video(out)
    assert rep.sampled_frames >= 5
    assert rep.passed, rep.violations


def test_qc_catches_unblurred(move_video, tmp_path):
    out = str(tmp_path / "leak.mp4")
    # det_conf>1 强制零检测 → 输出完全未脱敏 → qc 必须兜住
    process_video(move_video[0], out,
                  KernelParams(det_every=2, det_conf=1.1))
    rep = qc_video(out)
    assert not rep.passed
    assert rep.violations and rep.violations[0].conf >= 0.5
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_qc.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 最小实现**

`src/faceblur_lite/qc.py`：

```python
"""质检算子（数据面）：输出域抽帧复检——R9"怎么证明没漏"。

阈值语义（必须遵守）：pass_conf 作用在重编码后的输出域，与内核源域阈值
det_conf 不可比较——有损重编码会改变置信度，两套阈值各自声明信号域。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from faceblur_lite.detector import YuNetDetector
from faceblur_lite.ffmpeg_io import FFmpegReader

DEFAULT_SAMPLE_FPS = 1.0
DEFAULT_PASS_CONF = 0.5


@dataclass
class Violation:
    t_sec: float
    conf: float
    box: tuple[int, int, int, int]


@dataclass
class QCReport:
    sampled_frames: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


def qc_video(path: str, sample_fps: float = DEFAULT_SAMPLE_FPS,
             pass_conf: float = DEFAULT_PASS_CONF,
             model_path: str | None = None) -> QCReport:
    rep = QCReport()
    det = YuNetDetector(model_path, conf_thr=pass_conf)
    with FFmpegReader(path, sample_fps=sample_fps) as rd:
        for i, frame in enumerate(rd):
            rep.sampled_frames += 1
            h, w = frame.shape[:2]
            for d in det.detect(frame):
                area = (d.x2 - d.x1) * (d.y2 - d.y1)
                if area < 8 * 8 or area > 0.4 * w * h:
                    continue    # 微框/巨框多为糊纹理噪声，不算违规
                rep.violations.append(Violation(
                    round(i / sample_fps, 2), d.conf,
                    (int(d.x1), int(d.y1), int(d.x2), int(d.y2))))
    return rep
```

- [ ] **Step 4: 运行确认通过**

```bash
./venv/bin/python -m pytest tests/test_qc.py -v
```

Expected: `2 passed`。若 `test_qc_passes_fully_blurred` 因糊纹理误检失败：只允许调 `pass_conf`/面积过滤参数（记录新值到本方案 Global Constraints），不许删测试。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/qc.py tests/test_qc.py
git commit -m "feat: 输出域抽帧复检算子（面积过滤噪声地板）" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 10: worker.py + orchestrator.py — 数据面执行器 + 批量编排（R3/R7/R10）

**Files:**
- Create: `src/faceblur_lite/worker.py`、`src/faceblur_lite/orchestrator.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `ControlPlane` 协议（Task 7）、`SqliteControlPlane`（Task 7）、`process_video/KernelParams`（Task 5）、`split_video`/`merge_chunks`（Task 8）、`qc_video/QCReport`（Task 9）
- Produces:
  - `worker_loop(cp: ControlPlane, worker_id, params: KernelParams | None = None, poll_s=0.5, idle_exit_s=15.0, ttl_s=900.0) -> dict`——**控制面依赖注入**：Go 换实现时 worker 零改动；`idle_exit_s=0` = 常驻永不退出（独立 Worker/容器部署模式，R12）
  - `PipelineResult(video_id, input_path, output_path, status, qc: QCReport | None, wall_s, chunk_count, error: str | None = None)`
  - `run_batch(inputs: list[str], outdir: str, workdir: str, workers=2, segment_s=30.0, params=None, qc_sample_fps=1.0) -> list[PipelineResult]`——批量：逐视频切片+提交（**失败隔离**：单视频失败记 failed 结果，批次继续）→ 共享 Worker 池 → drain 后逐视频质检
  - `run_pipeline(input_path, output_path, workdir, ...) -> PipelineResult`——单视频便捷封装（显式输出路径）
  - `cp_url: str | None = None`（`run_batch`/`run_pipeline`/`_orchestrate` 透传）：给定时注入 `HttpControlPlane` 走 **Go 控制面模式**（Task 12）；默认 SQLite 内嵌。Worker 经依赖注入拿到任一实现，零改动
  - 输出命名：`outdir/<stem>_blurred.mp4`
  - Worker 不感知"多个视频"——队列模型免费获得两级并发（大视频多片并行 + 多视频交错）

- [ ] **Step 1: 写失败测试**

`tests/test_pipeline.py`：

```python
from pathlib import Path

from conftest import face_img, requires_assets

from faceblur_lite.ffmpeg_io import probe
from faceblur_lite.kernel import KernelParams
from faceblur_lite.orchestrator import run_batch, run_pipeline
from faceblur_lite.state import SqliteControlPlane
from faceblur_lite.testmedia import build_video
from faceblur_lite.worker import worker_loop

pytestmark = requires_assets


def test_worker_loop_full_single_chunk(tmp_path, move_video):
    """单 chunk 直通：处理 → 原子认领 merge → 合并 → qc_wait。"""
    cp = SqliteControlPlane(str(tmp_path / "w.db"))
    cp.init_db()
    final = str(tmp_path / "final.mp4")
    cp.submit_video(move_video[0], final,
                    [(0, move_video[0], str(tmp_path / "o0.mp4"))])
    stats = worker_loop(cp, "w0", KernelParams(det_every=2),
                        idle_exit_s=0.3, poll_s=0.05, ttl_s=60)
    assert stats == {"worker": "w0", "chunks_done": 1}
    assert cp.video_overview()[0]["status"] == "qc_wait"
    assert Path(final).exists()


def test_pipeline_end_to_end(tmp_path, move_video):
    """6s 视频 segment 2s → 多 chunk 多 Worker 并行 → done + qc 通过。"""
    out = str(tmp_path / "e2e.mp4")
    res = run_pipeline(move_video[0], out, str(tmp_path / "wd"),
                       workers=2, segment_s=2.0,
                       params=KernelParams(det_every=2))
    assert res.status == "done"
    assert res.qc.passed
    assert res.chunk_count >= 2
    assert abs(probe(out).duration_s - 6.0) < 1.0


def test_batch_multi_video_shared_pool(tmp_path, move_video, face_img):
    """R10：一个长视频（切 3 片）+ 一个短视频（快路径）共用 Worker 池。"""
    short = str(tmp_path / "short.mp4")
    build_video(short, face_img, duration_s=2.0)
    results = run_batch([move_video[0], short], str(tmp_path / "out"),
                        str(tmp_path / "wd"), workers=2, segment_s=2.0,
                        params=KernelParams(det_every=2))
    assert [r.status for r in results] == ["done", "done"]
    assert all(r.qc.passed for r in results)
    assert results[0].chunk_count >= 2      # 长视频切片并行
    assert results[1].chunk_count == 1      # 短视频快路径不切
    assert Path(results[0].output_path).exists()
    assert Path(results[1].output_path).exists()


def test_batch_failure_isolation(tmp_path, move_video):
    """R10：坏输入只废自己，好视频照常 done。"""
    results = run_batch([move_video[0], "/nonexistent.mp4"],
                        str(tmp_path / "out"), str(tmp_path / "wd"),
                        workers=2, segment_s=2.0,
                        params=KernelParams(det_every=2))
    by_status = {r.input_path: r.status for r in results}
    assert by_status[move_video[0]] == "done"
    assert by_status["/nonexistent.mp4"] == "failed"
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_pipeline.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现 worker + orchestrator**

`src/faceblur_lite/worker.py`：

```python
"""数据面 Worker：无状态循环——认领任务→执行算子→事务回写。可随时 kill。

只依赖 ControlPlane 协议（依赖注入）：换 Go 控制面/Redis 控制面时
本文件零改动。状态只存在于控制面；Worker 死亡由 lease 回收兜底。
"""
from __future__ import annotations

import time

from faceblur_lite.control import ControlPlane
from faceblur_lite.kernel import KernelParams, process_video
from faceblur_lite.merger import merge_chunks


def worker_loop(cp: ControlPlane, worker_id: str,
                params: KernelParams | None = None,
                poll_s: float = 0.5, idle_exit_s: float = 15.0,
                ttl_s: float = 900.0) -> dict:
    """空闲 idle_exit_s 后退出（demo 级优雅收尾）；idle_exit_s=0 常驻
    永不退出（独立 Worker/容器部署模式，R12）。返回统计。"""
    p = params or KernelParams()
    done, idle = 0, 0.0
    while True:
        task = cp.claim_chunk(worker_id, ttl_s)
        if task is None:
            idle += poll_s
            if idle_exit_s and idle >= idle_exit_s:
                return {"worker": worker_id, "chunks_done": done}
            time.sleep(poll_s)
            continue
        idle = 0.0
        try:
            rep = process_video(task["input_path"], task["output_path"], p)
            merge_claimed = cp.complete_chunk(task["id"], {
                "frames": rep.frames, "detections": rep.detections,
                "predicted_frames": rep.predicted_frames,
                "segments": rep.segments, "wall_s": rep.wall_s})
            if merge_claimed:      # 原子 Join：最后完成者负责合并
                video = cp.get_video(task["video_id"])
                merge_chunks(cp.chunk_outputs(task["video_id"]),
                             video["output_path"])
                cp.complete_merge(task["video_id"], video["output_path"])
            done += 1
        except Exception as e:      # noqa: BLE001 —— 单 chunk 失败不拖死 Worker
            cp.fail_chunk(task["id"], f"{type(e).__name__}: {e}")
```

`src/faceblur_lite/orchestrator.py`：

```python
"""控制面编排器：逐视频切片+提交（失败隔离）→ 共享 Worker 池 →
lease 巡检 → drain 后逐视频质检。R3/R10。"""
from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import dataclass
from pathlib import Path

from faceblur_lite.kernel import KernelParams
from faceblur_lite.qc import QCReport, qc_video
from faceblur_lite.splitter import split_video
from faceblur_lite.state import SqliteControlPlane
from faceblur_lite.worker import worker_loop


@dataclass
class PipelineResult:
    video_id: str
    input_path: str
    output_path: str
    status: str
    qc: QCReport | None
    wall_s: float
    chunk_count: int
    error: str | None = None


def _output_for(input_path: str, outdir: str) -> str:
    Path(outdir).mkdir(parents=True, exist_ok=True)
    return str(Path(outdir) / f"{Path(input_path).stem}_blurred.mp4")


def _orchestrate(specs: list[tuple[str, str]], workdir: str,
                 workers: int, segment_s: float,
                 params: KernelParams | None,
                 qc_sample_fps: float,
                 cp_url: str | None = None) -> list[PipelineResult]:
    t0 = time.time()
    p = params or KernelParams()
    if cp_url:      # Go 控制面模式（Task 12）：注入 HTTP 适配器，其余不变
        from faceblur_lite.remote import HttpControlPlane
        cp = HttpControlPlane(cp_url)
    else:           # 默认：SQLite 内嵌控制面
        cp = SqliteControlPlane(str(Path(workdir) / "pipeline.db"))
    cp.init_db()

    # 1) 逐视频切片+提交——失败隔离：单视频失败记结果，批次继续（R10）
    submitted: list[tuple[str, int]] = []   # (video_id, chunk_count)
    submit_errors: list[tuple[str, str, str]] = []  # (input, output, error)
    for k, (inp, outp) in enumerate(specs):
        try:
            chunk_dir = Path(workdir) / "chunks" / f"{k:02d}_{Path(inp).stem}"
            files = split_video(inp, str(chunk_dir), segment_s)
            items = [(i, f, str(chunk_dir / f"blurred_{i:03d}.mp4"))
                     for i, f in enumerate(files)]
            vid = cp.submit_video(inp, outp, items)
            submitted.append((vid, len(items)))
        except Exception as e:              # noqa: BLE001
            submit_errors.append((inp, outp, f"{type(e).__name__}: {e}"))

    # 2) 共享 Worker 池：一个池消化所有视频的 chunk（队列天然交错）
    procs = []
    for n in range(workers):
        proc = mp.Process(target=worker_loop, args=(cp, f"w{n}", p),
                          kwargs={"idle_exit_s": 3.0},
                          daemon=True, name=f"worker-{n}")
        proc.start()
        procs.append(proc)

    # 3) 巡检：lease 回收；单视频 failed 不终止批次（失败隔离）
    while any(pr.is_alive() for pr in procs):
        cp.reclaim_expired()
        time.sleep(0.5)
    for pr in procs:
        pr.join()

    # 4) drain 后逐视频质检收尾
    results: list[PipelineResult] = []
    for vid, nchunks in submitted:
        video = cp.get_video(vid)
        wall = time.time() - t0
        if video["status"] == "qc_wait":
            rep = qc_video(video["output_path"], sample_fps=qc_sample_fps)
            status = "done" if rep.passed else "qc_failed"
            cp.finish_video(vid, status)
            results.append(PipelineResult(
                vid, video["input_path"], video["output_path"],
                status, rep, wall, nchunks))
        else:   # failed / Worker 池退出时仍未完成（如回收发生在退出边缘）
            results.append(PipelineResult(
                vid, video["input_path"], video["output_path"],
                video["status"], None, wall, nchunks,
                video["error"] or "Worker 池退出时任务未到 qc_wait"))
    for inp, outp, err in submit_errors:
        results.append(PipelineResult(
            "", inp, outp, "failed", None, time.time() - t0, 0, err))
    return results


def run_batch(inputs: list[str], outdir: str, workdir: str,
              workers: int = 2, segment_s: float = 30.0,
              params: KernelParams | None = None,
              qc_sample_fps: float = 1.0,
              cp_url: str | None = None) -> list[PipelineResult]:
    """批量：输出到 outdir/<stem>_blurred.mp4。R10。"""
    specs = [(inp, _output_for(inp, outdir)) for inp in inputs]
    return _orchestrate(specs, workdir, workers, segment_s,
                        params, qc_sample_fps, cp_url)


def run_pipeline(input_path: str, output_path: str, workdir: str,
                 workers: int = 2, segment_s: float = 30.0,
                 params: KernelParams | None = None,
                 qc_sample_fps: float = 1.0,
                 cp_url: str | None = None) -> PipelineResult:
    """单视频便捷封装（显式输出路径）。"""
    return _orchestrate([(input_path, output_path)], workdir, workers,
                        segment_s, params, qc_sample_fps, cp_url)[0]
```

- [ ] **Step 4: 运行确认通过**

```bash
./venv/bin/python -m pytest tests/test_pipeline.py -v
```

Expected: `4 passed`。注意 macOS 上 `mp.Process` 默认 spawn 启动方式：`worker_loop` 是模块级函数、`SqliteControlPlane` 实例只含 db_path 字符串（可 pickle）——传参安全。

- [ ] **Step 5: 全量回归 + Commit**

```bash
./venv/bin/python -m pytest -v
```

Expected: 此前所有测试全绿。

```bash
git add src/faceblur_lite/worker.py src/faceblur_lite/orchestrator.py tests/test_pipeline.py
git commit -m "feat: 数据面 Worker（依赖注入控制面）+ 批量编排（共享池/失败隔离）" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 11: CLI + README + 详细设计文档 + 平台映射文档 + 批量冒烟

**Files:**
- Create: `src/faceblur_lite/cli.py`、`src/faceblur_lite/stats.py`、`README.md`、`docs/platform-mapping.md`
- Modify: `docs/design.md`（回填实测数字）
- Test: `tests/test_cli.py`、`tests/test_stats.py`

**Interfaces:**
- Consumes: 前面全部模块（stats 只读控制面库文件）
- Produces: `faceblur-lite` 命令（子命令 `single / pipeline / worker / split / merge / qc / stats / demo-video / status`）；`cli.main(argv: list[str] | None = None) -> int`
  - `stats.py`（R14 最小观测出口，**只读旁路**）：`collect_stats(db_path) -> dict`（videos/chunks 计数、排队 P50/P95＝started−enqueued、处理 P50/P95＝finished−started、吞吐 chunks/min、失败率、retried）/ `format_stats(dict) -> str`。零 schema 变更（时间戳/attempt 已入库）；**不进 ControlPlane 契约**——观测读不污染任务状态契约；SQLite WAL 允许并发只读（gocp 模式同样可直读其库文件）
  - `worker`：**独立数据面 Worker 常驻入口**（R12）——`--cp-url`（连 gocp）或 `--db`（本地 SQLite 跨进程）二选一，`--idle-exit-s 0` 常驻；容器/跨机/多终端扩容的部署单元
  - `split` / `merge`：**算子 CLI 子命令化**（R13）——四个数据面算子（split/single/merge/qc）全部可独立命令行调用，通用 DAG 引擎的节点模板可原样引用

- [ ] **Step 1: 写失败测试**

`tests/test_cli.py`：

```python
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
```

`tests/test_stats.py`（R14 观测出口）：

```python
from conftest import requires_assets

from faceblur_lite.kernel import KernelParams
from faceblur_lite.orchestrator import run_batch
from faceblur_lite.stats import collect_stats, format_stats
from faceblur_lite.state import SqliteControlPlane


@requires_assets
def test_stats_after_pipeline(tmp_path, move_video):
    run_batch([move_video[0]], str(tmp_path / "out"), str(tmp_path / "wd"),
              workers=2, segment_s=2.0, params=KernelParams(det_every=2))
    s = collect_stats(str(tmp_path / "wd" / "pipeline.db"))
    assert s["videos"].get("done") == 1
    assert s["chunks_total"] >= 2
    assert s["chunks_by_status"].get("done") == s["chunks_total"]
    assert 0 <= s["queue_s"]["p50"] <= s["queue_s"]["p95"]
    assert 0 <= s["proc_s"]["p50"] <= s["proc_s"]["p95"]
    assert s["throughput_per_min"] > 0
    assert s["fail_rate"] == 0.0
    assert "chunks/min" in format_stats(s)


def test_stats_empty_db(tmp_path):
    """空库零除/空列表安全（无资产依赖）。"""
    cp = SqliteControlPlane(str(tmp_path / "e.db"))
    cp.init_db()
    s = collect_stats(str(tmp_path / "e.db"))
    assert s["chunks_total"] == 0
    assert s["queue_s"]["p50"] == 0.0
    assert s["throughput_per_min"] == 0.0
    assert s["fail_rate"] == 0.0
```

- [ ] **Step 2: 运行确认失败，然后实现 CLI + 观测出口 stats.py**

```bash
./venv/bin/python -m pytest tests/test_cli.py -v
```

Expected: FAIL（`faceblur_lite.cli` 不存在）。

`src/faceblur_lite/cli.py`：

```python
"""入口：single（单进程内核）/ pipeline（控制面+数据面并行）/
qc（输出域复检）/ demo-video（合成测试视频）/ status（管线库状态）。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from faceblur_lite import __version__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="faceblur-lite")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("single", help="单进程：一个视频 → 一个脱敏视频")
    s.add_argument("input")
    s.add_argument("output")
    s.add_argument("--det-every", type=int, default=3)
    s.add_argument("--det-conf", type=float, default=0.3)
    s.add_argument("--margin", type=float, default=0.15)

    p = sub.add_parser(
        "pipeline", help="批量分片并行：控制面 + N 个数据面 Worker（R10）")
    p.add_argument("inputs", nargs="+", help="一个或多个输入视频")
    p.add_argument("--outdir", default="outputs",
                   help="输出目录（<stem>_blurred.mp4）")
    p.add_argument("--workdir", default="work-pipeline")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--segment", type=float, default=30.0)
    p.add_argument("--det-every", type=int, default=3)
    p.add_argument("--cp-url", default=None,
                   help="Go 控制面服务地址（如 http://127.0.0.1:8080）；"
                        "默认 SQLite 内嵌控制面")

    wk = sub.add_parser(
        "worker", help="独立数据面 Worker（常驻；容器/跨机/多终端扩容，R12）")
    wk.add_argument("--cp-url", default=None,
                    help="控制面服务地址（与 --db 二选一）")
    wk.add_argument("--db", default=None,
                    help="SQLite 控制面库路径（与 --cp-url 二选一，WAL 跨进程安全）")
    wk.add_argument("--idle-exit-s", type=float, default=0,
                    help="空闲多久退出；0=常驻（默认，容器部署模式）")
    wk.add_argument("--det-every", type=int, default=3)

    sp = sub.add_parser("split", help="切片算子（独立调用；DAG 节点模板用，R13）")
    sp.add_argument("input")
    sp.add_argument("outdir")
    sp.add_argument("--segment", type=float, default=30.0)

    mg = sub.add_parser("merge", help="合并算子（独立调用；DAG 节点模板用，R13）")
    mg.add_argument("output")
    mg.add_argument("chunks", nargs="+")

    q = sub.add_parser("qc", help="输出域抽帧复检")
    q.add_argument("video")
    q.add_argument("--sample-fps", type=float, default=1.0)

    d = sub.add_parser("demo-video", help="生成合成测试视频")
    d.add_argument("output")
    d.add_argument("--scenario", choices=["move", "occlude"], default="move")
    d.add_argument("--duration", type=float, default=6.0)

    st = sub.add_parser("status", help="查看管线库状态")
    st.add_argument("db")

    stat = sub.add_parser(
        "stats", help="最小观测出口：排队/处理分位、吞吐、失败率（R14）")
    stat.add_argument("db")
    stat.add_argument("--json", action="store_true", help="JSON 输出")

    args = ap.parse_args(argv)

    if args.cmd == "single":
        from faceblur_lite.kernel import KernelParams, process_video
        rep = process_video(
            args.input, args.output,
            KernelParams(det_every=args.det_every, det_conf=args.det_conf,
                         margin=args.margin))
        print(json.dumps(rep.__dict__, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "pipeline":
        from faceblur_lite.kernel import KernelParams
        from faceblur_lite.orchestrator import run_batch
        results = run_batch(
            args.inputs, args.outdir, args.workdir,
            workers=args.workers, segment_s=args.segment,
            params=KernelParams(det_every=args.det_every),
            cp_url=args.cp_url)
        print(json.dumps([{
            "video_id": r.video_id, "status": r.status,
            "qc_passed": r.qc.passed if r.qc else None,
            "violations": ([v.__dict__ for v in r.qc.violations]
                           if r.qc else None),
            "chunk_count": r.chunk_count,
            "wall_s": round(r.wall_s, 2),
            "output": r.output_path,
            "error": r.error} for r in results],
            ensure_ascii=False, indent=2))
        return 0 if all(r.status == "done" for r in results) else 1

    if args.cmd == "worker":
        from faceblur_lite.kernel import KernelParams
        from faceblur_lite.worker import worker_loop
        if not args.cp_url and not args.db:
            print("worker 需要 --cp-url 或 --db 之一", file=sys.stderr)
            return 2
        if args.cp_url:
            from faceblur_lite.remote import HttpControlPlane
            cp = HttpControlPlane(args.cp_url)
        else:
            from faceblur_lite.state import SqliteControlPlane
            cp = SqliteControlPlane(args.db)
        wid = f"w-{os.environ.get('HOSTNAME', os.getpid())}"
        worker_loop(cp, wid, KernelParams(det_every=args.det_every),
                    idle_exit_s=args.idle_exit_s)
        return 0

    if args.cmd == "split":
        from faceblur_lite.splitter import split_video
        for path in split_video(args.input, args.outdir,
                                segment_s=args.segment):
            print(path)
        return 0

    if args.cmd == "merge":
        from faceblur_lite.merger import merge_chunks
        print(merge_chunks(args.chunks, args.output))
        return 0

    if args.cmd == "qc":
        from faceblur_lite.qc import qc_video
        rep = qc_video(args.video, sample_fps=args.sample_fps)
        print(json.dumps({
            "passed": rep.passed, "sampled": rep.sampled_frames,
            "violations": [v.__dict__ for v in rep.violations]},
            ensure_ascii=False, indent=2))
        return 0 if rep.passed else 1

    if args.cmd == "demo-video":
        from faceblur_lite.testmedia import build_video, load_face
        root = Path(__file__).resolve().parents[2]
        face_path = root / "assets" / "lena.jpg"
        if not face_path.exists():
            print("缺少资产：先运行 scripts/download_assets.sh",
                  file=sys.stderr)
            return 1
        build_video(args.output, load_face(str(face_path)),
                    scenario=args.scenario, duration_s=args.duration)
        print(f"已生成 {args.output}")
        return 0

    if args.cmd == "stats":
        from faceblur_lite.stats import collect_stats, format_stats
        s = collect_stats(args.db)
        print(json.dumps(s, ensure_ascii=False, indent=2) if args.json
              else format_stats(s))
        return 0

    if args.cmd == "status":
        from faceblur_lite.state import SqliteControlPlane
        for v in SqliteControlPlane(args.db).video_overview():
            print(json.dumps(v, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

`src/faceblur_lite/stats.py`（R14 最小观测出口——只读旁路）：

```python
"""最小观测出口（只读旁路，R14）：从控制面库读时间戳/attempt，
输出排队/处理分位、吞吐、失败率、重试计数。

设计要点：不进 ControlPlane 契约——观测读不污染任务状态契约；
SQLite WAL 允许并发只读（gocp 模式同样可直读其库文件）。
血缘 / 时序化（exporter/大盘/告警）为平台期演进（开放细化项 9）。
"""
from __future__ import annotations

import sqlite3


def _pct(values: list[float], p: float) -> float:
    """最近邻分位（demo 级）：空列表返回 0.0。"""
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
    return round(s[k], 3)


def collect_stats(db_path: str) -> dict:
    """只读聚合 videos/chunks。排队=started-enqueued；处理=finished-started。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        chunks = con.execute(
            "SELECT status, attempt, enqueued_at, started_at, finished_at "
            "FROM chunks").fetchall()
        videos = con.execute("SELECT status FROM videos").fetchall()
    finally:
        con.close()

    by_status: dict[str, int] = {}
    for r in chunks:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    queue_s = [r["started_at"] - r["enqueued_at"] for r in chunks
               if r["started_at"] is not None]
    proc_s = [r["finished_at"] - r["started_at"] for r in chunks
              if r["started_at"] is not None and r["finished_at"] is not None]
    fin = [r["finished_at"] for r in chunks if r["finished_at"] is not None]
    enq = [r["enqueued_at"] for r in chunks if r["enqueued_at"] is not None]
    wall = (max(fin) - min(enq)) if fin and enq else 0.0
    done = by_status.get("done", 0)
    return {
        "videos": {s: sum(1 for v in videos if v["status"] == s)
                   for s in {v["status"] for v in videos}},
        "chunks_total": len(chunks),
        "chunks_by_status": by_status,
        "retried": sum(1 for r in chunks if r["attempt"] > 0),
        "queue_s": {"p50": _pct(queue_s, 50), "p95": _pct(queue_s, 95)},
        "proc_s": {"p50": _pct(proc_s, 50), "p95": _pct(proc_s, 95)},
        "throughput_per_min": round(done / (wall / 60), 2) if wall > 0 else 0.0,
        "fail_rate": round(by_status.get("failed", 0) / len(chunks), 4)
                     if chunks else 0.0,
    }


def format_stats(s: dict) -> str:
    v = " ".join(f"{k}={n}" for k, n in sorted(s["videos"].items())) or "-"
    c = " ".join(f"{k}={n}" for k, n in sorted(s["chunks_by_status"].items()))
    return (
        f"videos: {v}\n"
        f"chunks: {s['chunks_total']} {c} retried={s['retried']}\n"
        f"queue_s: p50={s['queue_s']['p50']} p95={s['queue_s']['p95']}\n"
        f"proc_s:  p50={s['proc_s']['p50']} p95={s['proc_s']['p95']}\n"
        f"throughput: {s['throughput_per_min']} chunks/min\n"
        f"fail_rate: {s['fail_rate']:.1%}")
```

- [ ] **Step 3: 运行确认通过 + 全量回归**

```bash
./venv/bin/python -m pytest tests/test_cli.py -v
./venv/bin/python -m pytest -v
```

Expected: CLI 测试 2 passed、stats 测试 2 passed；全量绿。

- [ ] **Step 4: 写 README.md**

内容必须包含（结构如下，正文完整写）：

```markdown
# face-blur-lite

轻量视频人脸脱敏管线——控制面/数据面分离的最小实现。

## 快速开始
（下载资产 → venv 安装 → demo-video 生成测试视频 → single 跑通 →
pipeline 分片并行 → qc 复检，全部给出可复制命令）

## 架构
（控制面/数据面 ASCII 图：SQLite 状态机+编排器 = 控制面；
无状态 Worker 进程 + 算子内核（kernel/splitter/merger/qc）= 数据面；
scatter-gather 流程图 split→blur×N→merge→qc）

## 设计决策
（R1~R9 表，同本方案 Context；阈值分域声明；宁可多糊；
跟踪器=下一帧预测+遮挡外推；音轨 copy 不重编）

## 指标输出
（`faceblur-lite stats work-pipeline/pipeline.db` 示例 + 字段解释：
排队/处理 P50/P95、吞吐 chunks/min、失败率、retried——只读旁路，
观测不进任务状态契约；冒烟实测数字回填此处）

## 已知限制与演进
（见本方案"开放细化项"）
```

- [ ] **Step 5: 校对 docs/design.md（设计阶段已创建——回填实测数字）**

`docs/design.md` 已在设计阶段创建（四大视图详细设计，对外交付版）。本步校对
其与最终实现的一致性（接口名/参数默认值/状态机），并把 §8 验收指标回填为
实测值（加速比、批量摊销、qc 结果、kill -9 恢复表现）。

- [ ] **Step 6: 写 docs/platform-mapping.md**

内容（胜算/LAS/百舸对照——依据 2026-09 调研）：

```markdown
# 业界工作流平台映射

三个平台分两层：胜算/火山 LAS 是数据层工作流（数据怎么被加工）；
百舸工作流是算力层工作流（加工任务怎么被调度执行）。

| 本项目组件 | 百舸工作流 | 火山 LAS | 百度胜算 |
|---|---|---|---|
| face-blur 算子（视频→脱敏视频） | 工作流节点/任务 | 数据处理任务 | 算子能力 |
| split→blur×N→merge→qc | DAG 组件化编排 | 工作流添加任务编排 | 数据处理管线 |
| 无状态 Worker + 队列 | 任务式作业（按需挂载/释放） | LAS 托管调度 | 平台托管 |
| gocp（Go 控制面服务）+ Python 算子 Worker | 平台调度控制面 + 算法任务容器（业界标准分工） | 同左 | 同左 |
| 原子 Join 门控 | DAG 依赖边（平台原生保证） | 同左 | 同左 |
| 状态机+重试+lease | 任务生命周期管理+自动重试 | 平台托管 | 平台托管 |
| stats 命令（排队/处理分位、吞吐、失败率） | 可观测：metrics-server / Prometheus | 监控大盘 | 平台指标 |
| 本地磁盘/输出文件 | BOS/并行文件系统 | 数据湖存储 | 多模态数据管理 |
| qc 抽帧复检算子 | DAG 中的质检节点 | 数据质量任务 | 算子 |

再加一段"通用引擎能力对照"（Airflow/Argo/Temporal/K8s Job 等通用形态
vs 本项目：调度语义、容错、可观测各自怎么做法，引用本方案 Context 的
审查表结论——骨架同构，差距在平台配套）。

控制面/数据面演进：SQLite 状态库+调度函数 → 平台工作流引擎；
Worker 子进程 → Docker 任务容器（百舸任务式）。上平台后队列/Join/lease/
状态机全部被平台收编，保留的是算子内核、质检判定逻辑、阈值与参数策略。

具身数据链路定位：采集→**脱敏（本项目）**→入湖→标注→训练；
本管线是具身数据合规的第一道工序。
```

- [ ] **Step 7: 批量冒烟——3 个视频一次提交（R10 手动验收，不进自动化测试）**

```bash
mkdir -p outputs
# 批次 = 5min 遮挡视频 + 30s 短视频 + 90s 遮挡视频（长短混合、场景混合）
./venv/bin/faceblur-lite demo-video outputs/a_5min.mp4 --duration 300 --scenario occlude
./venv/bin/faceblur-lite demo-video outputs/b_30s.mp4 --duration 30 --scenario move
./venv/bin/faceblur-lite demo-video outputs/c_90s.mp4 --duration 90 --scenario occlude

# 基线：5min 单视频单进程串行
time ./venv/bin/faceblur-lite single outputs/a_5min.mp4 outputs/a_5min_single.mp4

# 批量：3 个视频一次提交，共享 Worker 池（长视频切片并行 + 多视频交错）
time ./venv/bin/faceblur-lite pipeline outputs/a_5min.mp4 outputs/b_30s.mp4 outputs/c_90s.mp4 \
  --outdir outputs --workdir work-smoke --workers 3 --segment 30

# 观测出口：批量指标（数字进 README"指标输出"节，R14）
./venv/bin/faceblur-lite stats work-smoke/pipeline.db

# 逐个复检
./venv/bin/faceblur-lite qc outputs/a_5min_blurred.mp4
./venv/bin/faceblur-lite qc outputs/b_30s_blurred.mp4
./venv/bin/faceblur-lite qc outputs/c_90s_blurred.mp4

# （可选，需 go ≥1.22）Go 控制面模式跑同一批次——业界形态：
# 平台控制面 Go 服务 + Python 算子 Worker，过同一份契约测试
(cd gocp && go build -o /tmp/gocp .)
/tmp/gocp /tmp/go_cp.db 127.0.0.1:8080 &
time ./venv/bin/faceblur-lite pipeline outputs/a_5min.mp4 outputs/b_30s.mp4 outputs/c_90s.mp4 \
  --outdir outputs/go-mode --workdir work-smoke-go --workers 3 --segment 30 \
  --cp-url http://127.0.0.1:8080
kill %1
```

Expected: pipeline 输出 3 个结果全部 `status: done`、qc 全通过；把
a_5min 的 single 串行 wall 时间 vs 批量 pipeline wall 时间对比（另两个视频
基本免费搭车）写进 README（加速比 + 批量摊销）；stats 输出（排队/处理
分位、吞吐、失败率）一并记入 README"指标输出"节。若手头有真实采集视频，
替换 demo 视频再跑一轮，结果一并记录。

- [ ] **Step 8: Commit**

```bash
git add src/faceblur_lite/cli.py src/faceblur_lite/stats.py tests/test_cli.py tests/test_stats.py README.md docs/design.md docs/platform-mapping.md
git commit -m "feat: CLI 入口 + 最小观测出口 stats + README + 详细设计/平台映射文档" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 12: gocp/ + remote.py — Go 控制面服务 + HTTP 适配（R7 业界形态）

**Files:**
- Create: `gocp/go.mod`、`gocp/main.go`、`src/faceblur_lite/remote.py`、`tests/test_go_cp.py`
- Modify: `tests/conftest.py`（加 go fixtures）、`tests/test_contract.py`（参数化双实现）、`docs/control-plane-contract.md`（追加 HTTP API 映射表）

**Interfaces:**
- Consumes: `ControlPlane` 协议（Task 7）、契约测试场景（Task 7）、`cp_url` 注入点（Task 10）
- Produces:
  - `gocp` 二进制：`gocp <db_path> <listen_addr>`——契约的 Go/HTTP 实现；11 个端点 ↔ 11 个协议方法，同 schema 同事务语义（`_txlock=immediate` 对应 `BEGIN IMMEDIATE`）
  - `HttpControlPlane(base_url, timeout_s=60.0)`（`remote.py`）：`ControlPlane` 协议的 HTTP 客户端；stdlib `urllib` 零第三方依赖；实例只含 `base_url` 字符串（可 pickle，spawn 传参安全）
  - conftest：`go_bin`（session 级构建一次，无 go 工具链则 skip）、`go_cp_url`（**每测试全新服务+空库**，测试间零干扰）
  - `tests/test_contract.py` 参数化 `["sqlite", "go"]` 双跑同一场景——**契约是闸门，两种实现过同一份测试**
  - JSON 语义约定：响应键 = snake_case 列名；可空列显式 `null`（对应 Python `dict(row)` 的 None 语义，不缺键）；claim 无任务返回 JSON `null` → Python `None`

- [ ] **Step 1: 写失败测试 + 契约参数化 + go fixtures**

`tests/conftest.py` **追加**：

```python
import socket
import subprocess
import time
import urllib.request


@pytest.fixture(scope="session")
def go_bin(tmp_path_factory):
    """构建 gocp 二进制（session 只构建一次）；无 go 工具链则跳过。"""
    if shutil.which("go") is None:
        pytest.skip("需要 go >= 1.22 工具链（brew install go）")
    root = Path(__file__).resolve().parents[1]   # tests/ 上一级=项目根（同 Task 3 修正）
    bin_path = tmp_path_factory.mktemp("gobuild") / "gocp"
    subprocess.run(["go", "build", "-o", str(bin_path), "."],
                   cwd=root / "gocp", check=True)
    return bin_path


@pytest.fixture()
def go_cp_url(go_bin, tmp_path):
    """每个测试一个全新 gocp 服务 + 空库（测试间零干扰）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [str(go_bin), str(tmp_path / "go.db"), f"127.0.0.1:{port}"])
    try:
        for _ in range(50):            # 等服务就绪
            try:
                urllib.request.urlopen(f"{base}/videos", timeout=0.2).read()
                break
            except Exception:
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait()
```

`tests/test_contract.py` **替换 cp fixture**（其余 5 个测试零改动——这就是契约闸门的意义）：

```python
import pytest

from faceblur_lite.state import SqliteControlPlane


@pytest.fixture(params=["sqlite", "go"])
def cp(request, tmp_path):
    if request.param == "sqlite":
        cp_ = SqliteControlPlane(str(tmp_path / "s.db"))
    else:
        # getfixturevalue 惰性求值：sqlite 分支不触碰 go 工具链
        from faceblur_lite.remote import HttpControlPlane
        cp_ = HttpControlPlane(request.getfixturevalue("go_cp_url"))
    cp_.init_db()
    return cp_
```

`tests/test_go_cp.py`（新建）：

```python
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
    assert cp.complete_chunk(t["id"], {}) is True   # 单 chunk：完成即认领 merge
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
```

- [ ] **Step 2: 运行确认失败**

```bash
./venv/bin/python -m pytest tests/test_go_cp.py tests/test_contract.py -v
```

Expected: FAIL（`ModuleNotFoundError: No module named 'faceblur_lite.remote'`）；无 go 工具链的环境上 go 参数化项 SKIP（sqlite 项仍 FAIL 于 remote 缺失）。

- [ ] **Step 3: 实现 remote.py**

`src/faceblur_lite/remote.py`：

```python
"""HttpControlPlane——控制面契约的 HTTP 客户端，对接 gocp（Go 服务）。

stdlib urllib，零第三方依赖；实例只含 base_url/timeout（可 pickle，
spawn 传参安全）。JSON 语义见 docs/control-plane-contract.md §6。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class HttpControlPlane:
    def __init__(self, base_url: str, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _call(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"控制面 {method} {path} 失败: "
                               f"{e.code} {e.read().decode()}") from e

    def init_db(self) -> None:
        self._call("POST", "/init")   # schema 由服务启动时创建；幂等

    def submit_video(self, input_path: str, final_output: str,
                     chunk_inputs: list[tuple[int, str, str]]) -> str:
        return self._call("POST", "/videos", {
            "input_path": input_path, "final_output": final_output,
            "chunk_inputs": [list(t) for t in chunk_inputs]})["video_id"]

    def claim_chunk(self, worker_id: str, ttl_s: float) -> dict | None:
        return self._call("POST", "/chunks/claim",
                          {"worker_id": worker_id, "ttl_s": ttl_s})

    def complete_chunk(self, chunk_id: str, metrics: dict) -> bool:
        return self._call("POST", f"/chunks/{chunk_id}/complete",
                          {"metrics": metrics})["merge_claimed"]

    def complete_merge(self, video_id: str, merged_path: str) -> None:
        self._call("POST", f"/videos/{video_id}/merge_complete",
                   {"merged_path": merged_path})

    def fail_chunk(self, chunk_id: str, error: str,
                   max_attempts: int = 3) -> None:
        self._call("POST", f"/chunks/{chunk_id}/fail",
                   {"error": error, "max_attempts": max_attempts})

    def reclaim_expired(self, max_attempts: int = 3) -> int:
        return self._call("POST", "/maintenance/reclaim",
                          {"max_attempts": max_attempts})["reclaimed"]

    def get_video(self, video_id: str) -> dict:
        return self._call("GET", f"/videos/{video_id}")

    def chunk_outputs(self, video_id: str) -> list[str]:
        return self._call(
            "GET", f"/videos/{video_id}/chunk_outputs")["outputs"]

    def video_overview(self) -> list[dict]:
        return self._call("GET", "/videos")

    def finish_video(self, video_id: str, status: str) -> None:
        self._call("POST", f"/videos/{video_id}/finish", {"status": status})
```

- [ ] **Step 4: 实现 gocp（Go 控制面服务）**

```bash
cd gocp
go mod init gocp
go get modernc.org/sqlite@latest
go mod tidy
```

`gocp/main.go`：

```go
// gocp/main.go
//
// Go 控制面服务——docs/control-plane-contract.md 契约的 HTTP 形态。
// 业界分工复刻：平台控制面 Go + 算法数据面 Python（K8s/Temporal 同款）。
// 轻量约束：单文件、stdlib net/http + 唯一依赖 modernc.org/sqlite
// （纯 Go 无 CGO）、无框架/无 gRPC/无容器。与 Python 端
// SqliteControlPlane 同 schema、同事务语义，过同一份契约测试。
//
// 用法：gocp <db_path> <listen_addr>   默认 pipeline.db 127.0.0.1:8080
package main

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"time"

	_ "modernc.org/sqlite"
)

var db *sql.DB

// ---- 小工具 ----

func newID() string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func nowF() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	w.WriteHeader(code)
	writeJSON(w, map[string]string{"error": msg})
}

// 契约 §6：可空列必须显式置 nil（Python dict[row] 的 None 语义，不缺键）
func setNull(m map[string]any, k string, v sql.NullString) {
	if v.Valid {
		m[k] = v.String
	} else {
		m[k] = nil
	}
}

func setNullF(m map[string]any, k string, v sql.NullFloat64) {
	if v.Valid {
		m[k] = v.Float64
	} else {
		m[k] = nil
	}
}

// scanChunk：SELECT * 的 chunks 行 → map（键名 = 列名，与 Python 一致）
func scanChunk(scan func(dest ...any) error) (map[string]any, error) {
	var id, videoID, in, out, status string
	var idx, attempt int
	var workerID, metrics, errStr sql.NullString
	var leaseUntil, enqueuedAt, startedAt, finishedAt sql.NullFloat64
	if err := scan(&id, &videoID, &idx, &in, &out, &status, &attempt,
		&workerID, &leaseUntil, &metrics, &enqueuedAt, &startedAt,
		&finishedAt, &errStr); err != nil {
		return nil, err
	}
	m := map[string]any{"id": id, "video_id": videoID, "idx": idx,
		"input_path": in, "output_path": out, "status": status,
		"attempt": attempt, "enqueued_at": enqueuedAt.Float64}
	setNull(m, "worker_id", workerID)
	setNull(m, "metrics", metrics)
	setNull(m, "error", errStr)
	setNullF(m, "lease_until", leaseUntil)
	setNullF(m, "started_at", startedAt)
	setNullF(m, "finished_at", finishedAt)
	return m, nil
}

func getVideo(id string) (map[string]any, error) {
	var in, out, status string
	var merged, errStr sql.NullString
	var createdAt float64
	var finishedAt sql.NullFloat64
	err := db.QueryRow(`SELECT input_path, output_path, merged_path,
		status, error, created_at, finished_at FROM videos WHERE id=?`, id).
		Scan(&in, &out, &merged, &status, &errStr, &createdAt, &finishedAt)
	if err != nil {
		return nil, err
	}
	m := map[string]any{"id": id, "input_path": in, "output_path": out,
		"status": status, "created_at": createdAt}
	setNull(m, "merged_path", merged)
	setNull(m, "error", errStr)
	setNullF(m, "finished_at", finishedAt)
	return m, nil
}

// ---- schema：与 state.py 逐字一致（契约 §1） ----

const ddl = `
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY,
  input_path TEXT NOT NULL,
  output_path TEXT NOT NULL,
  merged_path TEXT,
  status TEXT NOT NULL,
  error TEXT,
  created_at REAL NOT NULL,
  finished_at REAL);
CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(id),
  idx INTEGER NOT NULL,
  input_path TEXT NOT NULL,
  output_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT,
  lease_until REAL,
  metrics TEXT,
  enqueued_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  error TEXT,
  UNIQUE(video_id, idx));`

// ---- handlers：每个写操作一个事务（契约 §3） ----

func handleSubmit(w http.ResponseWriter, r *http.Request) {
	var req struct {
		InputPath   string  `json:"input_path"`
		FinalOutput string  `json:"final_output"`
		ChunkInputs [][]any `json:"chunk_inputs"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	vid, now := newID(), nowF()
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	if _, err = tx.Exec(`INSERT INTO videos(id,input_path,output_path,
		merged_path,status,error,created_at,finished_at)
		VALUES(?,?,?,NULL,'processing',NULL,?,NULL)`,
		vid, req.InputPath, req.FinalOutput, now); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	for _, c := range req.ChunkInputs {
		if len(c) != 3 {
			writeErr(w, 400, "chunk_inputs 项必须是 [idx, input, output]")
			return
		}
		idx, _ := c[0].(float64)
		in, _ := c[1].(string)
		out, _ := c[2].(string)
		if _, err = tx.Exec(`INSERT INTO chunks(id,video_id,idx,input_path,
			output_path,status,attempt,worker_id,lease_until,metrics,
			enqueued_at,started_at,finished_at,error)
			VALUES(?,?,?,?,?,'pending',0,NULL,NULL,NULL,?,NULL,NULL,NULL)`,
			newID(), vid, int(idx), in, out, now); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]string{"video_id": vid})
}

func handleClaim(w http.ResponseWriter, r *http.Request) {
	var req struct {
		WorkerID string  `json:"worker_id"`
		TtlS     float64 `json:"ttl_s"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	row := tx.QueryRow(`SELECT c.* FROM chunks c
		JOIN videos v ON c.video_id = v.id
		WHERE c.status='pending' AND v.status='processing'
		ORDER BY v.created_at, c.idx LIMIT 1`)
	ch, err := scanChunk(row.Scan)
	if errors.Is(err, sql.ErrNoRows) {
		writeJSON(w, nil) // JSON null → Python None
		return
	}
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	now, lease := nowF(), nowF()+req.TtlS
	if _, err = tx.Exec(`UPDATE chunks SET status='running', worker_id=?,
		lease_until=?, started_at=? WHERE id=? AND status='pending'`,
		req.WorkerID, lease, now, ch["id"]); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	ch["status"], ch["worker_id"], ch["lease_until"] =
		"running", req.WorkerID, lease
	writeJSON(w, ch)
}

func handleComplete(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Metrics map[string]any `json:"metrics"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	metrics, _ := json.Marshal(req.Metrics)
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	var videoID string
	if err = tx.QueryRow("SELECT video_id FROM chunks WHERE id=?",
		r.PathValue("id")).Scan(&videoID); err != nil {
		writeErr(w, 404, "chunk 不存在")
		return
	}
	if _, err = tx.Exec(
		"UPDATE chunks SET status='done', metrics=?, finished_at=? "+
			"WHERE id=?", string(metrics), nowF(), r.PathValue("id")); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	var left int
	if err = tx.QueryRow(
		"SELECT COUNT(*) FROM chunks WHERE video_id=? AND status!='done'",
		videoID).Scan(&left); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	claimed := false
	if left == 0 { // 原子 Join：恰好一个调用者置 merging 成功
		res, err := tx.Exec("UPDATE videos SET status='merging' "+
			"WHERE id=? AND status='processing'", videoID)
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		if n, _ := res.RowsAffected(); n == 1 {
			claimed = true
		}
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"merge_claimed": claimed})
}

func handleFail(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Error       string `json:"error"`
		MaxAttempts int    `json:"max_attempts"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if req.MaxAttempts <= 0 {
		req.MaxAttempts = 3
	}
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	var videoID string
	var attempt int
	if err = tx.QueryRow("SELECT video_id, attempt FROM chunks WHERE id=?",
		r.PathValue("id")).Scan(&videoID, &attempt); err != nil {
		writeErr(w, 404, "chunk 不存在")
		return
	}
	if attempt+1 >= req.MaxAttempts { // 耗尽：chunk+video 双双 failed
		_, _ = tx.Exec("UPDATE chunks SET status='failed', error=?, "+
			"finished_at=? WHERE id=?", req.Error, nowF(), r.PathValue("id"))
		_, _ = tx.Exec("UPDATE videos SET status='failed', error=?, "+
			"finished_at=? WHERE id=?", req.Error, nowF(), videoID)
	} else { // 未耗尽：回 pending 重派，attempt+1
		_, _ = tx.Exec(`UPDATE chunks SET status='pending',
			attempt=attempt+1, worker_id=NULL, lease_until=NULL, error=?
			WHERE id=?`, req.Error, r.PathValue("id"))
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"ok": true})
}

func handleReclaim(w http.ResponseWriter, r *http.Request) {
	var req struct {
		MaxAttempts int `json:"max_attempts"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if req.MaxAttempts <= 0 {
		req.MaxAttempts = 3
	}
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	rows, err := tx.Query(
		"SELECT * FROM chunks WHERE status='running' AND lease_until < ?",
		nowF())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	type expired struct {
		id, videoID string
		attempt     int
	}
	var list []expired
	for rows.Next() { // 先取全再改：避免同事务内读写交错
		ch, err := scanChunk(rows.Scan)
		if err != nil {
			rows.Close()
			writeErr(w, 500, err.Error())
			return
		}
		list = append(list, expired{ch["id"].(string),
			ch["video_id"].(string), ch["attempt"].(int)})
	}
	rows.Close()
	for _, e := range list {
		if e.attempt+1 >= req.MaxAttempts {
			_, _ = tx.Exec("UPDATE chunks SET status='failed', "+
				"attempt=attempt+1 WHERE id=?", e.id)
			_, _ = tx.Exec("UPDATE videos SET status='failed', "+
				"error='lease 过期且重试耗尽', finished_at=? WHERE id=?",
				nowF(), e.videoID)
		} else {
			_, _ = tx.Exec(`UPDATE chunks SET status='pending',
				attempt=attempt+1, worker_id=NULL, lease_until=NULL
				WHERE id=?`, e.id)
		}
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]int{"reclaimed": len(list)})
}

func handleMergeComplete(w http.ResponseWriter, r *http.Request) {
	var req struct {
		MergedPath string `json:"merged_path"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if _, err := db.Exec(
		"UPDATE videos SET status='qc_wait', merged_path=? WHERE id=?",
		req.MergedPath, r.PathValue("id")); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"ok": true})
}

func handleFinish(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Status string `json:"status"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if _, err := db.Exec(
		"UPDATE videos SET status=?, finished_at=? WHERE id=?",
		req.Status, nowF(), r.PathValue("id")); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"ok": true})
}

func handleGetVideo(w http.ResponseWriter, r *http.Request) {
	v, err := getVideo(r.PathValue("id"))
	if err != nil {
		writeErr(w, 404, "video 不存在")
		return
	}
	writeJSON(w, v)
}

func handleChunkOutputs(w http.ResponseWriter, r *http.Request) {
	rows, err := db.Query(
		"SELECT output_path FROM chunks WHERE video_id=? ORDER BY idx",
		r.PathValue("id"))
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		out = append(out, p)
	}
	writeJSON(w, map[string]any{"outputs": out})
}

func handleOverview(w http.ResponseWriter, _ *http.Request) {
	rows, err := db.Query("SELECT id FROM videos ORDER BY created_at")
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer rows.Close()
	vids := []map[string]any{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		v, err := getVideo(id)
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		vids = append(vids, v)
	}
	writeJSON(w, vids)
}

func main() {
	dbPath, addr := "pipeline.db", "127.0.0.1:8080"
	if len(os.Args) > 1 {
		dbPath = os.Args[1]
	}
	if len(os.Args) > 2 {
		addr = os.Args[2]
	}
	// _txlock=immediate：写事务起手即 BEGIN IMMEDIATE（契约 §5，
	// 对应 Python 端语义）；WAL + busy_timeout 同 Python 端
	var err error
	db, err = sql.Open("sqlite",
		"file:"+dbPath+
			"?_txlock=immediate"+
			"&_pragma=journal_mode(WAL)"+
			"&_pragma=busy_timeout(10000)")
	if err != nil {
		log.Fatal(err)
	}
	if _, err = db.Exec(ddl); err != nil {
		log.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /init", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]bool{"ok": true}) // schema 启动时已建
	})
	mux.HandleFunc("POST /videos", handleSubmit)
	mux.HandleFunc("POST /chunks/claim", handleClaim)
	mux.HandleFunc("POST /chunks/{id}/complete", handleComplete)
	mux.HandleFunc("POST /chunks/{id}/fail", handleFail)
	mux.HandleFunc("POST /videos/{id}/merge_complete", handleMergeComplete)
	mux.HandleFunc("POST /videos/{id}/finish", handleFinish)
	mux.HandleFunc("POST /maintenance/reclaim", handleReclaim)
	mux.HandleFunc("GET /videos/{id}", handleGetVideo)
	mux.HandleFunc("GET /videos/{id}/chunk_outputs", handleChunkOutputs)
	mux.HandleFunc("GET /videos", handleOverview)
	log.Printf("gocp listening on %s (db=%s)", addr, dbPath)
	log.Fatal(http.ListenAndServe(addr, mux))
}
```

`docs/control-plane-contract.md` **追加**：

```markdown
## 6. HTTP API 映射（gocp 实现）

| 协议方法 | HTTP | 请求体 | 响应 |
|---|---|---|---|
| init_db | POST /init | — | {"ok": true}（schema 服务启动时已建） |
| submit_video | POST /videos | {input_path, final_output, chunk_inputs:[[idx,in,out],…]} | {video_id} |
| claim_chunk | POST /chunks/claim | {worker_id, ttl_s} | chunk 对象 或 null |
| complete_chunk | POST /chunks/{id}/complete | {metrics} | {merge_claimed: bool} |
| fail_chunk | POST /chunks/{id}/fail | {error, max_attempts} | {ok: true} |
| complete_merge | POST /videos/{id}/merge_complete | {merged_path} | {ok: true} |
| finish_video | POST /videos/{id}/finish | {status} | {ok: true} |
| reclaim_expired | POST /maintenance/reclaim | {max_attempts} | {reclaimed: int} |
| get_video | GET /videos/{id} | — | video 对象（404 + {"error"} 不存在时） |
| chunk_outputs | GET /videos/{id}/chunk_outputs | — | {outputs: [path,…]} |
| video_overview | GET /videos | — | [video 对象,…] |

JSON 语义：键 = snake_case 列名；可空列显式 null（对应 Python
dict[row] 的 None 语义，不缺键）；错误 = 非 2xx + {"error": msg}。
```

- [ ] **Step 5: 构建 + 契约双跑 + Go 模式 E2E**

```bash
(cd gocp && go build -o /tmp/gocp .)
./venv/bin/python -m pytest tests/test_contract.py -v
./venv/bin/python -m pytest tests/test_go_cp.py -v
```

Expected: contract `10 passed`（无 go 工具链则 `5 passed, 5 skipped`）；go_cp `2 passed`（无 go 则 skipped）。

- [ ] **Step 6: 全量回归（默认路径必须完全不受影响）**

```bash
./venv/bin/python -m pytest -v
```

Expected: 此前所有测试全绿 + 新增 go 项全过/跳过。SQLite 内嵌路径零变化——`cp_url=None` 分支不 import remote。

- [ ] **Step 7: Commit**

```bash
git add gocp/go.mod gocp/go.sum gocp/main.go src/faceblur_lite/remote.py \
  tests/test_go_cp.py tests/conftest.py tests/test_contract.py \
  docs/control-plane-contract.md
git commit -m "feat: Go 控制面服务 gocp + HttpControlPlane——契约双实现（业界形态）" \
  -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review（方案自检记录）

**1. 需求覆盖**：R1 内核=Task 5；R2 遮挡=Task 4 单测+Task 6 E2E；R3 长视频=Task 1 流式+Task 8 切片/合并+Task 10 并行；R4 输入格式=Task 1/3（pipe 链路+文档 Context）；R5 模型选型=Task 3 Detector 协议；R6 下一帧预测=Task 4 `predict()`（有专门单测）；R7 控制面/数据面+转 Go=Task 7（协议+契约文档+契约测试）/Task 10（依赖注入）/Task 12（Go 服务 gocp+HttpControlPlane+契约参数化双跑+Go 模式 E2E）；R8 平台映射=Task 11 文档（含通用引擎对照）；R9 质检=Task 9；R10 多视频批量=Task 10 `run_batch`（共享池+失败隔离，2 个专门测试）+Task 11 CLI `inputs nargs="+"`+批量冒烟；R11/R12/R13 演进就绪=Task 10 worker_loop 常驻模式 / Task 11 `worker`+`split`+`merge` 子命令（算子 CLI 化）/ Task 12 gocp 独立服务（详见 Context"演进就绪设计"节与文件树）；R14 观测出口=Task 11 `stats.py`+`stats` 子命令+冒烟数字回填 README（只读旁路，零 schema 变更）。无缺口。

**2. 占位符扫描**：所有代码步骤含完整代码与命令；"细化项"全部集中在下方开放清单，不在任务步骤里。

**3. 类型一致性**：`Det.xyxy()->np.ndarray`、`Tracker.predict/update->list[Track]`、`VideoMeta` 字段（含 `duration_s`）、`ControlPlane` 协议方法签名与 `SqliteControlPlane`/`HttpControlPlane` 两实现逐一对应（claim_chunk->dict|None、complete_chunk->bool 等）、`worker_loop(cp: ControlPlane, ...)` 依赖注入（两实现均可 pickle，spawn 传参安全）、`cp_url` 参数链 CLI→run_batch/run_pipeline→_orchestrate 贯通、`run_batch->list[PipelineResult]`、`PipelineResult` 含 `input_path/error` 字段；Go 响应键=snake_case 列名、可空列显式 null（`dict[row]` 的 None 语义，不缺键）；契约测试参数化双跑锁定两实现行为一致；orchestrator 依赖 qc（Task 9 先于 Task 10，顺序正确）。

## 开放细化项（持续迭代清单，按优先级）

1. **qc 包含率判定**：输出域检出 + 与 chunk 上报的模糊区域做包含率判定（区分"漏检"与"糊了但仍检出"），降低误报
2. **qc_failed 升级重处理闭环**：违规帧映射回 chunk，用更低 conf + det_every=1 重跑该 chunk 后复检（重试要么升级要么别试）；attempt 耗尽任务落死信表（DLQ）供人工处置
3. **merge 任务 lease 化**：Worker 死在合并中段目前无恢复（编排器只认 qc_wait）；把 merge 也做成租约任务
4. **边界闪烁**：分片重叠 1-2s + 合并去重（跟踪 ID 在分片边界重置，观感问题非安全问题）
5. **SCRFD 升级**：`Detector` 协议已就位，换 InsightFace SCRFD 提升遮挡/小脸 recall
6. **控制面演进：Redis 化 / 心跳 / 多机**：Go 服务已内置 MVP（Task 12：gocp + HttpControlPlane 双形态，契约测试双跑闸门）。剩余：SQLite→Redis（原子 Join 用 Lua）、lease 心跳续约（长任务不被误回收）、多机部署（HTTP API 已就位，gocp 独立进程可跨机）、Worker 容器化（Compose `--scale`）
7. **上云阶段二**：Worker 打镜像，百舸任务式提交跑分片作业；本地 vs 云单卡 vs 云多卡三档对比
8. **真实数据 golden set**：标注 20-50 段真实视频算 recall/误报率，替代合成视频的定性验证
9. **血缘 + 观测时序化**：stats 最小出口已进 MVP（Task 11：排队/处理 P50/P95、吞吐、失败率、重试计数，只读旁路）；剩余：血缘审计（chunks 表补算子名+版本+参数）、时序化（Prometheus exporter/大盘/告警，平台期）——业界平台标配，老项目已验证最小方案
10. **qc 分层抽样**：均匀 1fps 之外，分片边界帧必查（跟踪重置点=漏检风险最高）；违规热点区间加密度抽样
11. **算子通用化**：Worker 目前直接绑定 face-blur 内核（轻量取舍）；演进=任务声明算子名+参数、Worker 按声明从注册表加载（通用执行器）——老项目 v3 已验证该形态，加新算子（车牌/人体脱敏）控制面零改动
12. **批量断点续跑/幂等提交**：submit 以 `input_path + params hash` 为幂等键——同键已 done 跳过、未完成续跑（当前重跑会新建 video 行重复处理）；大量视频长批次崩溃恢复的关键（R11）





