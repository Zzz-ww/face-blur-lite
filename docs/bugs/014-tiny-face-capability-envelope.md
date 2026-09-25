# BUG-014：残余违规=YuNet 小脸能力包络（检测放大缓解，非根治）

- **日期**：2026-09-24
- **发现场景**：BUG-011/012/013 修复 + 参数矩阵后，真实视频全密度复检
  仍有 14.5–23% 违规帧。det_every=1（消灭全部预测帧）只降 18 帧，
  margin 8→14px 只降 84 帧——收益递减指向检测器自身能力，非管线缺陷。

## 定性（探针实测）

- 合成小脸梯度（lena 缩放贴图，conf_thr=0.3）：
  - 48px：1× 检不出，**2× 检出（conf 0.54）**——检测放大有效档
  - 16–32px：**1×、2× 均检不出**——超出 YuNet 能力包络
- 域移残余：源域 <0.3 而输出域重编码后 ≥0.5（重编码 ±0.1 置信度漂移）

## 修复（能力位，默认关）

`YuNetDetector(upscale=…)` + `KernelParams.det_upscale=1.0`：内部
`cv2.resize` 上采样检测、框坐标映射回原图。默认 1.0——推理耗时约随
倍率平方增长（实测本机 720×1406：1×=52ms，1.5×=131ms，2×=226ms/帧），
按需开启。

## 真实视频实测

| 配置 | 违规帧/2121 | 耗时 |
|---|---|---|
| det3 + px8 + 并集（默认） | 492 | 54s |
| det1 + px8 + 并集 | **308** | 133s |
| det3 + px8 + 并集 + 放大2×（v9） | 549 | 175s |

v9 反直觉但合理：检出确实变多（1049→1550），但小脸高密度下
BUG-011 的距离兜底匹配互相串位（track 88→188，框被邻脸拖走），
覆盖反而下降。**结论：放大 2× 只救 40–50px 档；本视频残余主体是
16–32px（探针实测 2× 仍盲），根治须换更强检测器**（SCRFD 升级位，
设计文档 §模型选择已预留；或高密度检测下的关联策略重做——开放项）。

## 回归锁

`tests/test_detector.py::test_upscale_maps_box_to_original_coords`
（坐标映射回原图）、`test_upscale_rescues_small_face`（48px 档 1× 盲/2× 检出）

## 附录：SCRFD Phase 0 spike（2026-09-26，go/no-go 闸门）——**GO**

**"OpenCV DNN 无 DeformConv 算子挡住 SCRFD"对 insightface buffalo 系导出被证伪**：
buffalo_s/m/l 三份检测模型（det_500m/det_2.5g/det_10g）在 cv2 5.0.0
`readNetFromONNX`+forward 全链通过、lena 均解码出框——det_10g 经 onnx 图
普查确认该导出无任何 DeformConv 节点（Conv58/Add16/Transpose9/…，opset 11）。

| 候选 | 来源（hf-mirror） | 体积 | ms/帧@640×640¹ | lena 出框 |
|---|---|---|---|---|
| YuNet（基线） | 本地 | 232KB | **12.73** | conf 0.909 |
| **det_500m**（buffalo_s） | immich-app/buffalo_s | 2.5MB | **15.52** | 0.811，定位正确 |
| det_2.5g（buffalo_m） | immich-app/buffalo_m | 3.3MB | 29.79 | 0.779 |
| det_10g（buffalo_l） | KlingTeam/LivePortrait | 16.9MB | 85.87 | 0.796，但定位粗（仅 stride-32 头过 0.5，IoU≈0.6）——**不选** |

¹ warmup 3 + 20 次均值，同机同协议。YuNet 720×1406 全幅 = 36.93ms
（旧记 52ms 为机器状态差异，现测 0.71×）。det_500m 的 720×406 内容带
面积 < 640×640，实际可望 ~11ms。

**裁定**：GO——det_500m 主候选（同尺寸成本仅 1.22× YuNet，2.5MB，
sha256 `5e4447f5…ea3a` 三仓一致）。解码层为唯一实现工作量：9 张量 =
[score×3, dist-bbox×3, kps×3] × strides(8,16,32)，2 锚/cell 共 16800 锚；
两个坑：bbox 预测须 ×stride；anchor 须逐位置交错复制（np.repeat）。
det_10g 的 cv2 输出经临时 onnxruntime 1.23.2 交叉验证逐位一致
（ORT 68.81ms）——cv2.dnn 计算正确性有独立背书。

**网络**：huggingface.co 大陆直连超时；hf-mirror.com 可用但慢（~15KB/s），
`curl -C -` 断点续传是关键。模型已入 models/（gitignore，不入库）。
