# BUG-009：预测框出画 → 空切片 → GaussianBlur 断言崩溃

- **日期**：2026-09-24
- **发现场景**：真实手机视频（720×1406 竖屏，70.6s，人脸快速出画）经 `blur.sh run` 处理，single 模式中途崩溃 rc=1
- **现象**：`cv2.error: (-215:Assertion failed) !_src.empty() in GaussianBlur`（blur.py），伴随 ffmpeg 端 `Broken pipe`（果不是因：Python 先崩 → 读端关闭 → ffmpeg EPIPE）

## 根因（两层，同一源头"外推无边界"）

1. **tracker.predict() 裸外推**：`t.box += t.vel` 无帧界钳制。人脸快速出画（仪表实测 vel≈11px/帧，frame 682/683）时预测框整体越出画面。
2. **expand_box 上界缺口**：`x1` 只钳 `max(0,…)` 不钳上界；框完全出右画时 `x1=725 > x2=720`，`max(x2, x1+1)` 补成 726 → `out[:, 725:726]` numpy 越界切片**静默返回空数组** → GaussianBlur 空图断言。出左画（x2≤0）不崩但语义同样错：糊出 1px 无意义边缘条带。

附随发现：出画 track 存活至 max_age 淘汰，期间 `face_frames=True` → "有人脸段"虚假延长最多 ~9 帧。

## 修复（defense-in-depth 双层）

- `blur.py expand_box`：完整钳制；框与画面无交返回 `None`；`blur_regions` 跳过 None 框（堵崩溃）
- `tracker.py Tracker(frame_wh=…)`：`predict()` 后出画剪枝——完全出画的 track 立即移除（ghost 剪枝的边界延伸，修正段语义）；半出画保留（画内还有脸）；不注入 `frame_wh` 保留纯运动学旧语义
- `kernel.py`：构造 Tracker 时传 `(meta.width, meta.height)`

## 教训

- numpy 切片越界**不报错**，静默给空数组——凡"切片下标由外部输入推导"处，钳制后必须显式判空
- 跟踪器的预测输出天然可能出画（恒速外推无界），消费端与生产端都要设防
- 单元测试只造过"画内框"，第一次跑真实手机视频（人脸出画）即暴露——边界用例要从真实数据形状里来

## 回归锁

`tests/test_blur.py::test_expand_fully_outside_returns_none`（四向参数）、`test_blur_fully_outside_box_noop`（单测复现原崩溃栈）、`test_blur_partial_outside_clamps`、`tests/test_tracker.py::test_predict_prunes_fully_outside_tracks`
