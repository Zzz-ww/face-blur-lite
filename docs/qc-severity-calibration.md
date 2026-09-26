# qc 严重度分级校准记录（T_FATAL_CONF = 0.7，临时）

- **日期**：2026-09-25（当前代码确定性复现；spec：superpowers/specs/2026-09-25-gate-discrimination-design.md §2）
- **分级轴**：纯 conf 单轴——采集数据显示尺寸在各 conf 带间不分离（celebv 大脸
  blur-blob 残块全在 [0.5,0.6)、中位 229px；real-video 小脸半糊 ≥0.84），
  尺寸加权只会把两族搅在一起。
- **语义**：分级只分流（退出码 1/3、stderr 分列、复核排序）；隔离不变——
  任何违规仍隔离 qc_failed/，"qc 是信号，人是裁判"。

## 分布（444 处违规 × conf 带，2026-09-25 采集）

| 样本 | [0.5,0.6) | [0.6,0.7) | [0.7,0.8) | [0.8,1.0) | 合计 | 备注 |
|---|---|---|---|---|---|---|
| celebv_5min（大脸特写） | 9 | 0 | 0 | 0 | 9 | 全边缘——blur-blob 残块 |
| celebv_sample1 | 0 | 0 | 0 | 0 | 0 | qc 全过 |
| musicfest_5min | 25 | 8 | 6 | 0 | 39 | |
| street_5min | 102 | 109 | 118 | 32 | 361 | max conf 0.93 |
| real_video 默认（det3） | 9 | 7 | 4 | 2 | 22 | top-2 = 0.849/0.838 |
| real_video 最优（det1+px8+并集） | 7 | 2 | 3 | 1 | 13 | 同源残余 |
| **合计** | **152** | **126** | **131** | **35** | **444** | |

## T=0.7 依据（外推，非校准）

- 结构分离：celebv 残块族全在 [0.5,0.6)（已知良性模式）；street/real-video
  高置信段集中在 0.7+；仅有的 2 个视觉定性标签（真半糊漏脸）都在
  0.838/0.849 → 高段 = 真信号；
- **已知局限**：0.5–0.7 段零视觉标签——T=0.7 是外推不是校准；无标签段
  归边缘（后续标签证明该段有真漏则下调 T）。

## 重校准触发条件

- 检测器升级（SCRFD，backlog #7）——违规分布整体平移，阈值作废重采；
- 新增视觉定性标签 ≥ 20 处（重点补 0.5–0.7 段）。

## 附录：top_k 绑定性压测（2026-09-26，spec SCRFD §5）

YuNet top_k∈{50,200,5000} × {real_video, celebv_5min}，det3 采样直测检出
（不跑管线）：detections 差 0%/0% → 判定【不绑定】。
A/B 两臂 top_k 同值，公平性成立。

原始数字（6 行，conf_thr=0.3）：

```text
inputs/real-video/real_video.MP4 top_k=50: detect_calls=707 detections=1049 per_frame_max=12
inputs/real-video/real_video.MP4 top_k=200: detect_calls=707 detections=1049 per_frame_max=12
inputs/real-video/real_video.MP4 top_k=5000: detect_calls=707 detections=1049 per_frame_max=12
inputs/big-face/celebv_5min.mp4 top_k=50: detect_calls=2783 detections=3850 per_frame_max=5
inputs/big-face/celebv_5min.mp4 top_k=200: detect_calls=2783 detections=3850 per_frame_max=5
inputs/big-face/celebv_5min.mp4 top_k=5000: detect_calls=2783 detections=3850 per_frame_max=5
```

判定过程：各视频 top_k=50 与 top_k=5000 的 detections 逐一对比——
real_video 1049 vs 1049（差 0%），celebv 3850 vs 3850（差 0%），均 ≤5%
→ 不绑定，产品码不动（detector.py 默认 top_k=50 维持）。旁证：
per_frame_max 仅 12/5，远低于 top_k=50 档位，本样本集无截断空间，
与 0% 差一致。产物日志：outputs/20260926-scrfd-ab/topk/raw_run.log。

## SCRFD 闸门分布重采（2026-09-26）

重校准触发条件命中（检测器升级，SCRFD A/B spec §6）。取样 = B 臂
real_video det3 三档 det_conf 输出 × SCRFD 闸门全密度复检（A/B 同批产物）：

| det_conf | [0.5,0.6) | [0.6,0.7) | [0.7,0.8) | [0.8,1.0) | 合计 | 致命（≥0.7） |
|---|---|---|---|---|---|---|
| 0.3 | 148 | 33 | 1 | 3 | 185 | 4 |
| 0.4 | 141 | 32 | 1 | 3 | 177 | 4 |
| 0.5 | 151 | 32 | 2 | 2 | 187 | 4 |

复评结论：**T=0.7 维持**。分布向低 conf 段进一步集中——[0.5,0.6) 占
80.0%/79.7%/80.7%（YuNet 闸门时代全库 152/444≈34%）；≥0.7 段三档恒 4 处
（占 ≤2.3%），与 A 臂 det3 fatal=4、两臂 det1-px8 fatal 均 0 互证 = det3
跟踪预测缺口残余，与内核检测器/conf 无关；0.5–0.7 段仍零视觉标签，下调
须标签证据（复核留人）。致命段占比未见剧变 → 无待用户裁决项。旁证：绿灯
回归 street_5min 主闸门 20 处致命残余（SCRFD 内核欠糊所致，见 014 A/B
回填）按定义全落 ≥0.7 段——"高段=真信号"方向再获印证。
