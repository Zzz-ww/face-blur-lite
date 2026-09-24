# BUG-007 concat 列表相对条目按列表目录解析，合并 ENOENT 视频永卡 merging

- **发现**：2026-09-24，Task 11 批量冒烟（R10 手动验收）——`--outdir outputs --workdir work-smoke` 相对路径下三视频全部 `merging`，`error: "Worker 池退出时任务未到 qc_wait"`
- **现象**：ffmpeg 报 `Impossible to open 'outputs/work-smoke/chunks/00_a_5min/blurred_000.mp4'`——条目被拼上了列表文件所在目录 `outputs/`；copy 与降级重编码都失败 → merge 抛异常 → worker 对已完成 chunk 调 `fail_chunk`（Task 7 Minor：无终态守卫）→ done 翻回 pending、attempt 耗尽，视频搁浅 `merging`（Task 10 已知搁浅路径首次被真实触发）
- **根因**：`merge_chunks` 把调用方给的相对路径**原样**写进 concat 列表，而 ffmpeg concat demuxer 按列表文件所在目录（非进程 cwd）解析相对条目——列表在 `outputs/` 下，相对条目就变成 `outputs/work-smoke/...`。既有测试全走 `tmp_path` 绝对路径，恰好绕开
- **修复**：concat 条目改写 `Path(p).resolve()` 绝对路径（相对路径以调用方 cwd 为准，语义正确；绝对路径不变）。顺带修好 `merge` CLI 子命令接收用户相对路径的场景。计划母本同步（Task 8 代码块）
- **教训**：跨进程边界的文件引用一律落绝对路径——ffmpeg 自己解析路径的入口（concat 列表/-f image2 pattern 等）解析基准是输入文件目录而非 cwd，与直觉相反；测试全用 `tmp_path` 绝对路径时会系统性绕开相对路径 bug，CLI 用户路径场景需要至少一条相对路径回归测试（终审清单已记）
