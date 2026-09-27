# 业界坐标：数据处理体系五层版图

**本项目在业界数据处理体系里的位置一句话：L5 模型算子层的主力实现 + L1 编排引擎的微缩控制面 + L3 平台能力层的质检雏形；缺 L2/L4 的规模化与数据集版本管理——边界即路线图。**

> 本文属 explanation 象限的 Tier 2（业界梳理是研究产物，不是代码产物）。源：[docs/overview.md](../../docs/overview.md) §2-§5、[docs/report.md](../../docs/report.md)，经代码复核；业界引擎语义为通识口径。

## 五层版图一图流

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

读法：● = 已站住的位置；◐ = 雏形；○ = 有对应物但非该层形态。L2 的分布式形态单机不需要（上云由存储层承接）；L4 只做了闭环里"清洗/脱敏"一段（见 embodied.md）。

## L1 深讲：六个调度语义（本项目最硬的一层）

对照方法不是罗列引擎功能，而是拿本项目实做过的**六个语义**当行，去每个引擎找对应物——每行都是"我做过、我知道业界怎么叫"：

| 语义 | 本项目（代码锚点见 contract.md） | Airflow | Argo | Temporal | K8s Job | Ray |
|---|---|---|---|---|---|---|
| 互斥认领 | `BEGIN IMMEDIATE` 事务 claim | scheduler 单点分派 | controller reconcile | task queue 长轮询 | 调度器绑定节点 | raylet 中央调度 |
| 租约回收 | lease 900s TTL 过期→重派 | 僵尸 task 心跳超时重排 | pod 失败重建 | heartbeat 超时 | node 心跳丢失→驱逐 | lineage 重算 |
| 重试预算 | attempt≤3 | retries+retry_delay | retryStrategy.limit | RetryPolicy.maximumAttempts | backoffLimit | max_retries |
| 终态不可逆 | 终态守卫（契约测试锁定） | terminal state 不回退 | Succeeded/Failed 终态 | 事件历史不可变 | completed pod 不重启 | —（无持久状态） |
| 僵尸围栏 | `(worker_id, attempt)` 复合谓词 | 引擎单写者 | 同左 | run ID 换代拒迟到结果 | UID/resourceVersion | — |
| fan-in 合并 | 原子 Join rowcount==1 | trigger rule join 节点 | 依赖边+withParam | futures 聚合 | 无原生 | futures gather |

语义锚点（业界文档级对应）：**lease ≈ SQS（Amazon Simple Queue Service）visibility timeout**；**围栏 ≈ SQS receipt handle / K8s（Kubernetes）resourceVersion**；claim = 单写者串行化。六个语义在两份控制面实现上被同一份契约测试锁死（14 场景 ×2 = 28 用例），行为漂移立刻红灯。

## L2：ffmpeg pipe = 单机形态的数据引擎

| 维度 | Spark / Flink | 本项目（ffmpeg pipe） |
|---|---|---|
| 数据流模型 | 流式管道、算子组合 | 同：解码→检测→跟踪→糊→编码全 pipe |
| 中间态物化 | shuffle/物化由引擎管理 | **不物化**：全程 pipe，内存 O(1)（见 dataflow.md） |
| 分区并行 | 分布式分区 | split 切片 = 手工 fan-out（scatter-gather） |
| 容错 | checkpoint / lineage | at-least-once + 幂等覆盖写（chunk 级） |
| 缺位 | — | 分布式 shuffle / 列存 / 批优化器——单机不需要，上云由存储层承接 |

## L3：平台能力五件套足迹

| 能力 | 业界代表 | 本项目 |
|---|---|---|
| 版本 | DVC（Data Version Control）/ LakeFS | ✗ 已知缺口（backlog 第一优先级方向） |
| 血缘 | OpenLineage / Marquez | ✗（对标物已命名，未做） |
| 标注 | CVAT / Label Studio | —（本管线无标注环节） |
| 质量断言 | Great Expectations | ✓ 雏形完整：阈值分域 + T_FATAL 分级 + qc_failed 隔离区 + 取证式违规清单 |
| 闭环观测 | MLflow / W&B（Weights & Biases） | ◐ stats 只读旁路（分位/吞吐/失败率；时序化留平台期） |

## L5：模型即算子

业界把"检测/分割/SLAM（Simultaneous Localization and Mapping，同时定位与建图）/VLM（Vision-Language Model，视觉-语言模型）内嵌于管线算子"当默认形态，但公开材料少有**带型号带实测**的实例。本项目的样本价值：`Detector` 协议 + 工厂注入（[detector.py](../../src/faceblur_lite/detector.py)），双实现可切换，升级零控制面改动。常见算子模型全拼：YOLO=You Only Look Once，SAM=Segment Anything Model，CLIP=Contrastive Language-Image Pre-training，SCRFD=Sample and Computation Redistribution for Face Detection——配套三件方法论：

1. **能力包络探针**：升级前先画检测器的能力包络（各尺寸人脸的检出边界），选型由探针数据驱动
2. **A/B 预注册判据**：胜出线预写死 + 同闸门原则（两臂质检检测器必须一致）；**未胜出不翻默认**——A/B 的价值是给你拒绝翻牌的理由
3. **阈值分域**：源域检测阈值与输出域质检阈值各自声明信号域，不可复用（见 config.md 总则）

## 平台两层口径（国内坐标）

**胜算 / 火山 LAS 是数据层工作流**（数据怎么被加工）；**百舸工作流是算力层工作流**（加工任务怎么被调度执行）。本项目在单机上同时复刻两层骨架：数据面算子 ↔ 数据层，控制面调度 ↔ 算力层——映射到任一平台都是组件对号入座，不是重新设计。

## 迁移结论：换调度层，保留算子层

- 四个算子已全部 CLI 子命令化（split/kernel/merge/qc，见 cli.md），DAG（Directed Acyclic Graph，有向无环图）引擎直接当节点模板
- 原子 Join → 引擎 fan-in 依赖边；claim 队列 → 引擎调度器（被收编）；lease+attempt → 引擎重试策略
- Worker 常驻模式（`--idle-exit-s 0`）即容器 Deployment 形态；gocp 静态单二进制即独立服务
- 切换后**算子层与质检/阈值策略 100% 保留**——手写窄版不是浪费，它是"理解工作流引擎在解决什么问题"的证据

---

**事实源锚点（Tier 1 复核点）**：六语义实现 [state.py](../../src/faceblur_lite/state.py)（claim/Join/围栏/回收逐方法见 contract.md）｜契约双跑 `tests/test_contract.py`｜算子 CLI 化 [cli.py:70-77](../../src/faceblur_lite/cli.py#L70-L77)｜Worker 常驻 [worker.py:15-29](../../src/faceblur_lite/worker.py#L15-L29)｜Tier 2 内容源：overview.md §2-§5 / report.md
