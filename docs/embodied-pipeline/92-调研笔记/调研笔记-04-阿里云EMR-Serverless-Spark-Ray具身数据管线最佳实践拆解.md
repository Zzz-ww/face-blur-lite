# 调研笔记 04：阿里云 EMR Serverless Spark Ray 具身数据管线最佳实践拆解

> 调研底稿：阿里云官方实践教程《基于 EMR Serverless Spark Ray 打造具身智能数据管线》（https://help.aliyun.com/zh/emr/emr-serverless-spark/use-cases/build-embodied-ai-data-pipeline-with-emr-serverless-spark-ray，访问于 2026-10-11），技术思路源自微软研究院 VITRA 论文。本篇拆解其五段流水线、显存/冷启动/依赖三组工程优化与 pyjindo 实战坑，并逐条映射到《本地出口提纲》可引用的弹药点。

## 0. 调研背景与问题

- 这是什么：阿里云 EMR Serverless Spark 的官方最佳实践，在托管 Ray 集群上把**第一视角人类视频**转成 LeRobotDataset v3.0 格式的 VLA 训练数据。
- 为什么值得看：与我们 L2 感知管线技术栈同构（HaWoR / MoGe-2 / MegaSAM / Ray），且是**云厂商官方背书**的架构——聊 Ray 选型时是最硬的行业佐证。
- 要回答的问题：
  1. 官方实践的管线怎么分阶段？和我们的 L1/L2/L3 分层什么关系？
  2. 单张 16GB 卡怎么装下合计 ~20GB 显存的三个模型？
  3. 冷启动（模型下载 + CUDA 扩展编译）怎么优化？
  4. 哪些点可以直接引用进口述回答？

## 1. 管线全景：五段流水线

**问题背景**：VLA 训练数据是规模化瓶颈——真机采集成本高，互联网视频缺精确 3D 状态和动作编码；第一视角视频是折中：可穿戴设备规模化采集、视角接近机器人、天然保留双手运动与物体交互，只需恢复相机位姿 / 场景深度 / 双手 3D 姿态 / 动作语义即可构造观测—动作轨迹。

| 步骤 | 算子 | 说明 |
|---|---|---|
| 读取 OSS 视频 | `scan_input` | OSS 前缀 / 本地目录 / .txt 列表三种输入 |
| 裁切并规范化视频 | CPU UDF | 20s clips（相邻重叠 2s）、30fps、去畸变、JPEG 帧 |
| 恢复深度与手部姿态 | Ray GPU remote task | HaWoR 双手 MANO + MoGe-2 深度 + MegaSAM 相机位姿 |
| 对齐坐标并生成动作片段 | CPU UDF | 腕部速度极小值切分为原子动作片段 |
| 写出 LeRobotDataset v3.0 | CPU writer | Parquet + H.264 MP4 + JSON 元数据 |

```
oss://<bucket>/vitra/videos/*.mp4
   │ scan_input：OSS 前缀扫描（pyjindo + 节点 RAM 角色，免 AK/SK）
   ▼
CPU 头节点（driver 进程）
   ├─ Stage 0 预处理：20s clips（重叠 2s）· 30fps · 去畸变 · JPEG 帧落盘复用
   ├─ Stage 2 动作分割：双腕速度极小值（find_peaks）→ 原子动作片段
   └─ Stage 3 写出：LeRobotDataset v3.0（Parquet + H.264 MP4 + meta）
        │ clips / frames_dir                          ▲ MotionLabel3D
        │ @ray.remote(num_gpus=1)                     │ ray.get
        ▼                                             │
GPU worker（Ray GPU remote task）
   └─ Stage 1 三维重建：三模型串行加载、用完即卸（del + empty_cache）
        HaWoR ~10G（双手 MANO）→ MoGe-2 ~6G（深度）→ MegaSAM ~4G（相机位姿）
        合计 ~20G 显存 ⇒ 串行复用挤进单张 16GB 卡
```

- **职责分域**：CPU 头节点跑 driver + 全部 I/O 密集阶段（发现、裁切、分割、写出）；GPU worker 只跑推理。Ray 只做编排，`ray.remote(num_gpus=1)` 把 GPU 推理调度到 worker。
- **细节术语**：正文写"Ray GPU Actor"，代码实际是 `@ray.remote(num_gpus=1)` **remote function**，内部实例化的 `MotionLabelingActor` 是普通 Python 类（非 Ray Actor）。形态取舍：每视频起一次推理、跑完整进程释放（显存干净）vs 常驻 Actor（权重常驻省加载、显存不还）。
- 预处理 IO 技巧：JPEG 帧直接写盘供 GPU 阶段复用，**避免重复解码视频**；长视频先提取全部帧到共享目录，各 clip 用符号链接切分边界。

## 2. Stage 1 三维重建：三模型串行加载

外层 remote function，worker 进程内按三阶段**串行执行，每个加载下一个前卸载（del + torch.cuda.empty_cache()）**：

| 模型 | 显存 | 内部链路 | 输出 |
|---|---|---|---|
| HaWoR | ~10 GB | YOLO 手部检测（WiLoR detector.pt）→ MANO → Masked DROID-SLAM → 深度先验 | 左右手各 T 帧腕旋转（轴角）/平移/关节姿态/手型 |
| MoGe-2 | ~6 GB | 逐帧推理 | 深度图、点云、法线、FoV |
| MegaSAM | ~4 GB | Depth-Anything 单目视差 → MoGe-2 深度转 UniDepth 格式 → DROID-SLAM 相机追踪 | camera_to_world [T,4,4] |

- 相机内参由 GeoCalib（pinhole.tar）提供焦距估计。
- 结果合成 `MotionLabel3D`，双手数组补齐到 T = max(left, right, depth_count, 1)，缺失帧填零并标 `valid=False`：

| 字段 | 形状 | 含义 |
|---|---|---|
| left/right_wrist_pos | [T,3] | 手腕位置 |
| left/right_wrist_rot_aa | [T,3] | 手腕旋转（轴角） |
| left/right_hand_pose | [T,45] | MANO 15 关节姿态 |
| left/right_valid | [T] | 每帧有效性 |
| camera_poses | [T,4,4] | 相机到世界坐标变换 |
| camera_fov | float | 水平视场角（度） |

## 3. 动作分割与数据集写出

**动作分割**（对应我们提纲里 episode 切分问题）：

- 信号：双手腕 3D 速度取 `max(left_speed, right_speed)` 为联合信号；
- 平滑：300ms 移动平均；
- 切分：`scipy.signal.find_peaks` 对**负向速度**找极小值（动作间停顿），约束最小间距 0.5s + 峰值显著度 5%·max；
- 每段标注主手（段内平均速度更高者）与持续时间；无切分点时整段兜底为单一动作。

**state/action 定义**（108 维）：

```
state = [left_hand(54), right_hand(54)]
每手 54 = wrist_pos(3) + wrist_rot_6d(6) + mano_joints(45)
action = 下一帧的 state（one-frame-ahead）
```

**LeRobotDataset v3.0 目录结构**（可作 L3 输出格式参考）：

```
dataset/
├── meta/
│   ├── info.json / stats.json / tasks.parquet
│   └── episodes/chunk-000/file-000.parquet
├── data/chunk-000/file-000.parquet          # 帧级 state/action/task_index
└── videos/observation.images.ego/chunk-000/file-000.mp4   # H.264, 1280x720@30fps
```

## 4. 工程优化三板斧（核心弹药）

### 4.1 显存复用：串行加载、用完即卸

- 三个模型合计 ~20GB，单张 16GB 卡跑下来的唯一办法：同进程内串行加载，加载下一个前 `del` + `torch.cuda.empty_cache()`。
- 省掉多卡部署与模型并行的复杂度；代价是三阶段无法流水线并行（吞吐换显存）。
- 对照我们提纲一 Q2"HaWoR 计算开销更高"：这里给出了量化数据（HaWoR ~10G / MoGe-2 ~6G / MegaSAM ~4G）。

### 4.2 两级缓存加速冷启动

- **节点级持久缓存** `/tmp/vitra_cache/`：模型权重（~4.5GB）symlink + CUDA 扩展编译产物，**跨 Ray session 复用**；Actor 启动时把 repo weights 目录链过去，同节点旧 session 残留自动清理，磁盘不膨胀。
- **OSS 二级缓存**：`compiled/` 前缀存编译产物 tar，跨集群冷启动复用。
- 命中链：节点缓存命中 → 秒级解压；未命中 → 从 OSS 下载并回写节点缓存；OSS 也无 → worker 现场编译并回传 OSS。
- 编译必须在 GPU worker 上做：CUDA 扩展要链接 worker 上确切的 PyTorch / Python / CUDA 版本（droid_backends / lietorch_backends / pytorch3d._C）。

### 4.3 依赖管理：绕开 runtime_env virtualenv

- 不用 Ray runtime_env 的 pip 方式（会在 worker 上创建 virtualenv：overlay 磁盘容易爆、屏蔽系统已装的 conda 包），改为 `setup_pip.py` 直接装**系统 Python**，集群冷启动后跑一次。
- 大件（torch/torchvision）由集群 conda 镜像提供，pip 只装增量（ultralytics / scipy / pyarrow / opencv / imageio-ffmpeg）。

## 5. pyjindo / 兼容性实战坑清单

| 坑 | 变通 |
|---|---|
| pyjindo 不支持 `np.savez` 的 seek() 写入 | monkey-patch 重定向到 /tmp 再上传 |
| `.so` 文件在 OSS 上无法 mmap / dlopen | 拷贝到 /tmp 本地磁盘 + 扩展 LD_LIBRARY_PATH |
| MoGe-2 依赖 xformers 的 MemEffAttention | patch 成 PyTorch 原生 SDPA |
| OSS 凭证管理 | 节点 RAM 角色免 AK/SK，提交机不落明文密钥 |

- 全部是"真跑过"的细节——讲解时引用可信度远高于纯理论推导。

## 6. 弹药映射表

对《本地出口提纲》逐题标注可引用点：

| 提纲题目 | 引用点 | 一句话话术 |
|---|---|---|
| 一 Q1 管线分层 | 五段流水线 + JPEG 中间帧落盘复用 | "阿里云 EMR 官方实践同样是发现→预处理→GPU 重建→分割→写出的分段链路，中间帧落盘复用就是按需缓存的实例" |
| 一 Q2 模型选型 | 显存预算 10G/6G/4G | "选型看算力开销可以量化：官方实测 HaWoR ~10G、MoGe-2 ~6G、MegaSAM ~4G，合计 20G" |
| 一 Q3 瓶颈定位优化 | 冷启动两级缓存、避免重复解码 | "冷启动优化：权重和 CUDA 编译产物做节点级持久缓存跨 session 复用，OSS 二级兜底，命中秒级" |
| 一 Q4 时间戳对齐/episode 切分 | 腕速极小值切原子动作 | "episode 切分有两条路线：语义事件流标记，或腕部速度极小值的运动学信号切分——后者是阿里云官方实践的做法" |
| 一 Q5 Ray vs K8s | CPU driver + GPU worker + num_gpus=1 | "阿里云 EMR Serverless Spark 的官方最佳实践就用 Ray 编排具身管线，GPU 推理走 ray.remote(num_gpus=1)——行业共识而非个人偏好" |
| 一 Q6 故障与断点续跑 | autoscaler 销毁 worker 后的恢复 | "集群冷启动后依赖装一次、缓存命中秒级恢复，worker 重建不影响数据面" |
| 一 Q7/Q8 质检与 badcase | 可视化三件套 | "重建结果叠回原画面做快速验证：深度伪彩叠加、腕部轨迹投影、相机轨迹俯视图——低成本自洽校验的辅助手段" |
| 二 Q1 百舸业务理解 | 竞品动态 | "阿里云已把具身数据管线列为 EMR 官方教程，说明云厂商都在卷具身数据工程，百舸+数据超市是行业卡位而非孤立投入" |
| 二 Q3 异构算力架构 | pyjindo RAM 角色免密、托管 Ray | "阿里云的样板：平台管资源配额与集群生命周期，数据面统一走对象存储 + 节点角色免密" |
| 三 Q3 Ray on K8s 稳定性 | runtime_env virtualenv 坑 | "runtime_env pip 会在 worker 建 virtualenv，overlay 磁盘易爆——官方实践都绕开它装系统 Python" |
| 四 Q1 从零搭建平台 | LeRobot v3.0 目录结构 | "数据集管理层可以直接对齐 LeRobotDataset v3.0：Parquet 帧级数据 + MP4 视频 + meta 三件套，社区生态现成" |
| 四 Q3 GeoPT 对管线要求 | 108 维 state 定义 | "物理预训练要求保留完整 3D 状态：官方 state 就是 wrist_pos + rot_6d + MANO joints + 相机位姿，不只是图片掩码" |

**可整段背的两句**：

1. Ray 选型佐证："这套选型不是我拍脑袋——阿里云 EMR Serverless Spark 今年官方最佳实践就是同样架构：CPU 头节点跑 driver 和预处理，GPU worker 用 ray.remote(num_gpus=1) 跑 HaWoR、MoGe-2、MegaSAM 串行推理，最后写 LeRobotDataset v3.0。"
2. 显存工程："显存受限时三个 ~20G 的模型可以在单进程内串行加载、用完即卸，挤进一张 16GB 卡；代价是三阶段不能流水线并行——这就是显存和吞吐的取舍。"

## 7. 与自研 L2 管线对照（差异点 = 辨析素材）

| 维度 | 阿里 EMR 官方实践 | 我们的 L2 管线 |
|---|---|---|
| 编排形态 | 单 driver 顺序执行 + 1 个 GPU remote task，单视频粒度 | 分阶段 DAG，多任务并发调度 |
| 模型共存 | 进程内串行加载卸载（显存优先） | 分阶段常驻（吞吐优先）——可主动讲这个取舍 |
| 质检 | 可视化人工验证为主 | 三层评测体系：自洽全量 + GT 抽样 + 下游收益 |
| episode 切分 | 腕速极小值（运动学信号） | 事件流语义标记（高层事件绑定） |
| 数据出口 | LeRobotDataset v3.0 | 自定义 L3 样本格式（可对齐 v3.0 标准） |
| 规模定位 | 教程级单集群演示 | 平台级：多源接入、质检门、badcase 回流闭环 |

- 辨析话术：官方实践解决"一条视频→一个数据集"的链路打通；平台化要解决的是多源接入、质量门禁、回流闭环——正好是我们 L1/L2/L3 分层 + 三层评测 + badcase 回流补的那一层。

## 8. 附：模型权重清单（~4.5GB，自行下载传 OSS）

| 文件 | 大小 | 来源 |
|---|---|---|
| geocalib/pinhole.tar | 110 MB | cvg/GeoCalib v1.0 release |
| hawor/hawor.ckpt | 3.1 GB | HuggingFace ThunderVVV/HaWoR |
| hawor/detector.pt | 51 MB | HuggingFace rolpotamias/WiLoR |
| hawor/hawor_DATA.zip | 6 MB | MANO 官网（注册后按 HaWoR 文档组装） |
| moge2/model.pt | 1.3 GB | HuggingFace Ruicheng/moge-2-vitl-normal |
| megasam/megasam_final.pth | 19 MB | GitHub mega-sam/mega-sam checkpoints |

## 来源

- 阿里云帮助中心：《基于 EMR Serverless Spark Ray 打造具身智能数据管线》（实践教程 → AI 能力集成 → 具身智能），URL 见文首，访问于 2026-10-11。
- 关联：VITRA（微软研究院，第一视角视频处理思路）；本仓库《调研笔记-02》（Ray 架构与 remote function/Actor 语义）、《本地出口提纲》（弹药映射目标）。
