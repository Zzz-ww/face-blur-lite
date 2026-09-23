# BUG-003 tracker 出生不计 hit + iou_thr 排除出生首跳

- **发现**：2026-09-24，Task 4（mini-SORT）实施轮——verbatim 实现过不了计划自带的 5 个测试中的 3 个（constant_velocity / occlusion / low_conf）
- **现象**：速度恒 0 不收敛；两轮后 hits==2 断言差 1；track 连锁重生
- **根因**：两个互相独立的 bug——
  1. 出生不设 `hits=1`：`Track` 默认 hits=0，出生后首次匹配才 +1，两轮后 hits==1，而计划测试断言 `t.hits == 2`
  2. `iou_thr=0.3` 排除出生首跳：新生儿 vel=0、无预测可依赖，40px 框每检测轮移动 30px 的裸 IoU=0.143 < 0.3 → 出生后第一次匹配**永不可能** → 高分框连环出生新 track（occlusion 测试同几何，一并炸）
- **修复**：出生 `Track(..., hits=1)`（SORT 语义：出生检测计入 hit）+ `iou_thr` 默认 0.3→0.1。demo 几何不受影响（120px 脸 ~6.5px/帧、gap=3 时 IoU≈0.72）；速度收敛轨迹独立推演复核（4.0→6.4→7.84→8.70→9.22，落在断言带 [7,11]）。五个测试保持 byte-verbatim
- **教训**：verbatim 实现失败时先把失败按根因分组（这里是两个独立 bug 叠加）；关联阈值必须与测试几何一起推演——出生首跳是纯裸 IoU 的特例，没有预测补偿
