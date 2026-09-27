# 参数与配置参考

全部默认值以代码为准。"暴露面"指该参数可从哪里改：**CLI** = 命令行可传，**代码级** = 只能经 `KernelParams`/函数签名构造（库用法）。

## 信号域总则（先读这个再调阈值）

| 域 | 参数 | 作用对象 |
|---|---|---|
| **源域**（原始解码帧） | `det_conf=0.3` | 内核检测阈值 |
| **输出域**（脱敏重编码后） | `pass_conf=0.5`、`T_FATAL_CONF=0.7` | qc 复检阈值 |

两套阈值**不可比较、不可复用**：有损重编码会改变检测置信度，各自只对自己的信号域有意义。

## 内核参数（源域，`KernelParams`）

| 参数 | 默认 | 作用 | 暴露面 |
|---|---|---|---|
| `det_every` | 3 | 每 N 帧检测一次，中间帧跟踪恒速预测补位 | CLI single/pipeline/worker |
| `det_conf` | 0.3 | 源域检测阈值（宁可多糊） | CLI single |
| `det_upscale` | 1.0 | YuNet 检测放大倍率（救 40-50px 小脸；耗时≈倍率²，**默认关**；SCRFD 忽略） | 代码级 |
| `margin` | 0.15 | 模糊框外扩比例（按框宽高较大者） | CLI single |
| `margin_px` | 8 | 外扩像素下限（小脸比例外扩仅 2-4px 盖不住框抖动） | 代码级 |
| `blur_all_dets` | True | 检测帧上检测框与 track 框**并集糊**（低分框也当场糊） | 代码级 |
| `crf` | 20 | 输出编码质量（CRF=Constant Rate Factor，恒定质量系数；其余编码参数钉死，见下） | 代码级 |
| `high_thr` | 0.5 | 跟踪高分阈值（一轮匹配 + 唯一出生通道） | 代码级 |
| `low_thr` | 0.3 | 跟踪低分阈值（二轮救活下限，只救不出生） | 代码级 |
| `max_age` | 3 | 遮挡容忍的连续未匹配检测轮数（ghost 剪枝） | 代码级 |
| `detector` | yunet | `yunet` \| `scrfd`（工厂注入，升级零控制面改动） | CLI single/pipeline/worker/qc |

## 跟踪器参数（代码级，`Tracker.__init__`）

| 参数 | 默认 | 作用 |
|---|---|---|
| `iou_thr` | 0.1 | 一/二轮关联 IoU（Intersection over Union，交并比）门（三轮距离兜底无 IoU 门） |
| `alpha` | 0.6 | 速度 EMA（Exponential Moving Average，指数移动平均）系数（前 2 次命中直接采纳瞬时速度自举） |
| `frame_wh` | None | 注入后启用出画剪枝（完全出画 track 剔除；内核由 probe 注入） |

## 质检参数（输出域）

| 参数 | 默认 | 作用 | 暴露面 |
|---|---|---|---|
| `sample_fps` | 1.0 | 抽样密度；`none`/`all` = 全密度逐帧（**最终验收**） | CLI qc；pipeline 走代码级 `qc_sample_fps` |
| `pass_conf` | 0.5 | 违规判定阈值（输出域） | 代码级 |
| `T_FATAL_CONF` | 0.7 | 致命/边缘分界（重校准条件见 qc.py 注释） | 代码级 |
| 面积过滤 | [8×8, 0.4×W×H] | 面积出界的微框/巨框视为糊纹理噪声，不算违规 | 代码级（不可配） |

## 管线与调度参数

| 参数 | 默认 | 作用 | 暴露面 |
|---|---|---|---|
| `workers` | 2 | Worker 进程数（共享池消化全部视频的 chunk） | CLI pipeline |
| `segment` | 30.0 | 切片秒数；时长 ≤ segment 快路径不切 | CLI pipeline/split |
| `outdir` | `outputs` | 成品目录（qc ❌ → `qc_failed/` 隔离） | CLI pipeline |
| `workdir` | `work-pipeline` | 一次性工件目录；sqlite 模式含历史记录即拒绝（rc 2）；`--cp-url` 共享队列豁免 | CLI pipeline |
| `--cp-url` | None | 切 Go 控制面 | CLI pipeline/worker |
| `--qc-detector` | 跟随 `--detector` | 交叉验收位 | CLI pipeline |
| `ttl_s`（lease） | 900.0 | claim 租约；过期由编排器巡检回收重派 | 代码级（worker_loop） |
| `max_attempts` | 3 | 重试预算（fail/reclaim 共用；耗尽 → chunk+video 双 failed） | 代码级（契约方法默认） |
| `poll_s` | 0.5 | Worker 轮询间隔 / 编排器巡检间隔 | 代码级 |
| `idle_exit_s` | CLI 0；编排器起池 3.0 | 0=常驻（容器部署模式） | CLI worker |

## 钉死不可配项（全局一致性的前提）

- **编码**：`libx264 / preset veryfast / crf / yuv420p / +faststart`——同批次所有 chunk 同参数，concat 流复制合并才成立
- **音轨**：`-map 1:a:0? -c:a copy`，**从不重编码**
- **切片**：`-c copy -map 0 -f segment`（关键帧流复制）
- **合并**：concat 流复制，失败降级重编码（veryfast/crf20/yuv420p/音轨 copy）
- **抽样步长**：`round(fps/sample_fps)` 向上取整到与 840=lcm(2..8) **互素**，首窗取中点帧——防抽样相位与 `det_every` 锁相

## 环境变量与模型

| 变量 | 作用 |
|---|---|
| `FACEBLUR_MODEL` | 覆盖 YuNet 模型路径（默认 `models/face_detection_yunet_2023mar.onnx`） |
| `FACEBLUR_SCRFD_MODEL` | 覆盖 SCRFD 路径（默认 `models/det_500m.onnx`） |

模型与素材经 `scripts/download_assets.sh` 下载（sha256 校验固定版本）。

---

**事实源锚点**：KernelParams [kernel.py:14-25](../../src/faceblur_lite/kernel.py#L14-L25)｜Tracker 参数 [tracker.py:57-65](../../src/faceblur_lite/tracker.py#L57-L65)｜qc 三阈值+面积过滤 [qc.py:13-23](../../src/faceblur_lite/qc.py#L13-L23)、[qc.py:64-72](../../src/faceblur_lite/qc.py#L64-L72)｜信号域声明 [qc.py:1-5](../../src/faceblur_lite/qc.py#L1-L5)｜lease/attempt/poll [worker.py:15-29](../../src/faceblur_lite/worker.py#L15-L29)｜编码钉死 [ffmpeg_io.py:107-129](../../src/faceblur_lite/ffmpeg_io.py#L107-L129)｜抽样互素 [ffmpeg_io.py:48-60](../../src/faceblur_lite/ffmpeg_io.py#L48-L60)｜workdir 复用拒绝 [orchestrator.py:88-96](../../src/faceblur_lite/orchestrator.py#L88-L96)｜模型路径 [detector.py:16-34](../../src/faceblur_lite/detector.py#L16-L34)
