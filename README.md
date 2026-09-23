# face-blur-lite

轻量视频人脸脱敏管线——控制面/数据面分离的最小实现。

输入一段视频（demo 5 分钟、架构支持更长），输出人脸全部模糊的**新视频**（音轨保留）。以业界数据处理工作流的形态落地：控制面（SQLite 内嵌 / Go 服务双形态）+ 无状态数据面 Worker（scatter-gather 分片并行），本地单机闭环，零云成本。

## 文档

- [docs/design.md](docs/design.md) — 详细设计（四大视图 + ADR + 容错 + 业界平台对照 + 演进路线，Mermaid 图示）
- [docs/superpowers/plans/2026-09-23-face-blur-lite-mvp.md](docs/superpowers/plans/2026-09-23-face-blur-lite-mvp.md) — 实施计划（TDD 任务分解）

## 状态

设计定稿，待实施（需求 R1-R14，任务 Task 0-12）。

核心设计一句话：**迁移时换的是调度层，保留的是算子层**——控制面/数据面分离 + 契约先行（`ControlPlane` 协议，SQLite/Go 双实现过同一份契约测试）的回报。
