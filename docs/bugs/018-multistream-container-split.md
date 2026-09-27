# BUG-018：多流容器切片失败（DJI data 流无 mp4 codec tag）

- **发现**：2026-09-27 用户实跑 `blur.sh run inputs/real-video --outdir outputs/20260927-test1/ --mode pipeline`
- **任务**：真实视频波（DJI Mimo 素材）
- **状态**：待修复（方案已定盘暂缓落地，见"修复"节）

## 现象

pipeline 切片阶段 ffmpeg 报错后单视频提交失败（批次继续）：

```
[mp4 @ …] Could not find tag for codec none in stream #2, codec not currently supported in container
[out#0/segment @ …] Could not write header (incorrect codec parameters ?): Invalid argument
[pipeline +0s] ✖ 提交失败：dji_mimo_…_video.MP4（CalledProcessError）
```

同一文件走 `single` 模式可正常处理（见根因）。

## 根因

输入是 DJI Mimo 录制的 MP4，容器共 **4 条流**：

| # | codec | 类型 | 说明 |
|---|---|---|---|
| 0 | h264 | video | |
| 1 | aac | audio | |
| 2 | **unknown** | **data** | DJI 私有遥测流（tag=`priv`，GPS/姿态元数据） |
| 3 | mov_text | subtitle | |

[splitter.py](../../src/faceblur_lite/splitter.py) 切片命令用 `-map 0`（**全量流复制**）+ `-f segment`：mp4 segment muxer 要为 stream #2 写 header，unknown codec 在 mp4 容器没有对应 codec tag → 写头失败 → ffmpeg 非零退出 → `check=True` 抛 `CalledProcessError` → 编排器提交期失败隔离（R10）接住。

对照证据：老 `real_video.MP4` 只有 v+a 两路，同命令一直正常。

**全链路流映射现状**（爆炸半径审计）：

| 调用点 | 流映射策略 | 多流输入 |
|---|---|---|
| FFmpegReader（qc/内核读） | rawvideo pipe，ffmpeg 默认取最佳视频流 | 安全 |
| FFmpegWriter（内核写成品） | `-map 0:v:0 -map 1:a:0?` 显式选择 | 安全 |
| merger | concat 自家 chunk | 安全 |
| **splitter** | **`-map 0` 全量复制** | **唯一爆点** |

## 修复（待做）

**短补丁**：splitter `-map 0` → `-map 0:v:0 -map 0:a:0?`（保首视频+可选音频，丢 data/subtitle/缩略流）。

**正解（2026-09-27 brainstorming 已定盘，用户裁决暂缓，后续波落地）**：入口标准化 normalize 算子——

- `normalize_video(input, workdir) -> str` 纯函数 file→file：probe 流清单，干净（恰好 1 视频 + ≤1 音频 + mp4/mov 容器）→ 原样返回零成本；否则 `-map 0:v:0 -map 0:a:0? -c copy` remux 到 `workdir/normalized/<k>_<stem>.mp4`（秒级零损）
- 接线：`_orchestrate` 在 split 前调用 + `prog.event("normalize")`；CLI `normalize` 子命令（R13 算子化惯例）；splitter 同步收紧为纵深防御
- 覆盖范围裁决：**仅流清洗+容器统一**（remux copy）；转码统一（HEVC 10bit/HDR 档案）、旋转烘焙、VFR→CFR 明确 out of scope（罕见档 remux 失败显式失败，记 backlog）
- 估时：TDD 五任务约半天；测试含多流 fixture 合成（视频+双音轨+mov_text）、干净快路径、端到端 run_pipeline 回归

## 教训

- 真实多源素材的容器不止 v+a 两路：DJI/手机录制常带 data 遥测流、字幕流、缩略视频流
- 下游算子对输入的隐式假设（"输入=单视频+可选音轨"）散落各调用点，应在入口一处显式化——业界对位 = 数据管线 ingest/normalization 级（ETL staging 标准化微缩版）
- R10 提交期失败隔离兜住了新失败类别：单视频失败不砸批次、错误可见——但"能兜住"不等于"该让它发生"
