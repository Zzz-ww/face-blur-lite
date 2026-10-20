# 模型卡 04：分割与跟踪 · SAM2 + TAPIR

> 02-生产域 模型卡系列第 4 张。定位：管线「实例分割 / 点轨迹跟踪」算子组合（首帧冷启动 → SAM2 时序传播 → TAPIR 点轨迹），产出**手/物 mask 时序轨 + 接触边界**。承接 [算子设计-01 §4 分割/点跟踪行](../算子设计/算子设计-01-业内通用算子全集总览.md)；素材底稿 = [调研笔记-01 场景 D](../../92-调研笔记/调研笔记-01-具身数据处理模型-场景化选型与管线结合.md)。

## 核实状态（2026-10-17）

- ✅ 段位：SAM2 = Meta 开源，分割 + 时序跟踪**事实标准**；Grounded-SAM = 工程界常用开域分割；TAPIR = Google DeepMind 点轨迹（动态重建/视频理解领域认可度高）
- ✅ 领域边界：EgoHOS 仅第一视角手物分割赛道有效（离开 ego 场景不用）
- ✅ 管线口径：与 [算子设计-03](../算子设计/算子设计-03-算子执行与编排规范.md) PipelineSpec 的 `op-per-detect → op-per-seg → op-per-track` 三节点链一致
- ❓ 待补：首帧两路冷启动的实测精度对比、mask 时序 IoU 衰减实测曲线（待跑 benchmark）

## 1. 定位与算子位置

- **任务**：手/物 **mask 时序轨** + **手-物接触边界**标注——mask 表达"谁在哪"，接触边界表达"手怎么碰物"
- **算子位置**：生产域 · L2 感知结构化 → 目标检测 → 实例分割 → 点跟踪三算子链（`op-per-detect`/`op-per-seg`/`op-per-track`）
- **坐标系**：mask 是 2D 图像域；TAPIR 点是 **2D 像素坐标**——3D 接触点必须结合④深度 + ③外参恢复
- **定位**：接触边界是操作数据最有价值的信号之一，也是 L3 动作分割/VLM 指令的物理依据

## 2. 模型原理（一句话 + 三段链）

- **核心思想**：首帧提示冷启动（降人工）→ 时序传播（保一致性）→ 点轨迹（补接触细节）
  1. **首帧冷启动二选一**：路径 A（ego 视角）**EgoHOS**：首帧 RGB → 手/交互物体 mask；路径 B（开域未知物体）**Grounded-SAM**：首帧 RGB + 文本 prompt（Grounding DINO 出框 → SAM 出 mask）
  2. **SAM2 时序传播**：首帧 mask + 全帧序列 → 跨帧时序 mask 轨（流式记忆机制，跨帧一致性）
  3. **TAPIR 稠密点轨迹**：时序 mask + RGB 视频 → 手-物接触边界 2D 点轨迹
- **组合逻辑**：mask 轨给"实例级"结构化，点轨迹给"亚实例级"接触细节，两者时间对齐后并出

## 3. 输入 / 输出规格

- **输入**：
  - L1 规范化 RGB 帧序列 + 时间戳
  - 辅助②：手部 mask（提升手部分割精度）
  - 路径 B 需文本 prompt（物体类别描述）
- **输出**：
  - 手/物体实例 mask 时序轨（二值/实例图 [T,H,W]）
  - 接触边界 2D 点轨迹 [T,N,2]
- **字段命名建议**：`hand_mask_seq[T,H,W]`、`object_mask_seq[T,H,W]`、`contact_points_2d[T,N,2]`

## 4. 管线位置与上下游

- **上游 / 回注**：手部 mask 来自②（[模型卡 01](01-手部重建-HaWoR.md)）辅助输入
- **下游分发（4 路）**：
  1. 手部 mask → ② Masked DROID-SLAM 遮挡屏蔽
  2. 手/物体 mask → ③ SLAM 动态区域屏蔽（[模型卡 02](02-相机位姿-MegaSaM.md)）
  3. 物体 mask + 接触点 → ⑥ FoundationPose ROI 裁剪（[模型卡 05](05-物体6DoF-FoundationPose.md)）
  4. mask + 接触边界 → L3（动作分割、VLM 指令生成的物理依据）
- **组合**：它既是②③的"屏蔽信号源"，又是⑥的"ROI 供给方"、L3 的"物理依据源"——L2 里扇出最广的模块之一

## 5. 指标与资源

- **质检指标**：**mask 时序 IoU 衰减率**（传播质量）、**接触点有效性**（点是否落在真实接触区）
- **资源**：GPU 推理；首帧冷启动（EgoHOS/Grounded-SAM）+ SAM2 传播 + TAPIR 三段可分算子独立扩缩；实测耗时 / 显存：待跑 benchmark（按算子设计-06 成本口径回填）
- **关键质检门**：**首帧质检门**——首帧 mask 质量不过关则整段重跑冷启动，不允许带病传播

## 6. 选型与替代

- **适用**：ego 手物操作视频，首帧可低成本提示（ego 路径或文本 prompt 路径）
- **替代 / 互检**：
  - 首帧：ego 视角 EgoHOS / 开域 Grounded-SAM 二选一；**Grounding DINO**（Grounded-SAM 的检测基座）可单独出框
  - 分割：**Mask2Former**（闭集语义分割替代，SAM2 同时承担时序传播是其独有价值）
  - 点跟踪：**CoTracker**（TAPIR 同档替代，需实测对比）
- **降级**：无时序需求（逐帧独立质检）→ 纯 SAM 逐帧分割放弃传播；无接触分析需求 → 砍 TAPIR 段

## 7. 失效模式与红线

- **失效**：
  - **首帧 mask 失效 → SAM2 全时序崩坏**（传播错误一错到底）——所以首帧质检门是硬门
  - 长时序传播漂移（IoU 逐步衰减）、快速运动/大遮挡下实例丢失
  - 文本 prompt 域外物体（Grounded-SAM 路径检不出 → 冷启动失败）
- **红线**：
  - **TAPIR 输出是 2D 点**，3D 接触点 = 2D 点 + ④深度 + ③外参——跳过深度/外参直接说"3D 接触"是坐标系错误
  - mask 用于屏蔽②③时必须做形态学校验（空洞/碎块 mask 会把有效区域误屏蔽）
  - 接触边界进 L3 前须时间戳对齐（mask 轨与点轨迹帧号错位 = 物理依据错位）

## 8. 参考来源

- SAM2：https://github.com/facebookresearch/sam2
- EgoHOS：https://github.com/owenzlz/EgoHOS
- Grounded-SAM：https://github.com/IDEA-Research/Grounded-Segment-Anything
- TAPIR：https://github.com/google-deepmind/tapnet

## 速讲话术（30 秒）

> "分割跟踪链解决手物 mask 时序轨和接触边界。链路三段：首帧冷启动二选一——ego 视角用 EgoHOS，开域未知物体用 Grounded-SAM 文本提示；然后 SAM2 做时序传播，Meta 的分割跟踪事实标准；最后 TAPIR 补稠密点轨迹，标出接触边界。它扇出最广：手部 mask 给手部重建做遮挡屏蔽、给 SLAM 做动态屏蔽、给 FoundationPose 做 ROI、给 L3 当动作分割和 VLM 指令的物理依据。两个关键工程点：一是首帧 mask 失败会让 SAM2 一错到底，所以必须设首帧质检门；二是 TAPIR 出的是 2D 点，要结合深度和外参才能得到 3D 接触点，直接说 3D 就是坐标系错误。"
