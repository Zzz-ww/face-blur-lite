# BUG-012：小脸的比例 margin 太窄——检测框抖动露出脸缘

- **日期**：2026-09-24
- **发现场景**：真实视频全密度复检后，对 det_every=1 输出（无预测混杂）
  做源域深检分类：93% 违规是"半糊"变体——同一张脸源域已检出（IoU
  0.4–0.8），糊区却只盖住一部分，脸缘裸露仍可被输出域 ≥0.5 检出。

## 根因

小脸（10–30px）的模糊外扩按比例算只有 2–4px，而检测框帧间抖动 +
输出域重检的框差异可达 ±30–50% 尺寸；比例 margin 系统性盖不住。
比例放大（0.25×）对大脸过度糊——需要的是**像素下限**而非更大比例。

## 修复

`blur.py expand_box(blur_regions)` 增加 `margin_px`（默认经
`KernelParams.margin_px=8` 注入）：外扩取 `max(比例×尺寸, 像素下限)`。
小脸获得 ±8px 环带（盖住抖动），大脸仍由比例主导。

## 效果与残余

与 det_every/margin 组合实测（违规帧/2121）：det1+px8 487、det1+px14
403——单独不足以收敛，与 BUG-013 并集糊组合后 det1+px8+并集 = 308。
残余为检测器能力包络（源域 <0.3 的不可检出/域移小脸）。

## 回归锁

`tests/test_blur.py::test_margin_px_floor_governs_small_boxes`（下限
生效/比例主导/默认 0 三态）、`test_margin_px_floor_blurs_wider_than_box`
