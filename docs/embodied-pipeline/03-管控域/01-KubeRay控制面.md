# 01 · KubeRay 控制面（CRD / reconcile / 三层伸缩 / 高可用）

> 03-管控域第 1 篇。定位：**控制面平台视角**——KubeRay 如何把 Ray 集群纳入 K8s 的声明式控制（CRD 期望态 → operator reconcile → 实际态收敛）。边界：Ray 的 Task/Actor/Object Store 执行模型与集群命令 = [调研笔记-02](../92-调研笔记/调研笔记-02-Ray分布式框架调研与管线应用.md) §1–§2；「数据面哪种算子用哪个 CRD 承载」= [算子设计-03](../02-生产域/算子设计/算子设计-03-算子执行与编排规范.md) §1/§6；跨团队的准入/配额/抢占归 [02-调度与多租户](02-调度与多租户.md)；集群/作业故障后的状态推进与恢复归 [03-任务状态机与容错](03-任务状态机与容错.md)。本篇只讲**控制器本身怎么把集群管起来、管健康**。

## 核实状态（2026-10-17）

- ✅ 已核实：三 CRD（RayCluster/RayJob/RayService）与 RayJob 两种提交模式（submit / create-cluster）、Ray 两级调度与 GCS/Object Store 角色、L2 常驻池 / L3-L4 按需的落位（与 [调研笔记-02](../92-调研笔记/调研笔记-02-Ray分布式框架调研与管线应用.md) §1/§2/§5 一致）
- ⚠️ 设计通识：下文 CRD 字段名（`headGroupSpec`/`workerGroupSpecs`/`autoscalerOptions`/`submissionMode`/`upgradeStrategy` 等）、reconcile 循环、三层伸缩、GCS FT、RayService 蓝绿为 KubeRay 官方通用机制，**字段名随 KubeRay 版本演进有差异**（如 `submissionMode` 旧称 CRDMode、新版称 K8sNative），落地以部署版本 API 为准
- ❓ 待补：生产 KubeRay/Ray 版本兼容矩阵（调研笔记-02 §8 已列待确认）、GPU 机型 worker 组划分、扩缩容阈值与空闲回收时长——待团队标定

## 1. 定位与边界

- **本篇管**：把「一个 Ray 集群」作为 K8s 期望态来管理——集群怎么被声明、控制器怎么调谐、节点怎么扩缩、控制面怎么高可用
- **与算子-03 §6 的分工**：算子-03 回答「这条数据面任务该放进 RayCluster 还是 RayJob」（**选型**）；本篇回答「控制器收到 CRD 后，怎么把集群真正起出来并维持健康」（**控制**）
- **面分离**：控制面 = KubeRay operator + Ray head（GCS / 调度器 / autoscaler）；数据面 = Ray worker 上的 Actor/Task。控制面不直接命令「跑哪个算子」，只对齐集群期望态（[总线 §1.2](../00-总体架构/README.md) 面分离原则）

## 2. 核心概念与组件

| 组件 | 归属 | 职责 |
| --- | --- | --- |
| KubeRay operator | 控制面（Deployment，常驻） | watch 三类 CRD，执行 reconcile 循环，调谐 Pod/Service/Job |
| RayCluster CRD | 期望态 | 描述一个长期集群：1 个 head + N 个 worker 组 |
| RayJob CRD | 期望态 | 一次性作业（entrypoint）+ 可选的内嵌集群 spec |
| RayService CRD | 期望态 | 常驻 Ray Serve 应用（serveConfig）+ 集群 spec，支持零宕机升级 |
| Ray head | 控制面（集群内） | GCS（全局元数据）、调度器、Dashboard、Job Submission Server、autoscaler |
| Ray worker | 数据面 | Raylet（本地资源管理）+ Object Store + Actor/Task 执行 |
| Ray autoscaler | 控制面（in-tree，跑在 head） | 看 Ray **逻辑资源**缺口，决定需要多少 worker |

> **与负载均衡控制面同构**（叙事核心锚点）：CRD 期望态 ≈ LB 配置声明，KubeRay operator ≈ 控制面服务，Ray worker ≈ 转发实例；被管对象从 LB 实例换成 Ray 集群，控制范式（声明式、调谐、level-triggered）完全一致。

## 3. 控制面机制：reconcile 循环

```
        ┌─────────────────────────────────────────────────────────┐
        │  期望态（desired）= CR spec（headGroupSpec / worker 组范围） │
        └─────────────────────────────────────────────────────────┘
                  │  watch（add/update/delete）
                  ▼
        ┌──────────────────────┐
        │  KubeRay operator    │  ① 读 CR spec
        │  reconcile loop      │  ② 查实际态：Pod 状态 + GCS 注册节点数
        │  （幂等、可重入）      │  ③ diff：缺 head 起 head、缺 worker 起 worker、
        └──────────────────────┘      多了/坏了则替换/回收
                  │  ④ 调谐：create/update/delete Pod、Service、Job
                  ▼
        ┌─────────────────────────────────────────────────────────┐
        │  实际态（actual）= 运行中的 Ray 集群（head + workers）      │
        └─────────────────────────────────────────────────────────┘
                  │  状态回写 CR.status（conditions / endpoints）
                  └──────────────────────────► 回到 ①（持续对齐）
```

要点：

- **level-triggered 而非 edge-triggered**：控制器不依赖「收到一次命令执行一次」，而是周期性/事件驱动地**对齐期望态**；中途错过事件、进程重启，下一轮 reconcile 仍能收敛——这是控制面自愈的根基
- **幂等**：同一 spec 被 reconcile 多次结果一致；创建前先查是否已存在，避免重复起集群
- **状态回写**：调谐结果写回 `CR.status`（集群 endpoint、就绪 worker 数、condition），供 [03 状态机](03-任务状态机与容错.md) 消费

## 4. 三类 CRD 与关键字段

### 4.1 RayCluster——长期常驻池

- **用途**：L2 感知重建 GPU Actor 池（模型权重常驻、跨分片复用，见 [调研笔记-02](../92-调研笔记/调研笔记-02-Ray分布式框架调研与管线应用.md) §6）
- **关键 spec（通识，字段名以版本为准）**：
  - `rayVersion`：对齐集群 Ray 版本
  - `headGroupSpec`：head Pod 模板（`template` 资源/探针）、`serviceType`（ClusterIP/NodePort）、`rayStartParams`
  - `workerGroupSpecs[]`：可声明多个组，每组 `groupName` + `replicas`/`minReplicas`/`maxReplicas` + Pod `template`（`nodeSelector` 选 GPU 型号、`resources.limits` 声明卡数）
  - `enableInTreeAutoscaling` + `autoscalerOptions`：开启并配置 Ray 自带伸缩
- **GPU 分组原则**：按显存/型号分 worker 组（如 `gpu-a100` / `gpu-l40`），大模型独占卡（`num_gpus=1`），禁止 MoGe/SAM2 混跑同卡（OOM 红线，调研笔记-02 §6.5）

### 4.2 RayJob——一次性作业

- **用途**：L3 语义标注、L4 清洗封装、Golden Set 定时回归（无状态批处理）
- **两种提交模式（`submissionMode`）**：
  - **K8sNative（旧称 CRDMode）/ create-cluster**：operator 为作业起一个临时集群，在 head 上跑 `entrypoint`，适合隔离、可跑完即销
  - **HTTPMode / submit**：通过 Job Submission Server 提交到**已存在的常驻 RayCluster**，省冷启动
- **关键 spec（通识）**：`entrypoint`（启动命令）、`runtimeEnvYAML`（依赖/环境）、`rayClusterSpec`（内嵌集群）、`shutdownAfterJobFinishes`（跑完销毁）、`ttlSecondsAfterFinished`（完成后保留多久再清理）、`backoffLimit`（失败重试次数）、`activeDeadlineSeconds`（超时）、`suspend`（暂停/恢复）
- **生命周期**：提交 → （按需）起集群 → 跑 entrypoint → 成功/失败 → 按 `shutdown`/`ttl` 清理；作业状态机 Pending→Running→Succeeded/Failed 与 03 篇衔接

### 4.3 RayService——常驻服务

- **用途**：常驻质检/双模型互检/伪 GT 生成等在线服务（算子-03 §1 已说明 Ray Serve 是库层 API，K8s 长驻形态对应 RayService CRD）
- **关键 spec（通识）**：`serveConfigV2`（Serve 应用声明）、`rayClusterSpec`（后端集群）、`upgradeStrategy`（升级策略）
- **零宕机升级（蓝绿）**：新版本先拉起一个**新集群**并部署 Serve → 健康检查通过后再切流 → 旧集群保留一段时间可回滚，避免「边升级边杀掉常驻服务」

## 5. 三层伸缩：谁在决定扩多少节点

这是控制面最容易讲混的点，必须分清楚三层各自看什么、做什么：

| 层 | 运行位置 | 输入信号 | 决策/动作 |
| --- | --- | --- | --- |
| ① Ray autoscaler（in-tree） | Ray head | Ray **逻辑资源**：任务/Actor 申请了 `num_gpus` 但集群无空闲（挂起的资源请求） | 算出「还需 N 个某 worker 组的节点」，注意它**不看 CPU 利用率**，看的是未满足的资源请求 |
| ② KubeRay operator | K8s 控制面 | autoscaler 的扩缩请求 + CR 的 min/max 范围 | 把逻辑需求变成**物理 Pod**：创建 worker Pod（触发节点加入）；缩容时优雅 drain 后删除 |
| ③ K8s Cluster Autoscaler | K8s / 云 | worker Pod 处于 Pending（无节点可调度） | 向云申请**新主机节点**；节点空闲则回收 |

- **缩容**：`autoscalerOptions.idleTimeoutSeconds`（通识默认约 60s，需按长任务调大）控制空闲多久回收；缩容必须**优雅 drain**，不能强杀正在跑的重建 Actor（否则丢任务，触发无谓重跑）
- **为什么不用 HPA**：K8s HPA 看 CPU/自定义指标扩「Deployment 副本」，不理解 Ray 这种「一个集群内按逻辑资源动态增减节点」的模型；Ray 必须用自己的 autoscaler，否则逻辑资源视图与物理节点会错位
- 三层链路是「**逻辑需求 → 物理 Pod → 物理主机**」逐级放大，每一层都有自己的配额与冷却（02 篇讲配额如何卡住第②③层）

## 6. 高可用与故障自愈

- **worker 故障（常态）**：worker Pod 崩溃/被驱逐 → operator 重建 Pod；Ray 检测节点丢失后把上面的任务/Actor 重调度到存活节点（这是 [算子-03 §7](../02-生产域/算子设计/算子设计-03-算子执行与编排规范.md) 任务级重试能成立的物理基础）
- **head 故障（关键单点）**：head 承载 GCS 全局元数据：
  - **开启 GCS Fault Tolerance（生产推荐）**：Ray 2.x 支持 GCS 状态持久化到外部存储（如 Redis）；head Pod 重建后 GCS 从外部存储恢复，worker 重新注册，集群不丢
  - **未开启 FT**：head 挂 = 集群元数据丢失，需重建集群并从检查点恢复（恢复语义归 [03 篇](03-任务状态机与容错.md)）
- **autoscaler 单例**：autoscaler 跑在 head 上，head 重建期间伸缩暂停，但不影响已在运行的任务
- **健康探针**：head/worker 配置 readiness/liveness（Ray Dashboard 端口等），未就绪不接流量/不计入可用节点
- **池化 vs 按需的成本权衡**：

| 策略 | 形态 | 收益 | 代价 | 适用 |
| --- | --- | --- | --- | --- |
| 常驻 | RayCluster（min≥1） | 无冷启动、权重常驻 | 空闲 GPU 空转 | L2 高频感知重建 |
| 按需 | RayJob（shutdown=true） | 无空闲成本 | 每次拉集群+加载模型，冷启动慢 | L3/L4 低频批处理 |
| 折中 | 常驻最小池 + 弹性上限 | 兜底低延迟，高峰再扩 | 参数需调优 | 负载有基线又有波峰 |

## 7. 控制面指标（L2/L3 落点）

| 指标 | 含义 | 用途 |
| --- | --- | --- |
| 集群就绪时间（ready latency） | CRD 提交到集群 ready 的耗时 | 按需池冷启动成本；SLO 输入 |
| 扩容/缩容时延、扩容成功率 | autoscaler 发请求到节点可用 | 高峰能否及时补卡 |
| 节点 churn（重启/替换次数） | worker/head 重启频率 | 故障与抖动信号 |
| GPU 分配率/利用率 | requested vs allocatable vs 实际占用 | 发现空转/超卖，L3 成本 |
| 常驻池命中率 | 作业直接命中常驻池 vs 按需拉起 | 评估池化收益 |

> 数据面指标（样本/小时、P50–P99、失败率、GPU 秒/样本）归 [算子-03 §10](../02-生产域/算子设计/算子设计-03-算子执行与编排规范.md)；本篇只采集**控制面把集群管得好不好**的指标。

## 8. 控制面规模与性能（大批量作业下控制面自身）

- **规模画像**：数据平台是「批量作业密集」场景——日批触发、回流补采、Golden Set 回归（G6）同时压上来，**千级 RayJob/RayCluster CR 并存是常态**；控制面自身（KubeRay operator、reconciler、etcd、准入链路）必须在大批量并发下保持可用——**控制面是平台的瓶颈候选，不是想当然的稳定层**
- **etcd watch 风暴**：千级 CR 同时更新 status → watch 事件洪峰；对策（业内通识）：status 写频率限流（合并小更新）、watch 按需订阅、CR 拆分（高频状态写 status 不整对象更新）——与 XLB 控制面「配置变更风暴 → 发布限流/批量 diff」同源问题
- **大批量并发提交**：编排器批量 reconcile 按优先级/依赖**分片收敛**，不做单轮全量扫描（对齐 [04 篇 §6](04-PipelineSpec与编排DSL.md) diff 只调度缺失）；准入链路（Kueue/Volcano）走批量校验与批量准入，不做逐作业串行
- **API 吞吐与乐观并发**：状态写用 CAS（`resourceVersion` 冲突重读再试，[03 篇 §3](03-任务状态机与容错.md) 同款）；控制面 API 吞吐（每秒作业提交/状态更新）是容量规划输入
- **水平扩展前提 = 控制面无状态**：reconciler 多副本 + 按队列/租户分片；状态全在存储层（etcd 高可用集群），内存不保存任何作业状态（§3 同范式）——无状态是「加了副本就线性扩容」的前提
- **控制面自身 SLO**：可用性 ≥99.9%、准入决策 P95 时延、reconcile 收敛时延预算——控制面自身的观测归 [横切 01](../90-横切/01-可观测性.md)（观测观测者）

## 9. 与其他域/文档的衔接

- **上游**：[02-调度与多租户](02-调度与多租户.md) 先做准入（Kueue 决定作业能否进、进哪个队列、配额够不够）；准入后本篇负责把集群起起来
- **下游**：集群 ready → 数据面 Actor/Task 执行（算子-03 §1/§6）；集群与作业状态事件喂给 [03-任务状态机与容错](03-任务状态机与容错.md)
- **声明式入口**：用户提交的 PipelineSpec 如何编译成 RayJob/RayCluster 提交，归 [04-PipelineSpec 与编排 DSL](04-PipelineSpec与编排DSL.md)
- **成本**：GPU 分配率/池命中率与 [算子设计-06](../02-生产域/算子设计/算子设计-06-算子成本模型.md) GPU 秒/样本口径联动

## 10. 红线与速讲话术

**红线**：

1. **不要用 HPA 直接伸缩 Ray worker**：绕过 Ray autoscaler 会让逻辑资源视图与物理节点错位；Ray 集群必须走自带 autoscaler（§5）
2. **大模型 GPU 独占、按型号分 worker 组**：混跑同卡即 OOM 风险（调研笔记-02 §6.5）
3. **head 是关键单点**：生产必须开 GCS FT（状态落外部 Redis），否则 head 挂就要整体重建、从检查点恢复
4. **缩容必须优雅 drain**：禁止强杀长时重建 Actor，长任务池要调大空闲回收时长
5. **常驻/按需分离**：低频任务不常驻，避免 GPU 空转；高频 L2 池不按需，避免反复冷启动
6. **控制面不越界**：operator 只对齐集群期望态，不直接下发算子执行命令（面分离红线）

**速讲话术（30 秒）**：

> "我们用 KubeRay 把 Ray 集群纳进 K8s 声明式控制，三个 CRD 分工：RayCluster 管常驻 GPU 池、RayJob 管一次性批处理、RayService 管常驻服务。KubeRay operator 跑的就是标准 reconcile 循环——监听 CRD 期望态，比对实际 Pod 和 GCS 注册节点，缺了补、坏了换，再把状态写回 status，这和我在 XLB 做的负载均衡控制面是同构的，都是 level-triggered、可重入。伸缩是三层协同：Ray 自带 autoscaler 看挂起的逻辑资源请求算要几个 worker，KubeRay 把它变成物理 Pod，Pod 没节点再交给 K8s Cluster Autoscaler 扩主机，这里不能用 HPA，因为它不理解 Ray 的逻辑资源视图。高可用上 worker 挂了自动重建，关键是 head 里的 GCS 是元数据中枢，生产要开 GCS 故障容忍、把状态落外部 Redis，否则 head 挂了整个集群要从检查点重建。部署上 L2 感知重建池常驻省冷启动，L3、L4 语义和清洗跑 RayJob 按需销毁。"
