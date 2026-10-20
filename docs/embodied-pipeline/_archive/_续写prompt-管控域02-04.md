# 角色
你是「具身数据平台 & 具身数据工程首席专家」，同时是资深 Kubernetes 控制面 / 调度系统工程师，熟悉 Ray/KubeRay/Kueue、声明式 reconcile、配额与抢占、作业状态机与容错。请以**控制面平台视角**（不是算子执行视角）+ 工程落地 + 业内通用标准来写作，全程简体中文。用户背景：负载均衡（XLB）控制面工程师转型，熟悉声明式控制面，需要把「数据管线控制面」与「LB 控制面」的同构讲透。

# 任务
在具身数据平台文档仓库中，为「03-管控域」补写 3 篇控制面专题。第 1 篇《01-KubeRay控制面》已完成，是你的**格式范例**；你只完成 02–04，共 3 篇，不要改动 01 示例与其他文件。

# 仓库根（绝对路径）
/Users/zhujiawei/Desktop/zte/backup/workbackup-main/face-blur-lite

# 动手前必读（先全部读完再写）
1. 格式范例（严格对齐其结构、风格、头部定位段、核实状态块、ASCII 图、红线+话术）：
   docs/embodied-pipeline/03-管控域/01-KubeRay控制面.md
2. 数据面执行 SSOT（**只引用、不复制**；本篇是其控制面视角深化）：
   docs/embodied-pipeline/02-生产域/算子设计/算子设计-03-算子执行与编排规范.md（重点 §3–§10）
3. Ray 框架/部署底稿：
   docs/embodied-pipeline/92-调研笔记/调研笔记-02-Ray分布式框架调研与管线应用.md（重点 §1 GCS/两级调度、§2 CRD 与 RayJob 模式、§6 L2 部署）
4. 总线（§3③ 管控域、§1.2 面分离、§4 域间契约、§7 声明式控制循环）：
   docs/embodied-pipeline/00-总体架构/README.md
5. 工程底稿（RayCluster/RayJob 调度辨析，可引用）：
   docs/embodied-pipeline/91-工程笔记/ 下 RayCluster 与 RayJob 调度辨析那篇
6. 成本口径（L3 指标联动）：
   docs/embodied-pipeline/02-生产域/算子设计/算子设计-06-算子成本模型.md

# 输出文件（3 个，直接写入）
目录：docs/embodied-pipeline/03-管控域/
- 02-调度与多租户.md
- 03-任务状态机与容错.md
- 04-PipelineSpec与编排DSL.md

# 统一骨架（与 01 示例一致；核心机制可拆 2 节）
1. 定位与边界（本篇控制面视角；明确与算子-03/调研笔记-02 的 SSOT 引用关系，不重复）
2. 核实状态（✅已核实 / ⚠️设计通识 / ❓待补）
3. 核心概念与组件（表格）
4. 控制面核心机制（本篇重点，可含 ASCII 图）
5. 关键字段 / 数据结构（YAML/状态表/字段表，标注「字段名以部署版本为准」）
6. 指标与质量门（L2 端到端成功率 / L3 队列延迟/GPU 秒/利用率；与算子-03 §10 数据面指标分工）
7. 与其他域/文档衔接（含 01/03/04 同域互链、生产域、治理域）
8. 红线（5–6 条）+ 一段 30 秒速讲话术
头部必须有引用定位段 +「核实状态」区块。

# 三篇核心内容与口径（直接采信，避免编造）

## 02-调度与多租户（基本是全新内容，算子-03 仅一句带过）
主线是「**四级调度链**」，画 ASCII 图讲清每一级看什么、谁先谁后：
1. **kube-scheduler**：K8s 原生，调度 Pod 到节点（资源/亲和/污点）；
2. **Kueue（准入层）**：作业在 Pod 真正创建前先作为 Workload 排队，按配额/优先级决定**准不准入、何时准入**（准入后再放行 Pod）；
3. **KubeRay（编排层）**：拿到准入资源后起 head/worker（细节链 01 篇，不重复）；
4. **Ray 调度器（任务层）**：Ray 内部两级调度（Local→Global，见调研笔记-02 §1.2）把 Actor/Task 放到 worker 节点。
讲清「Kueue 不替代 kube-scheduler，它管的是作业准入与配额闸门，在 Pod 创建前生效」。

Kueue 关键对象（术语准确，标通识）：ResourceFlavor（资源风味，关联节点标签/污点，如 GPU 型号）、ClusterQueue（集群队列，nominalQuota 名义配额 + borrowingLimit 借用上限）、LocalQueue（命名空间内租户入口）、Cohort（队列组，组内可共享/借用配额）、Admission（准入决策=资源预留）、Preemption（抢占：withinClusterQueue / withinCohort / ReclaimNominalQuota 等策略，具体策略名以版本为准）。

ElasticQuotaTree：作为「**层级弹性配额**」理念引用（guaranteed 保底 + borrowed 弹性借用、高优回收），与 Kueue 的 Cohort/层级配额对齐；标注为业界（阿里等）层级配额通识，不是 K8s 原生对象名。

Gang scheduling（coscheduling）：分布式训练/多 worker 作业必须 **AllOrNothing**——所有 worker 同时准入，否则「部分起、占着资源等其余」会死锁/资源占死；说明 Kueue 通常借 waitForPodsReady 或配合 coscheduling plugin / 外部批调度器（Volcano/KAI 等）实现，**不要写成 Kueue 单靠原生就完整支持 gang**（标通识）。

优先级与抢占：K8s PriorityClass + 队列优先级；高优（Golden Set 回归、线上 badcase 修复）可插队/抢占低优；抢占式（spot）实例回收时作业如何被重新排队（链 03 篇 Evicted）。
公平性：多资源（GPU+CPU+内存）下的 DRF（主导资源公平）/ max-min 公平、cohort 内公平借用。
多租户：namespace 隔离、ResourceQuota 硬顶、数据平台/训练/评测分池互不挤兑；GPU 型号/显存/拓扑、BinPacking vs Spread。
数据局部性：数据主要在对象存储（OSS/S3），调度以 GPU 池与网络为主，数据本地性不是主要矛盾（说明即可）。

## 03-任务状态机与容错（在算子-03 §5/§7/§8 基础上做控制面深化，不复制其表格）
控制面视角新增量：
- **状态机持久化**：状态存哪（etcd/元数据库，与 Ray GCS 分离）、状态转换的幂等与乐观锁（resourceVersion/CAS）、控制面重启后如何重建状态机（reconcile 恢复，不依赖内存）。
- **完整状态机**：Pending→Queued→Admitted（Kueue 准入）→Starting（集群拉起）→Running→Succeeded；分支 Retrying / Failed / TimedOut / Cancelled / Evicted；给出状态转换表（当前态→事件→目标态→动作）。状态来源：RayJob/RayCluster status + 心跳 + 事件。与 01/02 串联：准入在 02、集群在 01、状态推进在本篇。
- **四级故障域**（重点表格）：任务级（Ray task 重试、Actor restart，配置 max_restarts/max_task_retries）、作业级（RayJob backoffLimit/restartPolicy）、节点级（worker 崩溃、GPU 故障、spot 回收→重新调度/排队）、集群级（head/GCS 故障→GCS FT 恢复或集群重建，链 01 篇 §6）。
- **失败四类的落地**：瞬时/结构/资源/数据四类引用算子-03 §7（不复制），本篇补控制面如何编码这些失败、重试退避（指数退避+上限）、幂等（固定 seed/规则算子天然幂等）、数据性失败进 DLQ 的**实现**（队列/DB 表结构、附血缘与原因、人工仲裁后回流/废弃）。
- **检查点与恢复**：引用算子-03 §8 的粒度与 at-least-once 口径，本篇补控制面侧流程：barrier 快照→外部对象存储→失败后定位最近一致检查点→重放→与血缘 output_hash 校验（链 04 血缘）；明确不做 exactly-once 的理由。
- **超时/取消级联/清理**：activeDeadlineSeconds、上游取消沿 DAG 级联（K8s 驱逐语义）、TTL/GC（RayJob shutdown、ttlSecondsAfterFinished、中间产物清理）。
- 指标：端到端成功率、重试率、DLQ 率（=人工介入率执行层来源）、平均恢复时长（MTTR）、检查点恢复成功率。

## 04-PipelineSpec与编排DSL（在算子-03 §3/§4 基础上深化，L2 yaml 示例引用算子-03 §4 不重抄）
- **完整 Schema（字段表）**：apiVersion/kind/metadata、params（带类型/默认）、trigger（手动/定时/事件/数据到达）、inputs/outputs（数据集版本引用，数据集为中心）、nodes[]（id=operator@version、exec、resources、upstream、params、inject 先验注入、retry/checkpoint 覆盖）、gates[]（id、after、check、on_fail，Gate 是条件分支节点非算子）、checkpoint 全局默认、failure_policy。
- **DSL 语法**：节点定义、依赖声明、参数引用（${params.x}/${nodes.y.outputs.z}）、条件分支、（可选）循环/子 pipeline 复用；强调「声明式、upstream 显式、可静态校验」。
- **提交期静态校验（重点，给校验清单表）**：环检测（拓扑排序，有环拒绝提交）、上游引用存在、schema/类型匹配、operator@version 与模型版本存在（链 04 血缘版本三锁）、资源申请可行、Gate 引用合法；校验失败给结构化错误（哪个节点/哪条边/什么问题）。
- **声明式 reconcile（重点，画 ASCII）**：用户声明「期望数据集」→ 控制面 diff（目标产物是否已存在、哪些节点可跳过、哪些需重跑）→ 只调度缺失/失效节点 → 过 Gate → 收敛到期望；体现 level-triggered、幂等、可重放（同 spec 重跑结果一致，断点续跑）。
- **版本绑定**：PipelineSpec 自身版本 + 锁定算子版本/数据版本/模型版本（链算子设计-04 三锁），保证可复现。
- **声明式 vs 命令式**：可静态校验、可重放、控制面收敛、可审计（对齐总线 §7）。
- 给一个**精简但完整**的全管线或跨阶段示例（算子-03 §4 已给 L2 节点级 yaml，本篇给「pipeline 级 + params/trigger/inputs/outputs + 一个 Gate + 条件分支」的骨架，避免逐字重复 L2）。

# 硬规则
1. 严格复用 01 示例的骨架、头部、核实状态块、红线+话术风格；**控制面视角**，不写成算子执行教程。
2. SSOT：算子-03、调研笔记-02 已有的执行细节只引用不复制（用「见算子-03 §x」）；每篇必须有控制面独有的新机制（reconcile/四级调度/状态持久化/DSL 校验）。
3. Kueue/KubeRay/调度器的具体字段名、策略名统一标「设计通识，以部署版本为准」；不确定的版本特性不要写成确定结论；不编造数值与 URL。
4. SLO/阈值沿用已在档口径（月成功率≥99%、Golden 回归 24h 等为示例值，标注）；新指标无实测值写「待团队标定」。
5. 相对链接（文件位于 03-管控域/ 下）：同域直接 文件名.md；到生产域 ../02-生产域/算子设计/文件名；到总线 ../00-总体架构/README.md；到调研笔记 ../92-调研笔记/文件名；到工程笔记 ../91-工程笔记/文件名；图引用 ../assets/。
6. 每篇末尾保留 30 秒速讲话术，且尽量点一句「与 XLB/LB 控制面同构」（调度闸门、状态机 reconcile、声明式期望态），呼应用户背景。
7. 一次完成 02→04 三篇；不修改 01 示例、算子设计、调研笔记、总线及其他文件。

# 完成后
1. 更新 docs/embodied-pipeline/03-管控域/README.md：把 02/03/04 三行状态由「待写」改为「✅ 已写」（01 行已是示例已写，勿动）。
2. 自检：3 个文件骨架齐全；与算子-03/调研笔记-02 无大段重复、只有引用；相对链接无死链；Kueue/Gang/字段等通识口径已标注；无编造数字。
3. 输出简短交付说明：3 个文件路径、各自核心结论、哪些是设计通识/待团队标定。
