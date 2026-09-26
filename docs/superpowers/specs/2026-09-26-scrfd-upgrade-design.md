# SCRFD 升级（det_500m 移植 + top_k 压测）设计 v1

- **日期**：2026-09-26
- **状态**：设计已口头过审（qc 同升+独立可配 / YuNet 保持默认 / make_detector 工厂），待用户评审 spec
- **背景**：BUG-014 结论"16-32px 残余=YuNet 能力包络，根治须换更强检测器"；Phase 0
  spike 已 GO（附录在 docs/bugs/014）：det_500m（buffalo_s）15.52ms@640×640
  = 1.22× YuNet 同尺寸、2.5MB、sha256 三仓一致；"cv2.dnn 无 DeformConv"对
  buffalo 系导出被证伪。本波把它变成 `--detector scrfd`。
- **两处勘误（相对 benchmark/旧记录）**：① YuNet `top_k` 默认已是 50
  （detector.py:50），压测问题改为"50 是否在高密度绑死"；② YuNet 输入按帧
  全分辨率 `setInputSize((w,h))`（detector.py:62），构造参数 (320,320) 只是
  初始提示——"固定 320×320"旧刀过时（benchmark.md 有用户未暂存手笔，本波
  不动该文件）。

## 1. SCRFDDetector（detector.py 新类，实现 Detector 协议）

- **预处理 letterbox**：长边缩到 640、pad 到 640×640（720×1406 → 内容带
  328×640，即 spike "可望 ~11ms" 的形状）；框坐标按 scale+pad 映射回原图。
- **推理**：`cv2.dnn.readNetFromONNX`；动态输入为实现期验证项——导出若
  不支持任意尺寸，回退固定 640×640 letterbox（本就是默认路径）。
- **解码（纯 numpy，spike 已踩明两坑）**：9 张量 = score×3 / dist-bbox×3 /
  kps×3 × strides(8,16,32)，2 锚/cell；anchor 逐位置 `np.repeat` 交错；
  bbox dist ×stride 中心式 → xyxy；kps 丢弃（Det 不带关键点）。
- **阈值与截断**：`conf_thr` 与 YuNet 同语义；NMS 用 `cv2.dnn.NMSBoxes`；
  `top_k=50` 后置截断（NMS 后保留上限，与 YuNet 默认对齐保 A/B 公平）。
- **不做 upscale**：SCRFD 的小脸能力就是本次升级的目的，放大旋钮留 YuNet
  专属（docstring 注明；未来需要再加）。

## 2. make_detector 工厂与模型解析

- `make_detector(kind: str, model_path: str | None = None, *,
  conf_thr: float, top_k: int = 50, upscale: float = 1.0) -> Detector`：
  kind∈{"yunet","scrfd"}；scrfd 忽略 upscale（docstring 声明）；未知 kind
  抛 ValueError。选工厂而非纯 Protocol 注入：CLI/blur.sh/A-B 矩阵都要可达，
  命令行即自描述记录。
- `resolve_model_path(kind)`：yunet → models/face_detection_yunet_2023mar.onnx
  （`FACEBLUR_MODEL` 覆盖语义不变）；scrfd → models/det_500m.onnx
  （`FACEBLUR_SCRFD_MODEL` 覆盖）。缺文件报错文案指向 download_assets.sh。

## 3. 注入点与 CLI

- kernel.py:45-46 → `make_detector(p.detector, model_path, conf_thr=p.det_conf,
  upscale=p.det_upscale)`；`KernelParams.detector: str = "yunet"`。
- qc.py:57 → `qc_video(..., detector: str = "yunet")`，内部
  `make_detector(detector, model_path, conf_thr=pass_conf)`。
- CLI：`single`/`pipeline` 加 `--detector {yunet,scrfd}`（默认 yunet）；
  `qc` 子命令加 `--detector`（默认 yunet——独立子命令不随别人）；`pipeline`
  另加 `--qc-detector`（**缺省跟随 `--detector`**——qc 独立可配的交叉验收位，
  本轮只铺不评）。blur.sh 同款
  透传：Python 编排器内嵌 qc 与 shell 收尾 qc_one 必须同语义。

## 4. download_assets.sh

- 加 det_500m：主 URL
  `https://hf-mirror.com/immich-app/buffalo_s/resolve/main/detection/model.onnx`
  （huggingface.co 原址注释备选），`FACEBLUR_SCRFD_URL` env 覆盖，
  `curl -C -` 断点续传（hf-mirror ~15KB/s，续传是关键）。
- **全部模型补 sha256 -c 校验**：det_500m 用三仓一致值
  `5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a`；
  YuNet/lena 现值从本地已验证文件起算补上。

## 5. Phase 1：top_k 绑定性压测（Task 0，纯测量不改产品码）

- 矩阵：top_k ∈ {50, 200, 5000} × {real_video（默认 det3）, celebv_5min}；
  指标 detections / qc violations / wall_s；qc 闸门全程固定 YuNet 现默认
  （升级前冻结世界，变量只有 top_k）。
- 判定：50 档与 5000 档检出差 >5% 即"绑定"→ YuNet top_k 提档进本波
  （改默认值一行 + A/B 记录）；不绑定则结论归档（校准文档附录）。

## 6. A/B 验收（Phase 2 收尾，产物进 outputs/20260926-scrfd-ab/）

- **同闸门原则**：违规数可比的前提是两臂 qc 检测器一致——492/308 是
  YuNet 闸门的历史数，不能直接当 SCRFD 闸门下的对照（用户复核时戳出，
  2026-09-26 修）。
- **A 臂（新基线）**：YuNet 内核 + **SCRFD 闸门**重跑 real_video（默认
  det3 与最优 det1+px8+并集两配置）——A 臂对 492/308 的差 = 闸门灵敏度
  变化本身（独立记录）。
- **B 臂**：SCRFD 内核 + SCRFD 闸门，det_conf 矩阵 {0.3, 0.4, 0.5}
  （SCRFD 置信分布更尖锐，0.3 未必仍是对的角色）。
- **胜出判据**：B 对 A 违规下降 ≥30% 且 wall_s 劣化 ≤10%（同闸门同尺）。
  492/308 降为历史参照列出。
- **5 样本绿灯回归**：B 臂成品主判 SCRFD 闸门（celebv_sample1/musicfest/
  street/celebv_5min/real_video 中已绿者不得新转红）；YuNet 闸门顺带跑作
  交叉参考列（转红不否决、须解释——交叉验收副产品）。
- **重校准条款命中**：红灯样本 qc conf 分布重采 → T_FATAL_CONF 复评，
  结论进 docs/qc-severity-calibration.md（qc.py:15-18 注释的触发条件）。
- 胜出后翻默认=下一波一个 commit（本波 SCRFD 保持 opt-in）。

## 7. 测试

- **decode 纯 numpy fixture**：构造 9 张量（已知锚×已知 dist）→ 期望 xyxy，
  无模型依赖；NMS 边界（重叠/包含/并列）；anchor 交错与 ×stride 两坑各有
  专测。
- make_detector：分派正确类、未知 kind 抛错、scrfd 忽略 upscale。
- 注入点接线：KernelParams.detector / qc_video(detector=) 透传到工厂
  （mock 断言实例类型）。
- CLI flag 解析：--detector/--qc-detector 默认与跟随语义。
- 真实模型（requires_assets 惯例）：lena 出框坐标正确；real_video 冒烟
  wall_s 与 014 附录同量级。
- **YuNet 默认零变化**：现有全量绿（122）即回归锁。

## 8. 文档

README 模型表与 download_assets 用法；pipeline-guide §4 检测器节扩 SCRFD
（letterbox 链路 + 两检测器对照表）；design.md §2 模型选择升级位回收注记；
docs/bugs/014 附录回填 A/B 数字；qc-severity-calibration.md 重校准节。
**industry-benchmark.md 本波不动**（用户未暂存手笔在工作区）。

## 9. 非目标

- 默认检测器翻牌（A/B 胜出后下一波单独 commit）；
- SCRFD upscale / kps 输出 / 云 API；
- 交叉验收评估（--qc-detector yunet 位铺好，本轮不跑结论）；
- tracker/关联策略改动（BUG-014 开放项"高密度关联重做"另立）。

## 约束（继承）

依赖白名单不变（零新依赖，numpy/cv2 均已在）；模型资产不入库
（models/*.onnx gitignore）；outputs/inputs 永不入库；实验产物进
`outputs/20260926-scrfd-ab/` 会话目录；bash 3.2；commit 尾行
Co-Authored-By: Claude Code <noreply@anthropic.com>；push 仅用户明确要求。
