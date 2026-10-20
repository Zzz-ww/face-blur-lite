# 调研笔记 02：Ray 分布式框架调研与管线应用

> 调研底稿：《具身数据管线工程笔记》第二章由本篇提炼而成。本篇记录 Ray 分布式框架的调研：核心架构与执行模型、裸机与 K8s 部署形态、生态组件与训练框架边界，及其在具身数据管线 L1~L4 的执行形态落位与 L2 感知重建层的 RayCluster 部署设计。

## 0. 调研背景与问题

- Ray 是什么：面向分布式计算/调度的开源框架，统一承载任务执行、数据加工、分布式训练与在线服务。
- 为什么管线场景会选它：弹性扩缩、异构资源、任务与数据管线强耦合，比自写调度省得多。
- 要回答的问题：
  1. Ray 的核心架构和执行模型是什么？
  2. 部署到 K8s 的形态（RayCluster / RayJob）怎么组织？
  3. 生态组件（Data / Train / Serve / RLlib）各管管线哪一段？
  4. 和主流分布式训练框架比，边界在哪？

## 1. Ray 核心架构与执行模型

### 1.1 架构总览

- 控制面：GCS（Global Control Store）——集群元数据、命名、发布订阅中枢；调度器负责任务分发。
- 节点：Head（跑 GCS + 调度器 + Driver）与 Worker（跑任务执行）。
- 数据面：Raylet 负责节点资源管理；分布式对象存储（object store）承载任务间数据（ObjectRef / future 异步引用）。

### 1.2 论文版架构图：两级调度与 GCS

> 图示与命令示例取自 Berkeley 论文 *Ray: A Distributed Framework for Emerging AI Applications* 的社区解读（阿里云开发者社区《机器学习分布式框架 Ray》）。

```
                        Global Scheduler（全局调度器）
                                     │
                                     ▼
                              Control State（GCS，全局状态控制 = meta 数据 + 容错保证）
                        ┌──────────┬─────────┬─────────┬───────────┐
                        │Object    │Task     │Event    │Function   │
                        │Table     │Table    │Logs     │Table      │
                        └──────────┴─────────┴─────────┴───────────┘
                          │          │         │         │
                          └──► Web UI / Profiling Tools / Debugging Tools / Error Diagnosis
   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
   │ Node            │   │ Node            │   │ Node            │
   │  Local Scheduler├──►│  Local Scheduler├──►│  Local Scheduler│──► 上报 Control State
   │  Worker×n       │   │  Worker×n       │   │  Worker×n       │
   │  Shared Memory  │   │  Shared Memory  │   │  Shared Memory  │
   │  Object Store ──┼───┼── Object Store ─┼───┼── Object Store  │（节点间互联=通信通道）
   └─────────────────┘   └─────────────────┘   └─────────────────┘
```

- **两级调度语义**：Local Scheduler 收到任务先判断能否本地执行，不能则提交 Global Scheduler 分发给其他 worker——对应第 6 节"分片间并行、分片内依赖"的调度模型。
- **GCS**：全局状态控制（对象/任务/事件/函数四类 meta），是容错与元数据中枢；"GCS 是 Ray 容错的保证"。
- **Object Store**：节点内存对象存储，节点间通信走它（6.4 节对象存储选型的机制背景）。

### 1.3 执行模型与核心 API

- Task：无状态远程函数（@ray.remote 声明，.remote() 异步调用）。
- Actor：有状态常驻进程，显存/权重可常驻复用（对应管线里"常驻池"的形态）。
- 数据传递：ObjectRef 引用，跨节点经对象存储，避免重复落盘。

| API | 语义 | 工程陷阱 / 红线 |
|---|---|---|
| `@ray.remote` + `.remote()` | 函数/类转分布式任务，异步提交返回 ObjectRef | — |
| `ray.get` | 阻塞取结果 | **全量等待：短板 = 最慢任务**（所有 worker 完成才 process_results） |
| `ray.wait` | 返回"已完成 + 未完成"两组 | **增量处理：先到先算**（driver 边等边 process_incremental）→ 6.5 节增量 SLAM 流式思路的框架依据 |
| `ray.put` | 显式入对象存储，返回可跨节点引用的 id，异步 | 大对象先 put 再传引用，避免反复序列化 |
| 小任务 | 调度 + 进程间通信 + 状态更新有固定开销 | **禁止过小任务**（反例：10 万个 tiny task 耗时被调度开销淹没）→ L1 分片粒度不能太小的依据之一 |

## 2. 部署形态：K8s 集成与裸机集群

### 2.1 RayCluster 与 RayJob（K8s CRD）

- RayCluster：K8s CRD（Kuberay operator 管理），一个 Head + 一组 Worker 池；Autoscaler 按负载扩缩容。
- RayJob：两种标准模式
  - submit 模式：提交到已有 RayCluster 跑一次性作业。
  - create-cluster 模式：自带临时 RayCluster，跑完自动销毁（shutdownAfterJobFinishes）。
- 作业状态机：Pending → Running → Succeeded / Failed；restartPolicy 控制失败重试。

### 2.2 裸机集群：命令与节点结构

```bash
# head（Master）：先起
ray start --head --node-ip-address 10.8.xx.3 --port=6379 --num-cpus=<N> --num-gpus=<M>
# worker：指定 head 地址加入
ray start --address 10.8.xx.3:6379 --node-ip-address <ip> --num-cpus 10
# 提交任务：任何 worker 机先连 head
ray.init("10.8.xx.3:6379")
# 关闭：每台机器各自执行
ray.stop
```

集群结构：Head node = Driver + Worker + Raylet〔Scheduler + Object Store〕+ 独立 GCS；Worker node = Worker×n + Raylet〔Scheduler + Object Store〕——与 K8s 上 KubeRay 的 Head/Worker 池语义一一对应。

## 3. 生态组件与管线对应

| 组件 | 定位 | 对应管线环节 |
|---|---|---|
| Ray Data | 流式 / 批量数据加工 | L1 视频预处理、L4 清洗封装 |
| Ray Train | 分布式训练（数据/张量并行） | 训练环节 |
| Ray Serve | 在线推理服务 | 常驻推理、评测监控 |
| RLlib / Tune | 强化学习 / 超参搜索 | RL 训练池 |

生态分层：

```
┌── Native Libraries（原生库）：tune(超参) / rllib(RL) / RAY SERVE(在线服务) / raysgd(梯度下降)
├── 3rd Party Libraries（第三方）：MODIN / DASK(DataFrame) / spaCy / dmlc XGBoost / joblib /
│                                   HYPEROPT / OPTUNA(调参) / mlflow / W&B(实验) / MARS / Classy Vision / ANALYTICS ZOO
├── 核心：Ray —— Framework for distributed Python
└── 底层部署：aws / Microsoft Azure / Google Cloud / Kubernetes
```

原生库管训练/RL/服务/调参；第三方库补齐数据科学生态；底层 K8s 即 2.1 节 RayCluster/RayJob 的承载面。

## 4. 与主流分布式训练框架对比

| 框架 | 定位 | 与 Ray 的差异 |
|---|---|---|
| torch.distributed | 单机多卡 / 多机同步训练 | 专注训练通信，不覆盖调度与数据管线 |
| DeepSpeed | 大模型训练优化（ZeRO/流水并行） | 训练优化层，可跑在 Ray 之上 |
| Horovod | 同步数据并行 | 训练层面，弹性与任务编排弱 |
| Ray | 通用分布式执行 + 训练 + 数据 + 服务 | 弹性扩缩、异构资源、管线耦合是优势 |

## 5. 结合具身数据管线：执行形态落位

- 选型判断线：有状态 · 高频 · 要复用 → RayCluster（常驻）；无状态 · 低频 · 跑完走 → RayJob（按需）。
- L1 预处理：在线采集 → 流式常驻（Ray Data）；离线存量 → 批量。
- L2 感知重建池：GPU 模型权重常驻 + 段间回注 → RayCluster GPU Actor（细化见第 6 节）。
- L3 语义 / L4 清洗封装：无状态批处理 → RayJob。
- 评测拆两类：发布前 Golden Set 回归 → 定时 RayJob；训练中持续监控 → 常驻轻服务。
- Badcase 回流：质检拦截样本 → 池（对象存储 + 队列）→ 重跑 → 复检。
- 归约写法：map → tree reduce（分桶归约、每 20 个先合再合桶减少扇入），适用于 L4 清洗聚合与 Badcase 统计上报。

## 6. L2 感知重建层的 RayCluster 部署设计

> 承接第 5 节"L2 感知重建池 → RayCluster GPU Actor"，细化到单分片级别的调度设计：Actor 清单、依赖判定、数据传递、资源与风险。设计输入：L1 输出按**视频片段分片**（每段 3~10s：RGB 帧序列 + 时间戳 + 内参元数据）；分片之间无跨段状态，天然可并行。

### 6.1 Actor 拆分原则与清单

- 拆分原则：GPU 模型推理独立成 Actor；CPU 预处理/聚合/校验放 Ray Task（不占 GPU 显存）；每个 GPU Actor 在 `__init__` 加载一次权重、常驻显存，多分片复用同一 Actor 实例（Ray Actor 池化），避免反复加载大模型。
- 机制依据：Actor 有状态常驻（`Counter` 计数累加即最简示例）正是 GPU Actor 权重常驻复用的机制基础。

| Actor / Task | 承载模型 | 硬件 | 核心输出 |
|---|---|---|---|
| `DepthActor` | MoGe + Depth Anything V2 | GPU | 深度图、法线、FoV、视差图（互检用） |
| `HandActor` | WiLoR/YOLO + MANO 拟合 + Masked DROID-SLAM 平滑 | GPU | 相机系手部 54 维时序、手部 mask |
| `SegTrackActor` | EgoHOS / Grounded-SAM + SAM2 + TAPIR | GPU | 时序 mask、手物接触边界 2D 点轨迹 |
| `CamPoseActor` | GeoCalib + MegaSaM / DROID-SLAM | GPU | `camera_to_world[T,4,4]`、稀疏点云 |
| `Obj6DoFActor` | FoundationPose | GPU | 相机系 `obj_pose_cam[T,4,4]` |
| `L2ConsistencyCheckTask` | 几何一致性校验（无模型） | CPU | 校验结果、badcase 标记、世界系坐标融合 |

### 6.2 并行 / 串行依赖判定

- 🔴 强串行依赖（必须等上游完整输出才能启动下游）：
  1. `CamPoseActor` ← `DepthActor` 深度先验（SLAM 初始化/稠密约束，深度未出不能跑轨迹求解）；
  2. `Obj6DoFActor` ← `DepthActor` 深度 + `SegTrackActor` 物体 mask（FoundationPose 需要 RGB-D + ROI）；
  3. `L2ConsistencyCheckTask` ← 全部 5 个 Actor 输出齐备（世界系几何对齐校验）。
- 🟡 弱依赖（可部分并行、下游有阻塞点）：`HandActor` 拆两段——Stage1 检测+MANO 拟合（与 DepthActor 并行启动）；Stage2 Masked DROID-SLAM 平滑需等 `SegTrackActor` 手部 mask（阻塞）。
- 🟢 完全可并行（最大并行窗口，收到分片立即同时调度）：`DepthActor`、`HandActor` Stage1、`SegTrackActor`——三者只读 L1 RGB 帧，互无输入依赖，充分利用多 GPU。
- 机制依据：Ray Task 的 DAG 依赖调度（`zeros → x/y 并行 → multiply 等先决完成 → z` 即最简示例）与上述判定同一机制——可并行者并行、先决完成才调度下游。

### 6.3 单分片调度时序

```text
T0：Ray Data 把L1输出分片送入Ray集群，调度器向GPU池提交L2一组Actor任务
    ├─ 并行启动（T0）
    │   ├─ DepthActor：MoGe+DAv2 逐帧深度推理（输出：深度图、法线、FoV）
    │   ├─ HandActor Stage1：WiLoR+MANO拟合（输出原始相机系手部54维）
    │   └─ SegTrackActor：首帧EgoHOS/GroundedSAM分割 → SAM2时序传播 → TAPIR接触点
    │
T1：DepthActor 完成全片段深度推理
    ├─ 触发启动 CamPoseActor：接收深度先验 + L1帧 + 内参元数据，求解camera_to_world轨迹
    └─ DepthActor输出同时送入 Obj6DoFActor（等待SegTrack mask就绪）
    │
T2：SegTrackActor 完成时序mask、接触边界输出
    ├─ 触发 HandActor Stage2：Masked DROID-SLAM平滑（使用手部mask做遮挡屏蔽）
    └─ 触发 Obj6DoFActor：接收物体mask + DepthActor深度图 + L1 RGB → FoundationPose 6DoF跟踪
    │
T3：CamPoseActor 完成，输出 camera_to_world 时序矩阵
    ├─ 回注到 HandActor：仅做后处理坐标变换（不回灌MANO优化），生成世界系手部轨迹
    └─ 送入 Obj6DoFActor：仅后处理坐标变换，生成世界系物体位姿
    │
T4：HandActor、CamPoseActor、SegTrackActor、DepthActor、Obj6DoFActor 全部输出就绪
    └─ 调度 CPU Ray Task：L2ConsistencyCheckTask
        · 世界系几何一致性校验、尺度漂移检测、重投影误差计算
        · badcase标记 / 片段过滤
T5：校验通过：打包L2全部结构化时序数据，输出交给L3 RayJob；校验失败：标记badcase回流，不进入L3
```

### 6.4 Actor 间数据传递方案

- 小数据（元数据、时序向量：MANO 参数、4×4 矩阵、时间戳、接触点坐标）→ **Ray ObjectRef 直接传递**（跨 Actor 引用，不拷贝，可多 Actor 同读）。
- 大数据（深度图、mask 图、RGB 帧）→ **禁止跨 Actor 直接传张量**（对象存储打爆）；落地共享存储（S3/节点本地 SSD），Actor 间只传路径 + 元数据引用（`{"path": ..., "shape": ...}`），按需拉取。
- 中间产物按分片 ID 目录隔离；分片通过 L2 校验后自动清理中间图像缓存，防磁盘膨胀。

### 6.5 资源调度与部署风险

- 权重预热：Actor `__init__` 加载模型常驻显存，分片复用（如 2 个 `DepthActor` 池轮流处理多分片）。
- GPU 隔离：每 Actor `num_gpus=1` 独占，禁止 MoGe/SAM2 混跑同卡（OOM 风险）。
- 分片粒度：3~10s/片，防单分片推理过长导致 Actor 超时、Ray 心跳丢失。
- 背压控制：Ray Data 输入流设并发上限，不一次性灌入 GPU Actor 池。
- 失败重试：OOM/超时做**分片级重试**（不重跑全量），重试上限内仍失败直接标 badcase。
- **最大串行卡点 = CamPoseActor**（等整段深度输出）：优化方向是**增量 SLAM**——深度逐帧流式送入 CamPoseActor，消除整段深度推理等待，深度推理与 SLAM 求解流水线重叠（`ray.wait` 增量处理语义即框架依据）。
- 对象存储压力：大量深度/mask 张量必须落盘只传引用（见 6.4），否则打爆 ObjectStore 内存。
- Actor 状态污染：每分片处理完必须清空 Actor 内部时序状态（SAM2 memory bank、SLAM 窗口、TAPIR 轨迹缓存），否则跨分片泄漏（SAM2 尤其典型）。
- 回注环路控制：Actor 间只传推理结果；**禁止后段输出回灌上游模型前向**（camera_to_world 只能用于 CPU 侧坐标变换），防闭环误差放大。

### 6.6 调度伪代码骨架（极简）

```python
import ray

@ray.remote(num_gpus=1)
class DepthActor:
    def __init__(self):
        self.moge, self.dav2 = load_moge(), load_dav2()
    def run_segment(self, rgb_frames, ts):
        return depth_meta_ref  # 深度/mask 等大张量只传文件引用

@ray.remote(num_gpus=1)
class HandActor:
    def __init__(self):
        self.wilor, self.mano, self.smooth = load_wilor(), load_mano(), load_masked_droid()
    def stage1(self, rgb_frames, ts):
        return hand_cam_raw_ref
    def stage2(self, hand_raw_ref, hand_mask_ref):
        return hand_cam_smooth_ref

@ray.remote(num_gpus=1)
class SegTrackActor:
    def run_segment(self, rgb_frames, ts):
        return mask_meta_ref, contact_points_ref

@ray.remote(num_gpus=1)
class CamPoseActor:
    def run_segment(self, rgb_frames, ts, depth_meta_ref, intrinsics_meta):
        return cam2world_ref

@ray.remote(num_gpus=1)
class Obj6DoFActor:
    def run_segment(self, rgb_frames, ts, depth_meta_ref, obj_mask_ref):
        return obj_pose_cam_ref

def l2_consistency_check(hand_ref, cam_ref, obj_ref, depth_ref, seg_ref):
    return l2_output_meta, is_valid  # CPU 校验 Task，非常驻 Actor

def process_one_l1_segment(rgb_frames, ts, intrinsics_meta):
    depth_f = DepthActor.remote().run_segment(rgb_frames, ts)
    hand1_f = HandActor.remote().stage1(rgb_frames, ts)
    seg_f = SegTrackActor.remote().run_segment(rgb_frames, ts)
    depth_meta = ray.get(depth_f)
    cam_f = CamPoseActor.remote().run_segment(rgb_frames, ts, depth_meta, intrinsics_meta)
    mask_meta, contact = ray.get(seg_f)
    hand_f = HandActor.remote().stage2(hand1_f, mask_meta)
    obj_f = Obj6DoFActor.remote().run_segment(rgb_frames, ts, depth_meta, mask_meta)
    hand, cam, obj = ray.get(hand_f), ray.get(cam_f), ray.get(obj_f)
    return ray.get(ray.remote(l2_consistency_check)(hand, cam, obj, depth_meta, mask_meta))
```

## 7. 工程红线

- 冷启动代价：常驻池避免模型加载/集群拉起耗时；低频任务不值得常驻。
- 显存常驻：GPU Actor 权重常驻显存，按复用率评估成本。
- 版本绑定：评测绑数据集版本号，Golden Set 用不可变快照。
- 对象存储生命周期：中间产物（深度图/mask/位姿）占用与清理策略要定。

## 8. 待确认项

- Ray 2.x 与 KubeRay 版本兼容矩阵（部署时核实）。
- Actor 池的显存调度（GPU 分片 / 并发上限）与 Autoscaler 策略。
- Ray Data 流式算子与 RayJob 批处理切换的触发条件。

## 9. 参考来源

- Ray 官方文档（总入口）：https://docs.ray.io/
  - Core（Task/Actor/ObjectRef）：https://docs.ray.io/en/latest/ray-core/walkthrough.html
  - Ray Data：https://docs.ray.io/en/latest/data/data.html
  - Ray Train：https://docs.ray.io/en/latest/train/train.html
  - Ray Serve：https://docs.ray.io/en/latest/serve/index.html
  - RLlib / Tune：https://docs.ray.io/en/latest/rllib/index.html
- Ray 源码仓库：https://github.com/ray-project/ray
- KubeRay（RayCluster / RayJob CRD，Kuberay operator）：https://github.com/ray-project/kuberay
- RayJob 使用指南（submit / create-cluster 模式）：https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/rayjob.html
- 阿里云开发者社区《机器学习分布式框架 Ray》（论文架构图示 / API / 集群命令 / 示例，2024-05-18，Ray v1.6.0 时代入门文）：https://developer.aliyun.com/article/1512422
- 调研记录日期：2026-10-01；版本兼容矩阵见第 8 节待确认项。

## 10. 速讲话术

> "Ray 是 Berkeley RISELab 的通用分布式执行框架。架构上两级调度：Local Scheduler 就近执行，必要时交给 Global Scheduler 分发；GCS 维护对象/任务/事件/函数四类全局状态，是容错保证；节点内 Object Store 内存共享、节点间互联做通信。编程模型上 Task 无状态、Actor 有状态常驻；ray.get 是全量阻塞等待、ray.wait 支持增量处理，对应我管线里增量 SLAM 的流式思路；调度有固定开销，所以任务粒度不能过小，这也是我 L1 分片定 3~10s 的原因之一。"
