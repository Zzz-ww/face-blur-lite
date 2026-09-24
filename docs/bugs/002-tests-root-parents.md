# BUG-002 tests/ 下 parents[2] 解析越过项目根

- **发现**：2026-09-24，Task 3（detector）实施轮
- **现象**：`tests/test_detector.py` 报 "2 skipped"——模型与资产明明都在 `models/`、`assets/`
- **根因**：`Path(__file__).resolve().parents[2]` 在 tests/ 下的文件里解析到项目根的**上一级**（tests/ 的 parents: [0]=tests, [1]=项目根, [2]=上级目录），`MODEL.exists()` 恒 False，skipif 守卫误触发。同一表达式在 `src/faceblur_lite/` 下（[0]=包目录, [1]=src, [2]=项目根）才是对的——计划把后者直接复用到了前者
- **修复**：计划中 tests/ 下的三处 `parents[2]` → `parents[1]`（Task 3 测试、Task 5 conftest、Task 12 go_bin fixture）；src/ 下两处（detector.py、cli.py）确认正确保留
- **教训**：`parents[N]` 的合法层数取决于文件所在目录深度；跨目录复用同一表达式时逐位置核一遍
