# face-blur-lite

轻量视频人脸脱敏管线——控制面/数据面分离的最小实现。

输入一段视频（demo 5 分钟、架构支持更长），输出人脸全部模糊的**新视频**（音轨保留）。以业界数据处理工作流的形态落地：控制面（SQLite 内嵌 / Go 服务双形态）+ 无状态数据面 Worker（scatter-gather 分片并行），本地单机闭环，零云成本。

核心设计一句话：**迁移时换的是调度层，保留的是算子层**——控制面/数据面分离 + 契约先行（`ControlPlane` 协议，SQLite/Go 双实现过同一份契约测试）的回报。

## 快速开始

```bash
# 1) 资产：模型（YuNet ~230KB）+ 测试人脸图（产物已 gitignore）
bash scripts/download_assets.sh

# 2) 安装（Python ≥3.10，系统依赖 ffmpeg）
python -m venv venv && ./venv/bin/pip install -e ".[dev]"

# 3) 合成测试视频（move=匀速移动 / occlude=中段遮挡）
./venv/bin/faceblur-lite demo-video demo.mp4 --duration 30 --scenario occlude

# 4) 单进程内核：一个视频 → 一个脱敏视频
./venv/bin/faceblur-lite single demo.mp4 demo_blurred.mp4

# 5) 批量分片并行：控制面 + N 个 Worker（长短混合，一次提交）
./venv/bin/faceblur-lite pipeline a.mp4 b.mp4 c.mp4 \
  --outdir outputs --workdir work-pipeline --workers 3 --segment 30

# 6) 输出域复检（pipeline 已内置逐视频 qc，也可独立调用）
./venv/bin/faceblur-lite qc outputs/a_blurred.mp4

# 观测出口 / 状态
./venv/bin/faceblur-lite stats work-pipeline/pipeline.db
./venv/bin/faceblur-lite status work-pipeline/pipeline.db
```

子命令速查：`single`（单进程内核）｜`pipeline`（批量并行）｜`worker`（独立常驻 Worker：`--cp-url` 连 Go 控制面或 `--db` 连本地库，容器/跨机扩容单元）｜`split` / `merge`（算子独立调用，DAG 引擎节点模板可直接引用）｜`qc`｜`demo-video`｜`stats`｜`status`。

## 架构

```
             ┌───────────────── 控制面（状态的唯一拥有者）─────────────────┐
             │  orchestrator 编排器                                        │
             │    逐视频 split+submit（失败隔离）→ 拉起共享 Worker 池       │
             │    → 巡检 lease 回收 → drain 后逐视频 qc 收尾               │
             │  ControlPlane 契约（协议）                                  │
             │    ├ SqliteControlPlane（默认：SQLite WAL 内嵌，零进程）     │
             │    └ HttpControlPlane（--cp-url：gocp Go 服务，业界形态）    │
             │  存储：videos / chunks 两级状态机                            │
             └───────▲─────────────────────────────┬──────────────────────┘
        claim（互斥）│  complete / fail / reclaim    │ submit / merge / finish
             ┌───────┴─────────────────────────────▼──────────────────────┐
             │  数据面（零状态，可任意 kill / 扩容）                        │
             │  无状态 Worker × N：认领 → 执行算子 → 事务回写              │
             │  算子（纯函数，文件→文件）：kernel（检测/跟踪/模糊）         │
             │                            splitter / merger / qc           │
             └────────────────────────────────────────────────────────────┘

scatter-gather：  视频 ─split→ [chunk_0 … chunk_N] ─blur×N（并行）→ merge → qc → done
                                    └────────── 原子 Join：最后完成者认领合并 ──┘
```

## 设计决策

| # | 需求 | 落点 |
|---|---|---|
| R1 | 视频进 → 人脸全模糊视频出 | 流式内核：pipe 解码→隔帧检测→跟踪补帧→外扩模糊→pipe 编码 |
| R2 | 手臂遮挡不漏糊 | 跟踪器遮挡外推 + 低分框二轮回收（ByteTrack 思路）+ max_age 容忍 |
| R3 | 长视频 | 流式 IO 内存 O(1) + scatter-gather 分片并行 |
| R4 | 模型输入格式 | ffmpeg rawvideo pipe → `(H,W,3)` BGR uint8 帧；音轨 `-c:a copy` 不重编 |
| R5 | 主流模型选型 | YuNet（~230KB，CPU 快）起步，藏在 `Detector` 协议后，SCRFD 为升级位 |
| R6 | 预测下一帧人脸坐标 | `Tracker.predict()` 恒速运动模型外推——显式、可单测 |
| R7 | 控制面/数据面 + 控制面 Go | 契约先行：`ControlPlane` 协议，SQLite 内嵌 / gocp Go 服务双形态，同一份契约测试 |
| R8 | 业界工作流平台 | 三平台映射见 [docs/platform-mapping.md](docs/platform-mapping.md) |
| R9 | 怎么证明没漏 | 输出域抽帧复检闸门（qc），违规即 qc_failed + 违规帧清单 |

（R10-R14：批量共享池 / 大量视频吞吐 / 容器迁移 / DAG 引擎切换 / 观测出口——见 [docs/design.md](docs/design.md) §1.2。）

- **阈值分域（硬约束）**：内核检测阈值 `det_conf=0.3` 作用于**源视频信号域**；质检阈值 `pass_conf=0.5` 作用于**重编码后的输出域**。两者不可比较不可复用——置信度不跨有损编码域守恒（实测：源域 0.10 的框重编码后可涨到 0.75）。
- **宁可多糊**：脱敏代价不对称——漏一个脸是隐私事故，多糊一片只是观感损失。模糊核比例 0.75×短边（大核摧毁低频结构，σ 须与人脸尺度相当），框外扩 margin 0.15。
- **跟踪器** = 下一帧预测 + 遮挡外推：恒速模型 `predict()`，匈牙利匹配（IoU），遮挡期低分框救活不断链，max_age=3 剪 ghost。
- **音轨 copy 不重编**：编码参数全局钉死（libx264/veryfast/crf20/yuv420p/+faststart）是 concat 流复制秒级合并的前提。

## 指标输出

`faceblur-lite stats work-pipeline/pipeline.db`（只读旁路直读控制面库，观测读不进任务状态契约；SQLite WAL 允许并发只读）：

```
videos: done=3
chunks: 14 done=14 retried=0
queue_s: p50=19.282 p95=38.165
proc_s:  p50=10.721 p95=12.812
throughput: 17.35 chunks/min
fail_rate: 0.0%
```

字段：`queue_s` 排队时延＝started−enqueued（P50/P95，秒）；`proc_s` 处理时延＝finished−started；`throughput` 吞吐（chunks/min，按首入队至末完成的墙钟）；`fail_rate` 失败 chunk 占比；`retried` 重试过的 chunk 数（attempt>0）。`--json` 出机器可读格式。

**冒烟实测**（2026-09-24，Intel i9-9880H 8C16T / ffmpeg 8.1.2；批次 = 5min 遮挡 + 30s 移动 + 90s 遮挡，`--workers 3 --segment 30`，即上面 stats 输出）：

| 指标 | 实测 |
|---|---|
| 5min 视频单进程串行 | 37.9s |
| 三视频串行合计 | 54.9s（37.9 + 5.7 + 11.4） |
| 三视频一次提交批量 | **57.6s**，14 chunks 全 done，qc 3/3 通过（抽帧 300/30/90，0 违规），输出时长与输入逐帧一致 |
| 单 chunk 处理 | p50 10.7s / p95 12.8s（30s 切片） |

解读（诚实版）：本机单进程内核已吃满 CPU（user/real≈7.7 核，OpenCV/ffmpeg 内部多线程），进程级分片在**本机**无净加速（57.6s vs 54.9s，≈5% 编排开销）；scatter-gather 的加速位在单进程线程受限的场景——容器 CPU 配额、跨机 Worker 池。批量的确定收益是**摊销**：一次提交一个共享池，三视频在同一窗口完成（51.4/55.1/55.7s），小视频以队列交错搭车、无需逐个跑。另可观察 FIFO 公平性缺口：小视频 chunk 排在长视频 10 个 chunk 之后（queue p95 38.2s 即其等待），公平调度记为演进项。

## 已知限制与演进

- **lease 无心跳续约**（SQS 同款超时重派；长任务需调大 TTL）→ 心跳续约
- **qc_failed 不自动重跑**（确定性系统原参数重试无效）→ 升级重跑闭环（更低 conf + det_every=1 重跑违规 chunk）+ DLQ
- **均匀 1fps 抽检** → 分层抽样：分片边界帧必查（跟踪重置点=漏检风险最高）
- **无血缘记录** → chunks 表补算子名+版本+参数；stats 时序化（exporter/大盘/告警）
- **merge 无 lease**（Worker 死在合并中段无恢复）→ merge 任务 lease 化
- **重复提交不去重** → 幂等提交键（input+params hash），断点续跑
- **FIFO 无优先级/公平调度**（长视频头部队头阻塞后提交的小视频）
- **检测绑定 face-blur 内核** → 任务声明算子+注册表，Worker 变通用执行器（加车牌/人体脱敏算子控制面零改动）
- **YuNet 遮挡 recall 一般** → SCRFD 升级（协议已留，换实现类管线零改动）
- 上云三步：gocp 独立部署 → Worker 打镜像常驻（`worker` 子命令已就位）→ 百舸任务式；完整迁移清单见 design.md §7.2

完整清单见实施计划"开放细化项"（12 条，按优先级）：`docs/superpowers/plans/2026-09-23-face-blur-lite-mvp.md`。

## 文档

- [docs/design.md](docs/design.md) — 详细设计（四大视图 + ADR + 容错 + 业界平台对照 + 演进路线，Mermaid 图示）
- [docs/pipeline-guide.md](docs/pipeline-guide.md) — 全链路管线指南（一帧的旅程：逐层格式变换 + 检测/跟踪/糊机理 + 异常五层处置矩阵）
- [docs/platform-mapping.md](docs/platform-mapping.md) — 百舸/火山 LAS/胜算平台映射 + 通用引擎对照
- [docs/superpowers/plans/2026-09-23-face-blur-lite-mvp.md](docs/superpowers/plans/2026-09-23-face-blur-lite-mvp.md) — 实施计划（TDD 任务分解）
- [docs/bugs/](docs/bugs/) — 执行期 bug 记录（根因/修复/教训）

## 状态

MVP 完成（Task 0-12）：55 测试绿，控制面双形态（SQLite 内嵌 / gocp Go 服务）契约测试参数化双跑（11 用例 ×2=22）+ 终审修复波回归（切片确定性/撇号路径/输出碰撞/缺 id 语义）。
