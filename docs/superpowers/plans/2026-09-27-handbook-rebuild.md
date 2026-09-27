# handbook 重建实施计划

- spec：[2026-09-27-handbook-rebuild-design.md](../specs/2026-09-27-handbook-rebuild-design.md)
- 产线：reference → explanation → intro/how-to → 验证对账收口；每波结束用户 checkpoint

## 波 1：reference 四篇（纯代码派生，建事实底座）

| 任务 | 产出 | 事实源 | 验证 |
|---|---|---|---|
| T1 签名普查 | （无产出，读码） | grep 全模块签名/常量 + 读 cli.py/control.py/kernel.py 全文、state.py DDL、gocp 端点 | 签名与常量逐一过眼 |
| T2 | handbook/reference/cli.md | cli.py | 子命令数/参数/默认值/退出码与代码一致 |
| T3 | handbook/reference/contract.md | control.py/state.py/gocp/main.go | 12 方法+DDL+12 端点对照齐 |
| T4 | handbook/reference/config.md | kernel.py/qc.py/tracker.py/ffmpeg_io.py/orchestrator.py | 全参数含信号域标注 |
| T5 | handbook/reference/modules.md | 全模块 docstring+签名 | 19 模块一表流+依赖铁律 |

## 波 2：explanation 四篇（T1 底座 + T2 融合）

| 任务 | 产出 | 要点 |
|---|---|---|
| T6 | handbook/explanation/architecture.md | 分离架构/两级状态机/六语义/容错矩阵；重测数字（pytest、冒烟） |
| T7 | handbook/explanation/dataflow.md | 格式链 7 层 8 形态/pipe O(1)/跟踪补帧/并集糊/大脸快路径 |
| T8 | handbook/explanation/industry.md | T2：五层版图/六语义×引擎/胜算-LAS-百舸两层口径/迁移原则 |
| T9 | handbook/explanation/embodied.md | T2：具身链路/对照表/换算子不换错误模型 |

## 波 3：intro + how-to 六篇 + README（从底座提炼）

| 任务 | 产出 | 要点 |
|---|---|---|
| T10 | handbook/intro/overview.md + quickstart.md | 成果速览=波 2 重测数字 |
| T11 | handbook/how-to/run-batch.md | 进度双出口/qc_failed/退出码排障表 |
| T12 | handbook/how-to/switch-gocp.md | --go/--cp-url/curl 验证 |
| T13 | handbook/how-to/extend.md | Detector 协议/算子注册/K8s 映射 |
| T14 | handbook/README.md | 四象限导航+分工声明 |

## 波 4：数字重测 + 实跑验证 + 对账收口

| 任务 | 内容 |
|---|---|
| T15 重测 | pytest 全量（计数+耗时）、模型体积、单帧耗时、single/pipeline 冒烟计时、stats 输出 |
| T16 实跑验证 | quickstart/run-batch/switch-gocp 三篇逐字实跑（临时目录+outputs 约定） |
| T17 抽检 | 继承数字选 1 配置复跑（违规矩阵）比对 |
| T18 对账 | 扫旧 docs/ 机制+数字声明 → drift-audit.md |
| T19 收口 | 锚点抽查、行数预算核对、缩写全拼一致性扫描（reference 象限：CRF/HTTP/JSON/URL/DDL/WAL 等首现展开，格式同 explanation 象限 `ABBR（Full Name，中文）`；图节点与 ASCII 框线图保持紧凑不塞全拼；纯产品名不展开）、用户验收 |

## 约束

- 数字纪律：文档里每个数字要么本轮重测、要么标 ⟨fa65f90⟩ 继承，禁止无出处数字（历史教训：pipeline-guide 波 381）
- outputs 约定：实验一律进 `outputs/20260927-实验名/`
- 本地运行不 commit，收口后用户拍板
