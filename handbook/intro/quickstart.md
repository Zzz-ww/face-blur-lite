# 快速上手

**目标：10 分钟内从零跑通 单进程脱敏 → 批量并行 → 质检 → 观测。**

## 前置

- ffmpeg/ffprobe（`brew install ffmpeg`）
- Python ≥3.10
- 想试 Go 控制面再加：go ≥1.25（`brew install go`）

## 路线 A：一条命令看完整故事（推荐首跑）

```bash
bash scripts/blur.sh demo
```

自动完成：venv 创建 + 依赖安装（`pip install -e ".[dev]"`）→ 模型/测试资产下载（`scripts/download_assets.sh`）→ 合成 2 段 30s 演示视频（move/occlude）→ single 单进程 → pipeline 批量（共享 Worker 池）→ stats 观测 → qc 质检报告。

加 `--go` 多一节 gocp 控制面模式；`--workers N` 调并行度。

## 路线 B：手动步骤（看清每一步）

```bash
# 1. 环境与资产（blur.sh demo 已代办；手动路径如下）
python3 -m venv venv
./venv/bin/pip install -e ".[dev]"
bash scripts/download_assets.sh          # 模型 → models/，测试人脸 → assets/

# 2. 造两段合成测试视频（无真人数据即可体验全流程）
./venv/bin/faceblur-lite demo-video inputs/demo/move.mp4 --duration 30 --scenario move
./venv/bin/faceblur-lite demo-video inputs/demo/occlude.mp4 --duration 30 --scenario occlude

# 3. 单进程脱敏：一个视频 → 一个成品
./venv/bin/faceblur-lite single inputs/demo/move.mp4 outputs/move_blurred.mp4

# 4. 质检：输出域复检成品（rc 0=通过）
./venv/bin/faceblur-lite qc outputs/move_blurred.mp4

# 5. 批量并行：控制面 + Worker 池，自动切片/合并/质检/隔离
./venv/bin/faceblur-lite pipeline inputs/demo/move.mp4 inputs/demo/occlude.mp4 \
  --outdir outputs/quickstart --workdir outputs/quickstart/work \
  --workers 2 --segment 30

# 6. 观测：排队/处理分位、吞吐、失败率
./venv/bin/faceblur-lite stats outputs/quickstart/work/pipeline.db
```

合成 30s 视频单进程约 4.4s ⟨RE⟩；真实视频直接替换输入路径（源片放 `inputs/`，不入库）。

## 真实批量的日常入口

```bash
scripts/blur.sh run inputs/real-video/ --outdir outputs/20261008-批次名
```

blur.sh 自动：环境引导、目录展开（mp4/mov/mkv/avi/m4v）、同 stem 碰撞预检（撞名 rc 2 不静默重命名）、输出目录 gitignore 隔离、跑批后聚合 stats + 逐成品 qc。1 个输入自动 single；≥2 个或 `--go` 自动 pipeline。批量排障与 qc_failed 流转见 [../how-to/run-batch.md](../how-to/run-batch.md)。

## 退出码速记

`0` 全过 ｜ `1` 边缘违规/有 failed ｜ `2` 环境/输入错误（优先级最高）｜ `3` qc 含致命违规。全表见 [../reference/cli.md](../reference/cli.md)。
