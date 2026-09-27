# face-blur-lite 总览：项目与业界坐标

> **定位**：项目门面文档——「这个项目是什么 + 在业界版图的哪个位置」。[README](../README.md) 管快速上手，[design.md](design.md) / [pipeline-guide.md](pipeline-guide.md) 管单项深挖，[industry-benchmark.md](industry-benchmark.md) / [platform-mapping.md](platform-mapping.md) 管对标锐评与平台映射，本文做总装与业界坐标。
> **数字纪律**：项目数字全部溯源到 file:line（口径基线 fa65f90，139 测试绿）；业界引擎语义为通识口径；具身/智驾行业数字为公开分享口径（技术报告/厂商分享，按名称可检索），量级供定位不供考核。
> **章节地图**：§1 项目本身｜§2 管线要素与业界工作流总览｜§3 编排引擎语义对照｜§4 数据引擎·算子·模型｜§5 AI 数据平台能力层｜§6 具身智能数据全景（落地方向）｜§7 行业数据闭环（具身/智驾双泳道）｜§8 边界与路线图。

## 1. 项目是什么

一段视频进、人脸全部模糊的**新视频**出（音轨保留）——以业界数据处理工作流的形态落地的**轻量 AI 数据平台原型**：控制面/数据面分离的两层架构，scatter-gather 切片分发、共享 Worker 池调度、lease 租约容错、原子 Join 合并、独立质检闸门。

算子是人脸脱敏，但平台的**错误模型**（互斥/租约/终态/围栏/幂等/相位/取证）与具身智能、智驾场景的数据处理是同一套——**算子换了，错误模型不变**。把脱敏算子换成多模态清洗、格式转换、动作重定向，调度语义、容错语义、质检语义原样成立。这是「平台」与「应用」的区别：交付的不是脱敏效果，是那套错误模型。

### 1.1 两层架构

```
    ┌──────────────────────────────────────────────────────────────┐
    │                  控制面 —— 状态的唯一拥有者                  │
    │                                                              │
    │  orchestrator 编排器                                         │
    │  · 逐视频 split + submit（失败隔离）                         │
    │  · 拉起共享 Worker 池                                        │
    │  · 巡检 lease 回收，drain 后逐视频 qc 收尾                   │
    │                                                              │
    │  ControlPlane 契约（协议，三实现）                           │
    │  ├─ SqliteControlPlane（默认：SQLite WAL 内嵌，零进程）      │
    │  └─ HttpControlPlane（--cp-url：gocp Go 服务，业界形态）     │
    │                                                              │
    │  存储：videos / chunks 两级状态机                            │
    └───────────────────────▲─────────────────────────┬────────────┘
                            │                         │
 Worker 接口（上行）        │                         │  编排器接口（下行）
 claim（互斥认领）          │                         │  submit / merge / finish
 complete / fail / reclaim  │                         │
 （lease 租约回收）         │                         │
                            │                         ▼
    ┌───────────────────────┴─────────────────────────▼────────────┐
    │             数据面 —— 零状态，可任意 kill / 扩容             │
    │                                                              │
    │  无状态 Worker × N                                           │
    │    认领 → 执行算子 → 事务回写                                │
    │                                                              │
    │  算子（纯函数，文件 → 文件）                                 │
    │    kernel（检测 / 跟踪 / 模糊）                              │
    │    splitter / merger / qc                                    │
    └──────────────────────────────────────────────────────────────┘

scatter-gather 数据流：

  视频 ─split→ [ chunk_0 … chunk_N ] ─blur×N（并行）→ merge → qc → done
                                     │                ▲
                                     └── 原子 Join ──┘
                                       最后完成者认领合并（rowcount==1，恰好一次）
```

（图与 [README 架构节](../README.md) 同源；容错时序与 ADR 见 [design.md](design.md)。）

### 1.2 三份契约（架构纵切）

架构图是横向分层；纵向看，跨层流转由三份契约承担——**状态、配置流入、证据流出**各一份：

| 契约 | 定义处 | 承担 | 消费路径 |
|---|---|---|---|
| **ControlPlane**（状态） | [control.py](../src/faceblur_lite/control.py) 协议；[state.py](../src/faceblur_lite/state.py) SQLite / [remote.py](../src/faceblur_lite/remote.py) HTTP / [gocp/main.go](../gocp/main.go) 三实现 | 状态唯一拥有者：videos/chunks 两级状态机的全部迁移操作 | 编排器提交、Worker claim/complete/fail、契约测试参数化双跑 |
| **KernelParams**（配置流入） | [kernel.py](../src/faceblur_lite/kernel.py) | 单一配置对象：检测器选择、阈值、IO/跟踪参数 | CLI 构造 → run_batch → worker → kernel；qc 复用其检测器注入点（换 pass_conf 不换检测器族） |
| **QCReport**（证据流出） | [qc.py](../src/faceblur_lite/qc.py) | 质检证据：passed / fatal_count / marginal_count + 违规帧清单 | qc_video → 编排器 verdict（done / qc_failed）→ CLI 退出码梯 0/1/3 |

这框架由本地知识图谱（`.graphify/`，524 节点 975 边，未入库）的介数分析独立导出：KernelParams 是全图第一桥（介数 0.180，2 跳邻域触及 8 个社群）——配置总线跨切全部数据面；SqliteControlPlane 度最高（43）但边不出簇（0.088）——状态核心稳态；QCReport 的方法边全部指向编排器消费侧——**API 面为裁决而生，membership follows the heaviest consumer**。

### 1.3 权威数字表

| 维度 | 数字 | 出处 |
| --- | --- | --- |
| 测试 | 139 绿（含契约 14 场景×2=28 双跑） | pytest 实测；[tests/test_contract.py](../tests/test_contract.py) |
| 格式链 | 7 层变换 / 8 种形态；2.90MB/帧；2121 帧 ≈6.4GB 帧流全程 pipe 内存 O(1) | [pipeline-guide.md](pipeline-guide.md) §0.2 |
| 调度语义 | lease 900s TTL 回收；attempt≤3；claim 互斥 / 原子 Join / 终态不可逆 / fencing 复合围栏 | [worker.py:18](../src/faceblur_lite/worker.py) / [control.py:22](../src/faceblur_lite/control.py) |
| 实测吞吐 | 17.35 chunks/min；queue p50 19.3s / p95 38.2s；proc p50 10.7s / p95 12.8s | [README](../README.md) 指标节 |
| 诚实负结果 | 三视频批量 57.6s vs 串行 54.9s（≈5% 编排开销；单进程内核吃满 CPU，加速位在容器配额/跨机池） | [README](../README.md) 冒烟实测节 |
| 质量 | 违规取证链 621→584→566→487→308（最优 29%→14.5%；默认口径 23.2%）；T_FATAL=0.7 分级，重采三组 fatal 恒 4 | [pipeline-guide.md](pipeline-guide.md) / [qc-severity-calibration.md](qc-severity-calibration.md) |
| 模型 | YuNet 232KB/12.73ms（默认）；SCRFD det_500m 2.5MB/15.52ms（1.22×，opt-in） | [bugs/014](bugs/014-tiny-face-capability-envelope.md) |

### 1.4 代码地图

| 分组 | 模块 | 职责 |
|---|---|---|
| 控制面（Py） | control / state / remote | 协议；SQLite 实现（WAL + BEGIN IMMEDIATE）；HTTP 客户端（`--cp-url` 切 gocp） |
| 控制面（Go） | [gocp/main.go](../gocp/main.go) | stdlib net/http + modernc.org/sqlite（纯 Go 无 CGO 静态单二进制），12 端点，DDL/事务语义与 Python 端逐字一致 |
| 编排 | orchestrator / worker / cli | run_batch 批量编排；常驻 Worker（lease/attempt）；CLI 入口（9 个子命令） |
| 算子 | splitter / merger / kernel / detector / tracker / blur / qc | 切片；合并；内核（检测+跟踪+模糊）；检测器协议+工厂；恒速外推跟踪；高斯模糊；质检闸门 |
| IO | ffmpeg_io | rawvideo pipe 流式读写（内存 O(1)）、帧号抽样 |
| 支撑 | stats / progress / testmedia | 只读旁路观测；进度上报；合成测试视频 |
| 测试 | tests/（18 文件 + conftest） | 139 用例；契约测试参数化 `["sqlite","go"]` 双跑 |

## 2. 数据处理管线与业界工作流总览

### 2.1 管线要素分解

一条数据处理管线的五个要素，本项目全部有对应实现——这是「以业界工作流形态落地」的可核对版本：

| 要素 | 业界通用形态 | 本项目实现 |
|---|---|---|
| 输入抽象 | 数据集 / 表 / 对象存储路径 | POSIX 路径字符串全链路传递（迁 PFS/BOS 算子零改动，design §7.2 步 3） |
| 算子链 | DAG 节点（容器 / 函数） | split→blur×N→merge→qc，全部 CLI 子命令化、纯函数幂等覆盖写 |
| 调度 | 中心调度器+依赖边 / 队列领取 | SQLite 事务 claim 互斥 + lease TTL + attempt 预算（§3 逐语义对照） |
| 质检 | 质量断言 / 规则引擎 | 输出域抽帧复检闸门：阈值分域 + T_FATAL 分级 + qc_failed 隔离区 |
| 证据 | 指标 / 血缘 / 报告 | QCReport + 违规帧清单 + stats 只读旁路（血缘=已知缺口，§5） |

具身侧对位：输入=episode 级多模态时序文件（对象存储路径，仍是同一 POSIX 路径契约）；算子链=对齐/重定向/清洗/格式转换/质检五类（§6.3）；调度、质检、证据三要素语义原样成立——**换算子，不换错误模型**。

### 2.2 业界版图五层

| 层 | 业界代表 | 本项目位置 |
|---|---|---|
| 1 工作流编排引擎 | Airflow（批处理事实标准）/ Argo（K8s 原生 DAG）/ Kubeflow Pipelines（ML）/ Temporal（持久化执行）/ Dagster·Prefect·Flyte·Metaflow / Ray | **微缩控制面**：lease/attempt/终态/Join ≈ Temporal、Argo 的核心语义（§3 矩阵逐项对照） |
| 2 数据引擎 | Spark（批）/ Flink（流）/ Beam（统一模型）；湖仓 Delta/Iceberg/Hudi | **单机形态内嵌**：ffmpeg pipe 流式管道（§4.1）；分布式形态缺——单机不需要，上云由存储层承接 |
| 3 AI 数据平台能力层 | DVC/LakeFS（版本）/ OpenLineage（血缘）/ CVAT·Label Studio（标注）/ Great Expectations（质量断言）/ MLflow（闭环）；国内=百舸/胜算/火山 LAS | **质量断言有**（分级闸门+隔离区+校准）；版本/血缘=已知缺口（§5） |
| 4 行业数据闭环 | 具身：OXE/RT-X/LeRobot/Cosmos/Isaac 工具链；智驾：影子模式→长尾挖掘→自动标注→OTA | 「清洗/脱敏」段的微缩完整实现；qc_failed 隔离区=badcase 隔离回流语义的微缩（§7） |
| 5 模型算子层 | YOLO 系 / SAM（预标注革命）/ SCRFD / DeepPrivacy2（生成式替换）/ CLIP 嵌入去重 / 小模型当过滤器 | **主力位置**：双检测器+能力包络探针+A/B 方法论（§4.3） |

一句话对位：**第 5 层的算子 + 第 1 层的微缩控制面 + 第 3 层的质检雏形；缺 2、4 的规模化与真正的数据集版本管理**——边界即路线图（§8）。

**业界代表速写**（一句话版，通识口径；第 4/5 层的代表已在 §6-§7 与 §4.3 展开）：

- **L1 编排引擎**——**Airflow**：Python DAG+定时/依赖调度，批处理事实标准（Apache，Airbnb 出身）。**Argo Workflows**：K8s 原生，每步一容器，CNCF 毕业项目。**Kubeflow Pipelines**：K8s 上 ML 管线的组件化拼装。**Temporal**：持久化执行——代码即工作流，崩溃后从事件历史续跑，lease/attempt 语义的完全体（Uber Cadence 系）。第二梯队分流：**Dagster** 资产导向（管线=数据资产的产出链）、**Prefect** 轻量 Pythonic、**Flyte** 强类型 ML（Lyft）、**Metaflow** 脚本即作业（Netflix）。**Ray**：通用分布式计算框架，常当训练/数据处理后端。
- **L2 数据引擎**——**Spark**：批 ETL 代名词，内存中间态+shuffle。**Flink**：流优先，事件时间/状态计算，流批一体。**Beam**：统一编程模型，一套代码跑批与流（runner 挂 Flink/Spark 等）。湖仓三件套 **Delta Lake**（Databricks）/ **Iceberg**（Netflix，表格式事实标准）/ **Hudi**（Uber）：给对象存储数据湖加 ACID 事务、time travel、schema 演进。
- **L3 能力层**——**DVC**：数据/模型的 git 式版本控制（大文件指针+远端存储）。**LakeFS**：对象存储上的 git 分支语义。**OpenLineage**：血缘采集开放标准（配 Marquez 元数据库）。**CVAT**（Intel 系）/ **Label Studio**：主流开源标注平台，支持模型预标注。**Great Expectations**：「期望即测试」的数据质量断言。**MLflow** / **W&B**：实验跟踪+模型注册，闭环观测事实标准。国内：百舸=算力层工作流、火山 LAS/胜算=数据层（§2.3）。

**五层版图一图流**（● 实做 ◐ 雏形 ○ 微缩/部分形态；与上表、一句话对位同口径）：

```
┌─────────────────────────────────────────┬────────────────────────┐
│ 业界版图五层                            │ 本项目足迹             │
├─────────────────────────────────────────┼────────────────────────┤
│ L1 工作流编排引擎                       │ ● 实做：微缩控制面     │
│ Airflow·Argo·Kubeflow·Temporal·Ray 等   │   claim/lease/Join     │
├─────────────────────────────────────────┼────────────────────────┤
│ L2 数据引擎                             │ ○ 微缩：单机内嵌       │
│ Spark·Flink·Beam；Delta/Iceberg/Hudi    │   ffmpeg pipe O(1)     │
├─────────────────────────────────────────┼────────────────────────┤
│ L3 AI 数据平台能力层                    │ ◐ 雏形：质检断言       │
│ DVC·OpenLineage·CVAT·GE·MLflow；百舸等  │   版本/血缘=缺口       │
├─────────────────────────────────────────┼────────────────────────┤
│ L4 行业数据闭环                         │ ○ 微缩：脱敏一段       │
│ 具身 Ego·LeRobot·Cosmos；智驾 影子→OTA  │   qc_failed 隔离区     │
├─────────────────────────────────────────┼────────────────────────┤
│ L5 模型算子层                           │ ● 实做：主力位置       │
│ YOLO·SAM·SCRFD·DeepPrivacy2·CLIP        │   双检测器+A/B         │
└─────────────────────────────────────────┴────────────────────────┘
```

读法：右栏三个 ●/◐ 是「已站住的位置」，两个 ○ 是「有对应物但非该层形态」——L2 单机不需要分布式（§4.1），L4 只有脱敏一段（§7）；边界即路线图（§8）。

### 2.3 平台两层口径

[platform-mapping.md](platform-mapping.md) 的分层口径：**胜算/火山 LAS 是数据层工作流**（数据怎么被加工），**百舸工作流是算力层工作流**（加工任务怎么被调度执行）。本项目在单机上同时复刻两层骨架——数据面算子对应数据层，控制面调度对应算力层——映射到任一平台都是组件对号入座，不是重新设计。

## 3. 工作流编排引擎语义对照

对照方法：不罗列引擎功能，拿本项目的**六个调度语义**当行，去每个引擎找对应物——每行都是「我做过、我知道业界怎么叫」。

| 语义 | 本项目 | Airflow | Argo | Temporal | K8s Job | Ray |
|---|---|---|---|---|---|---|
| 互斥认领 | `BEGIN IMMEDIATE` 事务 claim（单写者串行化） | scheduler 单点分派 task instance | controller reconcile，节点单实例 | task queue 长轮询，server 保证单投 | 调度器绑定节点，pod 单实例 | raylet 中央调度 |
| 租约回收 | lease 900s TTL 过期→pending 重派 | 僵尸 task 心跳超时重排 | pod 失败 controller 重建 | heartbeat / ScheduleToClose 超时 | node 心跳丢失→驱逐重建 | 对象丢失 lineage 重算 |
| 重试预算 | attempt≤3 | retries + retry_delay | retryStrategy.limit | RetryPolicy.maximumAttempts | backoffLimit（默认 6） | max_retries |
| 终态不可逆 | 终态守卫（契约测试锁定） | terminal state 不回退 | Succeeded/Failed 终态 | 事件历史不可变（durable execution） | completed pod 不重启 | —（无持久状态） |
| 僵尸围栏 | `(worker_id, attempt)` 复合谓词 | 引擎单写者，应用层不设防 | 同左 | run ID 换代，旧执行迟到结果被拒 | UID / resourceVersion 乐观并发 | — |
| fan-in 合并 | 原子 Join rowcount==1（恰好一次） | trigger rule all_success join 节点 | 依赖边 + `withParam` 动态展开 | Activity/child workflow futures 聚合 | 无原生（需自建） | futures gather |

口径说明：本项目列的锚点在 [platform-mapping.md](platform-mapping.md) 引擎对照节与 [control-plane-contract.md](control-plane-contract.md)；僵尸围栏行业界侧的文档锚点是 **SQS receipt handle / K8s resourceVersion**（[industry-benchmark.md](industry-benchmark.md) 任务围栏行）；其余引擎格为通识口径。Kubeflow Pipelines 的编排语义随其底层（Argo 系/自有 backend），不单列。

**迁移路径**（证明「搬上引擎每一步改什么」）：

- design §7.2 五步清单全部「MVP 已预留」：gocp 静态单二进制即 Deployment；Worker 常驻模式（`idle_exit_s=0`）即 Deployment/百舸任务式；路径契约全链 POSIX 可接 PFS/BOS；模型 env 注入已预留；编排器退化为提交客户端。
- design §7.3 收编表：原子 Join→fan-in 依赖边（引擎原生）、claim 队列→引擎调度器（被收编）、lease+attempt→引擎重试策略、split 输出 chunk 清单→Argo `withParam` 动态展开 blur×N。
- 结论：**切换=换调度层，算子层与质检/阈值策略 100% 保留**（design.md §7.3）。手写窄版不是浪费——它是「理解工作流引擎在解决什么问题」的证据。

## 4. 数据引擎·算子·模型

### 4.1 ffmpeg pipe = 单机数据引擎

数据引擎层不是「缺」，而是以单机形态内嵌——流式管道的三个核心语义都在：

| 维度 | Spark / Flink | 本项目（ffmpeg pipe） |
|---|---|---|
| 数据流模型 | 流式管道、算子组合 | 同：解码→检测→跟踪→糊→编码全 pipe，帧不落盘 |
| 中间态物化 | shuffle/物化由引擎管理 | **不物化**：7 层格式变换 8 种形态全程 pipe（[pipeline-guide.md](pipeline-guide.md) §0.2） |
| 内存 | 按窗口/分区 | O(1)（2121 帧 ≈6.4GB 帧流不过内存） |
| 分区并行 | 分布式分区 | split 切片 = 手工 fan-out（scatter-gather） |
| 容错 | checkpoint / lineage | at-least-once + 幂等覆盖写（chunk 级） |
| 缺位 | — | 分布式 shuffle / 列存 / 批优化器——单机不需要；上云时存储位由 BOS/PFS 承接 |

### 4.2 算子抽象

- **CLI 子命令化**：9 个子命令（single/pipeline/worker/split/merge/qc/demo-video/stats/status），算子可独立调用——DAG 引擎节点模板直接引用（design §7.3 收编表第一行）。
- **纯函数幂等覆盖写**：失败留半成品无害，重跑覆盖——at-least-once 语义的数据面答法。
- **检测器藏在协议后**：`Detector` 协议 + `make_detector` 工厂注入（[detector.py](../src/faceblur_lite/detector.py)）——SCRFD 升级零控制面改动；qc 交叉验收位 `--qc-detector`（换闸门检测器族）。
- 业界同型：算子=K8s Job 封装（百舸口径）；Beam ParDo / Argo 模板。
- 一句话：**「迁移换调度层、保留算子层」**——算子层投资全部延续（design.md §7.3）。

### 4.3 算子层模型

手里的模型与实测：

| 检测器 | 模型 | 体积/单帧耗时 | 状态 |
|---|---|---|---|
| YuNet | face_detection_yunet_2023mar.onnx | 232KB / 12.73ms | 默认 |
| SCRFD | det_500m.onnx（buffalo_s） | 2.5MB / 15.52ms（1.22×） | opt-in（`--detector scrfd`；A/B 未胜出不翻默认） |

围绕模型的三件方法论（比模型本身更可迁移）：

1. **能力包络探针**：升级前先画包络——YuNet 对 16-32px 小脸全盲、48px 需 2× 放大 conf 0.54 才检出；SCRFD 32px conf 0.374 可检出（包络部分外扩）。选型决策由探针数据驱动，不是拍脑袋（[bugs/014](bugs/014-tiny-face-capability-envelope.md)）。
2. **A/B 预注册判据**：胜出线预写死（违规降 ≥30% 且 wall 劣化 ≤10%）+ 同闸门原则（两臂 qc 检测器必须一致——两检测器 conf 标定不同，同数值 conf 下 SCRFD 出框仅 YuNet 约 1/3（344 vs 1049），闸门看更少框=「闸门变瞎」不是「质量变好」）。结果未胜出→不翻默认，翻牌前置条件=先重校 conf 工作点。
3. **阈值分域**：内核检测 `det_conf=0.3` 作用于源视频信号域，质检 `pass_conf=0.5` 作用于重编码后的输出域——**置信度不跨有损编码域守恒**（实测源域 0.10 的框重编码后可涨到 0.75）。这是「便宜模型先筛、贵模型复核」模式的微缩版：分域 = 分模型/分域各用各的工作点。

业界算子层模型版图与对照：

| 模型 | 在管线里的角色 | 本项目对照 |
|---|---|---|
| YOLO 系 | 通用检测算子 | YuNet/SCRFD 同位（人脸专用小模型） |
| SAM | 预标注革命：分割掩码供标注与质检 | 矩形糊 vs 分割+生成替换——DeepPrivacy2 路线，[industry-benchmark.md](industry-benchmark.md) 自评落后代差 |
| DeepPrivacy2 | 生成式替换（不可逆性最强） | 同上；不可逆性验收（FR 相似度闭环）在 backlog |
| CLIP 嵌入 | 数据集去重/检索 | 无（无数据集层，§5） |
| SLAM/手部姿态/IK | 具身 Ego 采集算子链 | 同型：模型藏在算子协议后注入，随算子版本走 |
| VLM 打分 | 语义层质检（任务成败/因果链） | QC 检测器复检=同型更弱位（「规则+模型」双层中的模型层） |

注：业界把「模型即算子」（检测/分割/SLAM/VLM 内嵌于管线算子）当默认形态，但公开材料里少有带型号带实测的实例——本仓库的双检测器+包络探针+A/B 存档是这类对照里少见的可核对样本。

## 5. AI 数据平台能力层

| 能力 | 业界代表 | 本项目 | 状态 |
|---|---|---|---|
| 数据集版本管理 | DVC / LakeFS；湖仓 time travel（Delta/Iceberg） | 幂等提交键 + 逐输出 manifest | ✗ 已知缺口（backlog） |
| 血缘 | OpenLineage / Marquez | 无 manifest、无逐输出溯源 | ✗（[industry-benchmark.md](industry-benchmark.md) 自评落后一档） |
| 标注 | CVAT / Label Studio / SAM 预标注 | N/A（本管线无标注环节；qc 抽样复检属质量侧） | — |
| 质量断言 | Great Expectations / dbt test | 阈值分域 + T_FATAL=0.7 分级（fatal/marginal）+ qc_failed 隔离区 + 分布校准（重采三组 fatal 恒 4，判别力稳定） | ✓ 雏形完整 |
| 闭环追踪 | MLflow / W&B | stats 只读旁路（queue/proc P50·P95、吞吐、失败率、重试计数） | 雏形（时序化留平台期） |

质检分层的具身对照（具身侧三层质检 ↔ 本项目）：

| 具身质检层 | 内容 | 本项目对应 |
|---|---|---|
| 规则层 | 最短帧数/位移/角速度阈值 | 阈值断言 + T_FATAL 分级 |
| 物理层 | 自碰撞/逆解失败/遮挡 | N/A（视频脱敏域无物理约束） |
| 语义层 | 任务成败/因果链（VLM 辅助打分+人工回放） | 检测器复检=可检测性断言（语义层的弱位实现） |

平台坐标：国内两层口径见 §2.3（百舸=算力层，胜算/火山 LAS=数据层）；国际通用参照是 SageMaker/Vertex AI 一类「数据→训练→部署」托管闭环——本项目即其数据处理工作流件的微缩。

## 6. 具身智能数据全景（本项目的落地方向）

> 本项目的平台归宿是具身数据管线（[platform-mapping.md](platform-mapping.md) 具身链路定位：采集→**脱敏（本项目）**→入湖→标注→训练）。本章是那块背景板：具身模型吃什么、数据长什么样、怎么采怎么处理——不求全，铺到「每个对照都站得住」为止；环节级量级数字见 §7.1 泳道表。

### 6.1 三大模型范式与数据需求

| 范式 | 代表 | 数据需求 |
|---|---|---|
| VLA（视觉-语言-动作） | π0、GR00T、RDT-1B、Helix | 大规模遥操轨迹+互联网视觉-语言数据联合训练（π0≈1 万小时遥操+OXE） |
| 世界模型 | Cosmos（≈2000 万小时视频，NVIDIA 官方口径） | 海量无标注视频——与智驾数据引擎同源（§7.2） |
| 小脑/分层 | 快慢双系统（快系统高频控制、慢系统规划） | 高频时序控制数据，采集端硬实时对齐 |

三条关系线：VLM→VLA（语言接口）、世界模型↔VLA（预训练与仿真供给）、快慢分层（架构组织）——数据平台的供给结构因此是「轨迹+视频+仿真」三路进料。

**本项目对照**：遥操与 Ego 产出的素材正是视频主导的时序流——本管线的输入形态、格式链（§4.1）与批处理调度对这一形态直接成立。

### 6.2 数据格式：LeRobot / RLDS / HDF5

| 格式 | 形态 | 主要用户 | 特点 |
|---|---|---|---|
| LeRobot | episode 组织，视频/动作/状态分轨 | π0、GR00T 开源栈 | 社区事实标准（HF 生态） |
| RLDS | 流式 dataset 抽象 | Google 系（RT-X/OXE） | TF 生态 |
| HDF5 | 单文件大对象 | RDT-1B 等传统机器人栈 | 随机访问弱 |

三个工程要点：**模型↔格式强绑定**（RDT→HDF5、π0/GR00T→LeRobot——平台必须做格式路由）；**体积**：单条 30s 双臂轨迹原始 GB 级→编码后 10-25MB；**IO 强度**：同等样本量比 LLM 高 1-2 个数量级（150-300GB 级 vs GB 级）。

**本项目对照**：ffmpeg pipe 的「流式不物化、内存 O(1)」（§4.1）是对 IO 压力的单机答法；HDF5→LeRobot 一类格式转换，正是具身平台五类算子（对齐/重定向/清洗/格式转换/质检）中的一类。

### 6.3 采集与处理管线

- **三条采集路线**：真机遥操（废片率 30-40%、有效数据 >8 元/条、单机日产约千条）／Ego 无本体（RGB→SLAM→手部姿态→IK→重定向链路，成本收敛到真机 1/2~1/3）／仿真生成（Isaac/ManiSkill；NVIDIA GR00T Blueprint 官方口径：11 小时生成 78 万条合成轨迹≈9 个月连续人类演示）。
- **时序对齐是第一道工程约束**：硬触发 <1ms／PTP 亚毫秒；33ms=30fps 差一帧不可修复；对齐容忍窗口 ≤5ms——多模态时序数据的「脏」主要脏在时间戳。
- **三层质检**：规则（最短帧数/位移/角速度）／物理（自碰撞/逆解失败）／语义（任务成败，VLM 辅助）；原始素材约 30% 可进训练（8h 素材→2-3h 可用）。

**本项目对照**：本管线坐在「采集后、入库前」的批处理工序位——视频域算子直接复用；qc 分级闸门=规则层的微缩（§5 对照表）；关节角/力矩通道与车牌/人体同走注册表扩算子路径。

### 6.4 具身 vs LLM 数据平台：五维差异

| 维度 | LLM | 具身 | 平台设计含义 |
|---|---|---|---|
| 数据形态 | 文本 token | 多模态时序（视频+关节+力矩+语言指令） | 编解码、对齐、存储分层全变 |
| 单样本 | KB 级文本 | 30s 轨迹 GB 级（编码后 10-25MB） | IO 主导而非算力主导 |
| 标注 | 人工/偏好反馈 | 遥操即标注+事后结构化 | 标注前移到采集端 |
| 质检 | 规则+评测集 | 规则/物理/语义三层 | 物理仿真介入质检 |
| 回流 | 数据→训练→评测 | 再加真机执行回流（失败轨迹高价值） | 回流语义更重（§7.1） |

一句话：**LLM 数据平台的核心稀缺是算力调度，具身数据平台的核心稀缺是把多模态时序数据合格地搬进训练**——本项目练的正是这条搬运链的错误模型。

## 7. 行业数据闭环（具身/智驾双泳道）

闭环骨架（两条泳道共用的抽象链）：

```
采集 → 上传 → 清洗/脱敏 → 入库 → 标注 → 训练 → 评测 → 部署 → 回流 ↺
                                                        （badcase/新场景回到采集或标注）
```

### 7.1 具身泳道

| 环节 | 机制与量级（公开分享口径） |
|---|---|
| 采集 | 真机遥操（废片率 30-40%，有效数据 >8 元/条、单机日产约千条）/ Ego 无本体（成本收敛到真机 1/2~1/3，头戴千元级 vs 本体数十万级）/ 仿真生成；数采中心 10+ 家 |
| 上传/对齐 | 硬触发 <1ms / PTP 亚毫秒；33ms=30fps 差一帧不可修复；对齐容忍窗口 ≤5ms |
| 清洗/质检 | 三层（规则/物理/语义，VLM 辅助）；原始素材约 30% 可进训练（8h 素材→2-3h 可用） |
| 入库 | LeRobot/RLDS/HDF5 格式路由（模型↔格式强绑定）；单条 30s 双臂轨迹 GB 级→编码 10-25MB；同等样本量具身 IO 比 LLM 高 1-2 个数量级（150-300GB vs GB 级） |
| 标注/结构化 | 原子标签+层级标签；数据超市 Beta（10+ 企业、1000 万+ 条） |
| 训练 | π0≈1 万小时遥操+OXE；Cosmos≈2000 万小时视频（NVIDIA 官方口径） |
| 评测 | Isaac/ManiSkill 仿真批量；推理指标（Fast-WAM 类 <90ms） |
| 回流 | 真机任务结束自动回流→新版本数据集；**失败轨迹=高价值样本**（RL/偏好学习） |

行业三条路线：**NVIDIA 全家桶**（Teleop+Mimic/Dreams 合成→Isaac→GR00T/Cosmos→Jetson）/ **特斯拉·Figure 垂直整合**（工厂数据飞轮）/ **百度中立平台**（预置模板+Ego 降本+数据超市）——数据策略是分水岭，三种路线对应三种数据平台设计。

**本项目位置**：采集→**脱敏（本项目）**→入湖→标注→训练（[platform-mapping.md](platform-mapping.md) 具身链路定位节）——链路最前端的合规工序；量大利不出实时价值，决定其批处理工作流形态与「数据层工作流第一个算子节点」的平台化归宿。qc_failed 隔离区=badcase 隔离回流语义的微缩（通过层永不见未验收件）。

### 7.2 自动驾驶泳道

同一闭环骨架，两端换装：采集端从「遥操/Ego」换成**车队+触发器**（影子模式），标注端从「人工/VLM」换成 **4D 自动标注**：

| 环节 | 机制与量级（公开口径） |
|---|---|
| 车队采集 | 影子模式：候选模型车端静默推理不接管，与在役栈/人类行为做 diff，分歧即回传（Tesla 自 2016）；触发器=预测不一致/接管/极端工况，Tesla 单任务 4 个月手写 **221 个**（Karpathy CVPR 2021）、理想 200+（ICCV 2025）；10 万台具备回传能力的车+合适触发规则即可构建数据集 |
| 回传 | 车端本地缓存+压缩分片+断点续传；Tesla 官方吞吐 **50 万 clips/天**、全链 2000 万次函数调用/天、视频缓存 30 PB（AI Day 2022）；**触发命中率/回传率官方从未披露**（「十亿英里数据」是累计行驶里程非上传量，两口径勿混） |
| 长尾挖掘 | 四路信号：规则触发器 / 不确定性（熵/低置信采样）/ 模型分歧（多相机多模型预测不一致）/ 新场景聚类+多样性采样；LLM/VLM 辅助预标注与筛选（CODA-LM 2024、Cosmos Reason）。难度标尺：CODA 基准（1500 真实场景）上大规模数据集训练的检测器 mAR 跌至 ≤12.8% |
| 自动标注 | 4D 离线范式（Waymo 3DAL，CVPR 2021）：多帧检测→跟踪→按 track 精修——离线无实时约束，可用大模型+测试时增强；10% 人工+90% 自动 ≈ 100% 人工效果（AP 70.0 vs 71.2）。Tesla 多趟重建：标注每代吞吐 **~100×**，1 万 trips 的 **500 万小时人工→集群 12 小时**，离线轨迹精度 1.3cm/m（AI Day 2022）。效率倍数分母各异（小鹏 45000×、Tesla 转述 100-1000×、SAM2 仅 19% 帧需人工编辑），并列引用不可互换 |
| 数据湖 | 车队年产数百 PB，对象存储分层管理；湖仓表格式（Iceberg/Delta/Hudi）在智驾的落地**无车企一手信源**（云厂商方案+招聘 JD 佐证的推断口径）——正是本项目「数据集版本/血缘缺口」（§5）的业界对位 |
| 仿真评测 | 世界模型补长尾：NVIDIA GTC 2025 范式=路采日志→**4D 神经重建**→场景增删改→批量生成（Replicator 域随机化；Cosmos-Drive-Dreams 给出最优混合比合成:真实=1:2、VLM 拒绝采样当质检器，2025）；理想 ICCV 2025 宣告「从数据闭环走向**训练闭环**」（世界模型+RL）；合成占比 20-30%→50-60%（机构估算口径，非官方） |
| OTA/回流 | 影子验证→badcase 自动转回归场景→1% 灰度→分批全量；Tesla data engine 实例：126 条评测 clip→定向修正 13,900 条即修复「转弯误判停驻车」（AI Day 2022）；一年训 7.5 万个模型、281 个上车。安全对照终点（华为官方滚动口径）：ADS 平均 998.7 万 km/严重碰撞 vs 人驾同车队 585.6 万 |

玩家三档：**Tesla 垂直整合**（训练集群 720 节点 A100（2021）→1.4 万 GPU（2022，1 万训练+4 千自动标注）→Cortex 5 万 H100（2024）；2025-08 Dojo 团队解散转 GPU 路线；车队 600 万+辆、FSD 累计 148 亿英里（2026-09 官方计数器）；**仿真:真实配比官方未披露**）；**国内头部**（华为模型 5 天一迭代/日学超 1200 万 km、搭载破 200 万台；小鹏扶摇 600 PFLOPS+XNet；理想 1000 万 clips 后性能增长放缓→转向世界模型+RL；Momenta 30 亿 km 提炼 7000 万条「黄金数据」；蔚来 NWM 100ms 推演 216 种场景）；**云厂商卖水**（火山/腾讯带 4D 自动标注工具链、华为云八爪鱼 PB 级、阿里云汽车云份额第一）。

**本项目位置**：智驾链路里车外视频**入湖前的匿名化工序**——「云端批量脱敏」形态，对应百舸把 Meta EgoBlur（开源人脸/车牌脱敏模型）封装成平台算子的那个位置。触发器筛选决定了进管线的只是高价值片段，脱敏是这些片段入湖/合规出舱的法定前置；qc_failed 隔离区同样对应 badcase 隔离回流语义。

### 7.3 双泳道对照

| 环节 | 具身 | 智驾 | 本项目覆盖 |
|---|---|---|---|
| 采集 | 遥操/Ego/仿真 | 影子模式+触发器筛选（非全量上传） | — |
| 清洗/脱敏 | 三层质检前的一道工序 | **法定第一道工序**（7 号令/GDPR；车端实时或云端批量） | **✓ 微缩完整**（含质检闸门） |
| 质检 | 规则/物理/语义三层 | 自动标注质量断言+人工抽检（SAM2 仅 19% 帧需人工） | ✓ 分级闸门微缩 |
| 标注 | 原子标签+人工/VLM | 4D 自动标注（离线范式；效率口径 100×~45000×，分母各异） | ✗（语义对应物=qc 复检） |
| 训练/评测 | 模板化/仿真批量 | 世界模型仿真补长尾（合成占比 20-30%→50-60%，机构估算口径） | ✗ |
| 回流 | 失败轨迹高价值 | 影子 diff+badcase 转回归场景 | 语义对应物=qc_failed 隔离区 |

## 8. 边界与路线图

### 8.1 七轴自评

| 轴 | 评级 | 依据 |
|---|---|---|
| 格式链 | 强 | 7 层 8 形态 pipe O(1)（§4.1） |
| 编排调度 | 强 | 六语义全实做+契约测试锁定（§3） |
| 质量校验 | 强 | 分域+分级+隔离区+校准（§5） |
| 控制面/数据面 | 强 | 契约先行三实现，28 用例双跑（§1.2） |
| 实验方法论 | 强 | 预注册判据+失败实验可复跑（§4.3） |
| K8s 迁移 | 中 | 五步清单已设计、MVP 已预留，集群实证未做 |
| 数据集版本管理 | 弱 | 无幂等键/manifest/resume（backlog 第一优先级方向） |

### 8.2 benchmark 位移注记（2026-09-26）

[industry-benchmark.md](industry-benchmark.md) 是 2026-09-25 基线存档（四镜头冷读，其口径不随迭代自动更新）。此后的位移：

- 刀 1 相位锁相（BUG-016 候选）→ **已收口**：抽样步长取与 840=lcm(2..8) 互素，帧号 mod d 双射遍历全部残差类；数学证明+性质测试（7fps×d∈2..8）+30fps E2E。
- 刀 2 闸门判别力 → **收口一半**：T_FATAL=0.7 分级+qc_failed 隔离区落地，重采 fatal 恒 4（判别力恢复）；n=1 真实样本的校准局限仍在。
- 刀 4 控制面竞态 → **已收口**：complete/fail 围栏升级为 `(worker_id, attempt)` 复合谓词（等价 SQS receipt handle），契约测试+同名转世场景双跑锁定。
- backlog #7 SCRFD → **已执行未翻默认**：A/B 预注册判据未胜出（§4.3），opt-in 档位。
- 仍在桌：刀 3 硬解/batch 推理（免费 2-5×）、刀 5 chunk 边界 warm start、断点续跑、FR 相似度验收、血缘 manifest（对照 [industry-benchmark.md](industry-benchmark.md) backlog 表）。

### 8.3 路线图指针

- 演进三段：平台配套补全（心跳/DLQ/血缘/幂等键/观测时序化）→ Redis 控制面+Worker 容器化 → 百舸任务式上云三档实测（design §7.4）。
- 具身化演进（换算子、不换控制面）：视频脱敏 → 多模态清洗（抽帧/过滤/去重）→ LeRobot/HDF5 格式转换算子——每步=算子注册表新条目+KernelParams 新配置，控制面与质检闸门零改动（§6 全景）。
- 已知限制与演进 10 条：[README](../README.md) 已知限制节。
- 实施计划开放细化项 12 条：`docs/superpowers/plans/2026-09-23-face-blur-lite-mvp.md`。
