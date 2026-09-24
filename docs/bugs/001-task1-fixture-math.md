# BUG-001 Task 1 测试 fixture 帧值数学矛盾

- **发现**：2026-09-24，Task 1（ffmpeg_io）实施轮
- **现象**：`test_writer_reader_roundtrip` GREEN 失败——`np.mean(np.abs(got[0].astype(int) - 40)) < 6` 无法满足
- **根因**：计划 fixture 写帧值 `(i * 40) % 255`，i=0 时整帧=0；断言却要求首帧≈40。生成式与断言互相矛盾（verbatim 对不可满足），与实现无关
- **修复**：`((i + 1) * 40) % 255`（帧值 40..200），两条断言保持原文不动；计划母本同步
- **教训**：verbatim 计划也要先做"fixture ↔ 断言"一致性静态检查——尤其取模/边界这类让首帧特异的写法
