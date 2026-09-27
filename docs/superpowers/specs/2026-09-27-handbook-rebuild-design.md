# handbook：以代码为基准重推的完整文档集（设计）

- 日期：2026-09-27
- 状态：已批准（头脑风暴三轮定盘：目的 → 数字口径 → 骨架；目录名用户定夺 `handbook/`）
- 前置决议：交付版+对账双目的；三层证据模型；Diátaxis 四象限

## 背景

现有 `docs/` 按波次生长（叙事密、时间线深、冷读门槛高）。本设计以**当前代码实现为基准**重推一套体系化文档，存独立目录 `handbook/`，与旧集并存分工。用户最终目的：可交付（面试官/外部读者可直接冷读）+ 顺手对账（抓旧文档漂移）。

## 决策记录

| 决策点 | 结论 |
|---|---|
| 首要目的 | 干净交付版 + 与旧 docs/ 对账输出漂移清单，一举两得 |
| 证据三层 | T1 事实源=代码（18 模块+gocp+blur.sh+测试）；T2 参考源=全局设计文档（业界对照/具身定位/ADR 动机，来源 overview/report/benchmark/platform-mapping/design）；T3 排除=波次叙事、bug 取证过程、showcase 走查 |
| 冲突裁决 | T1 与 T2 冲突以代码为准（正是对账要抓的漂移） |
| 数字口径 | 便宜重测（pytest 计数/耗时、模型体积、单帧耗时、冒烟计时、stats 输出）+ 贵的继承标注（real_video 违规矩阵、SCRFD A/B、包络探针——标 fa65f90 口径）；对账时抽 1 套继承项复跑验证 |
| 骨架 | Diátaxis 四象限：intro / how-to / reference / explanation |
| 目录 | 仓库根 `handbook/`，入 git；commit 时机用户拍板 |

## 文档清单（14 篇 + 对账件）

| 篇 | 契约 | 证据 | 数字 |
|---|---|---|---|
| README.md | 四象限导航 + 两套文档分工声明（矛盾以 handbook 为准） | — | — |
| intro/overview.md | 项目是什么/三形态/成果速览 | T1 | 重测 |
| intro/quickstart.md | 从零到跑通，命令可逐字复制 | T1 | 重测 |
| how-to/run-batch.md | 批量全流程：进度双出口/隔离区/退出码排障 | T1 | 重测 |
| how-to/switch-gocp.md | --cp-url 切 Go 控制面+验证+适用场景 | T1 | 重测 |
| how-to/extend.md | 加检测器/加算子/上 K8s 三条扩展路径 | T1+T2 | 继承 |
| reference/cli.md | 9 子命令×参数×默认值×退出码梯 | T1 | — |
| reference/contract.md | 12 方法契约语义+DDL+gocp 12 端点对照 | T1 | — |
| reference/config.md | 全参数表逐项标注信号域 | T1 | — |
| reference/modules.md | 19 模块地图一表流+依赖铁律 | T1 | — |
| explanation/architecture.md | 控制面/数据面、两级状态机、六语义、容错 | T1 | 重测 |
| explanation/dataflow.md | 一帧的旅程：格式链/pipe O(1)/并集糊/快路径 | T1 | 重测 |
| explanation/industry.md | 五层版图+六语义×引擎矩阵+三平台两层口径 | T2 | — |
| explanation/embodied.md | 具身链路定位+对照+"换算子不换错误模型" | T2 | 继承 |
| drift-audit.md | 对账清单（内部件，README 标注可跳过；去留用户定） | 混合 | 混合 |

## 可审计性机制

1. 每篇尾部**事实源锚点**：机制声明的 `file:line` 清单；T2 内容标 `[源：xx.md §x，经代码复核]`
2. 每个实测数字来源标记：⟨RE⟩ 本轮重测 / ⟨fa65f90⟩ 继承档，收在各篇末"数字来源"行，不进正文
3. 图示 Mermaid（GitHub 原生渲染），每篇 ≤2 图，从代码重画（例外：dataflow.md 3 图——文首全程总览 + 帧级/批量两处特写，2026-09-27 用户追加总览图）

## 写作产线（顺序即方法论）

reference → explanation → intro/how-to → 验证对账收口。参考层先建事实底座（纯代码派生零叙事）→ 机理层在底座上论证（融合 T2）→ 入口层最后从底座提炼。文档版"先测试后实现"。

## 验证与对账

- quickstart / run-batch / switch-gocp 三篇写完按文档逐字实跑（临时目录），命令即证据
- 对账：以新文档（=代码事实）扫旧 docs/ 机制与数字声明 → 过时数字/已变机制/口径不一致 → drift-audit.md
- 继承数字抽检：违规矩阵选 1 配置复跑比对

## 非目标

- 不替换旧 docs/（分工并存，历史与深挖留在旧集）
- 不写 19 篇逐模块文档（modules.md 一表流）
- 不复制 bug 取证过程与波次叙事（Tier 3 排除）
- 不做 CI 文档一致性自动化（本期人工锚点）

## 验收标准

1. 14 篇全部落地，四象限导航完整
2. 三篇 how-to/intro 实跑命令逐字可复制执行成功
3. 每篇事实源锚点存在且抽查 file:line 命中
4. drift-audit.md 列出旧文档漂移点清单（含抽检复跑结果）
5. 总量 1200-1500 行
