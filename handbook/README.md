# face-blur-lite handbook

视频人脸脱敏管线的**体系文档集**，以当前代码实现为唯一事实源重推（Diátaxis 四象限组织）。

> **两套文档的分工**：本集（`handbook/`）= 机制与参考，矛盾时以本集为准；`docs/` = 深度叙事、设计决策过程、bug 取证史、波次过程记录。每篇尾部附事实源锚点（file:line）。

## 结构全景图

一图看全：**控制面是状态的唯一拥有者，数据面零状态、可任意 kill/扩容**；算子全部是纯函数（文件→文件）。

```mermaid
flowchart TB
    U["使用者"] --> SH["blur.sh<br/>环境引导 · 聚合报告 · 退出码梯"] --> CLI["faceblur-lite CLI · 9 子命令"]

    CLI --> E1["① single — 单进程直达算子<br/>（不经控制面）"]
    CLI --> E2["② pipeline — 经编排器（默认）"]
    CLI --> E3["③ worker — 独立数据面<br/>（跨机 / 容器）"]

    E2 --> ORCH["orchestrator 编排器<br/>切片提交 · lease 巡检 · qc 收尾"]

    subgraph DP["数据面 —— 零状态 · 可任意 kill / 扩容"]
        direction LR
        WLOOP["worker_loop × N<br/>认领 → 执行 → 事务回写"] --> OPS["纯函数算子（文件 → 文件）<br/>split · kernel · merge · qc"] --> PARTS["kernel 部件<br/>detector YuNet/SCRFD · tracker · blur"] --> FIO["ffmpeg_io<br/>rawvideo pipe · 内存 O(1)"]
    end

    subgraph CP["控制面 —— 状态的唯一拥有者"]
        direction LR
        PROTO["ControlPlane 协议 · 12 方法<br/>互斥认领 · lease · 围栏 · 原子 Join"]
        SQLITE["SqliteControlPlane<br/>SQLite WAL · 默认"]
        HTTP["HttpControlPlane<br/>urllib 适配"]
        GOCP["gocp · Go 服务<br/>无 CGO · 12 端点"]
        DB[("SQLite 库<br/>videos / chunks 两级状态机")]
        PROTO -.->|"参考实现"| SQLITE
        PROTO -.->|"--cp-url 切换"| HTTP
        HTTP <--> GOCP
        SQLITE --- DB
        GOCP --- DB
    end

    E1 --> OPS
    E3 --> WLOOP
    ORCH -->|"submit · reclaim · finish"| PROTO
    ORCH -->|"split 切片"| OPS
    WLOOP -->|"claim · complete · fail"| PROTO

    OBS["stats / progress 观测<br/>只读旁路 mode=ro · 不进契约"]
    DB -.-> OBS

    %% ── 分层配色 ──
    classDef user fill:#f8fafc,stroke:#64748b,color:#334155
    classDef entry fill:#fff7ed,stroke:#ea580c,color:#7c2d12
    classDef data fill:#ffffff,stroke:#16a34a,color:#14532d
    classDef ctrl fill:#ffffff,stroke:#2563eb,color:#1e3a8a
    classDef store fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a
    classDef obs fill:#faf5ff,stroke:#9333ea,stroke-dasharray:6 4,color:#581c87

    class U user
    class SH,CLI,E1,E2,E3,ORCH entry
    class WLOOP,OPS,PARTS,FIO data
    class PROTO,SQLITE,HTTP,GOCP ctrl
    class DB store
    class OBS obs

    style DP fill:#f0fdf4,stroke:#16a34a,stroke-width:2px,color:#166534
    style CP fill:#eff6ff,stroke:#2563eb,stroke-width:2px,color:#1e40af

    %% ── 关键边强调：契约边蓝粗、数据面流水线绿、观测旁路紫 ──
    linkStyle 6,7,8 stroke:#16a34a,stroke-width:2px
    linkStyle 16,18 stroke:#2563eb,stroke-width:3px
    linkStyle 19 stroke:#9333ea
```

**scatter-gather 一行版**（批量模式的数据旅程）：

```
视频 ─split（流复制）→ [chunk 0..N] ─blur × N 并行→ merge（concat 流复制）→ qc（输出域复检）
                                                              ├─ ✅ 0 违规 → 成品 outdir/
                                                              └─ ❌ 有违规 → qc_failed/ 隔离（人工复核）
```

## 四象限导航

| 象限 | 篇目 | 状态 | 回答什么 |
|---|---|---|---|
| **intro** | [overview](intro/overview.md) · [quickstart](intro/quickstart.md) | ✅ 已落地 | 这是什么 / 怎么跑起来 |
| **how-to** | [run-batch](how-to/run-batch.md) · [switch-gocp](how-to/switch-gocp.md) · [extend](how-to/extend.md) | ✅ 已落地 | 怎么批量跑 / 切 Go 控制面 / 加检测器·算子·上 K8s |
| **reference** | [cli](reference/cli.md) · [contract](reference/contract.md) · [config](reference/config.md) · [modules](reference/modules.md) | ✅ 已落地 | 逐命令 / 逐方法 / 逐参数 / 逐模块的权威表 |
| **explanation** | [architecture](explanation/architecture.md) · [dataflow](explanation/dataflow.md) · [industry](explanation/industry.md) · [embodied](explanation/embodied.md) | ✅ 已落地 | 为什么这样设计 / 一帧的旅程 / 业界坐标 / 具身定位 |

另有 [drift-audit.md](drift-audit.md)（与旧 docs/ 的对账清单，内部件，外部读者可跳过）——收口波产出。
