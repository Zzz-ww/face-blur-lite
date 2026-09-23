# BUG-006 切片快路径不建目录，Worker ENOENT 耗尽重试

- **发现**：2026-09-24，Task 10（worker+orchestrator）实施轮——`test_batch_multi_video_shared_pool` 确定性失败（短视频 `failed`）
- **现象**：批量测试里长视频 `done`、短视频 `failed`；Worker stderr：`Error opening output .../blurred_000.mp4: No such file or directory`，3 次尝试耗尽
- **根因**：`split_video` 快路径（时长≤segment_s 不切）在 `out_dir` 创建**之前**就 `return [input_path]`——"什么都不做"连目录也不建。而编排器把 chunk 输出路径算在 `wd/chunks/01_short/` 下：目录不存在 → Worker 的 ffmpeg 打不开输出 → 重试耗尽 → video 误判 failed。Task 8 单测全绿是因为测试只调 split 不往 out_dir 写东西——跨任务集成点才暴露
- **修复**：编排器提交循环里 `chunk_dir.mkdir(parents=True, exist_ok=True)`（算输出路径者负责父目录；splitter 的快路径契约"零副作用"保持不动）。计划母本同步
- **教训**：快路径省略的不仅是计算，还有副作用（目录创建）——审计快路径要看它跳过了什么；"谁建目录"这类集成契约在计划期就该写明归属，而不是靠集成测试兜底
