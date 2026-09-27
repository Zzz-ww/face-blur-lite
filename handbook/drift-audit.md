# drift-audit：handbook 与旧 docs/ 对账清单

> **内部件**（外部读者可跳过）。方法：handbook 以代码为唯一事实源重推（T1），旧 `docs/` 为叙事与研究档案（T2）；两者冲突以代码/handbook 为准（见 [README.md](README.md) 分工声明）。旧档不改——它们记录的是"当时的真相"。

## 总评

- **机制层零漂移**：12 方法语义、DDL 逐字、12 端点、围栏/终态/原子 Join 与代码全部一致；139 测试全绿（含契约 14 场景 ×2 双跑）——对账期间未发现任何机制声明失效
- **数字层三类分歧**：测次代际 / 口径演进 / 口径差异（下表 1-3），无一属于错误声明
- **疑点排除两项**（下表 4-5）、**易错点记录一项**（下表 6）

## 清单

| # | 旧档位置 | 旧说法 | 当前事实（本轮复测/复码） | 性质 | 处置 |
|---|---|---|---|---|---|
| 1 | [docs/overview.md](../docs/overview.md) §1.3（引根 README 指标节） | 吞吐 17.35 chunks/min；queue p50 19.3s / p95 38.2s；proc p50 10.7s / p95 12.8s | ⟨RE⟩ 16.76 chunks/min；queue 0.9 / 9.3s；proc 10.3 / 12.2s——同机不同测次，同量级 | 数字代际 | handbook 基线以 ⟨RE⟩ 为准，旧数存档不引用 |
| 2 | docs/overview.md §1.3 质量行 | 违规 621→308；默认口径 23.2% | ⟨RE⟩ 同口径复测默认配置 27.8%（589/2121，fatal 117）——[bugs/014](../docs/bugs/014-tiny-face-capability-envelope.md) 早已注记"闸门灵敏度变化" | 口径演进（qc 面积过滤等闸门变更后，历史绝对值不可直接复现） | architecture.md 实测基线双行并存：⟨RE⟩ 新测 + ⟨fa65f90⟩ 阶梯（各标口径） |
| 3 | docs/overview.md §1.3 模型行 | YuNet 12.73ms / SCRFD 15.52ms（1.22×） | ⟨RE⟩ 8.38 / 17.58ms（2.1×）——**帧尺寸口径不同**：旧=720×1406 真实帧，新=640×480 合成帧 | 口径差异（两数各自有效，比值不可横比） | architecture.md 基线已标注帧尺寸条件 |
| 4 | docs/overview.md §1.4 | CLI 9 个子命令 | 代码实测 9 ✓（编写期疑点"旧档普遍写 8"不成立） | 疑点排除 | — |
| 5 | —（模块计数） | — | `src/` 下 18 个 .py 含 `__init__.py`；[modules.md](reference/modules.md) 写"17 个 Python 模块"= 不含 `__init__` | 口径自洽 | — |
| 6 | —（易错点） | 旧档无 хуже误述 | `idle_exit_s` 三重默认：CLI 子命令 0（常驻）/ `worker_loop` 形参 15.0 / 编排器池 3.0——三个位置三个值 | 易错点记录（非漂移） | [config.md](reference/config.md) 已三列并列 |
| 7 | —（handbook 自身） | — | T16 逐字实跑暴露两处编写错误并当日修复：quickstart 路线 B 漏生成 occlude.mp4；switch-gocp curl 示例 `chunk_inputs` 误写二元组（正确为 `[idx, input, output]` 三元组，实跑 400 后核码改正） | 验证收益注记 | 已修复入正文 |

## 覆盖范围声明

- **对账对象**：docs/ 主档的机制与数字声明（权威数字表、调度语义、吞吐、质量、模型、计数类）；契约类声明（12 方法/DDL/端点/六语义）经 reference 象限逐一对码核验
- **不在范围**：T2 叙事篇（platform-mapping、industry/embodied 的行业梳理内容）——研究产物非机制声明；docs/superpowers/ 过程档案；docs/bugs/ 取证史（本身就是历史记录）
- 复测产物：`outputs/20260927-t16-verify/`（三篇 how-to 实跑 + gocp curl 契约 + T17 全密度复检）
