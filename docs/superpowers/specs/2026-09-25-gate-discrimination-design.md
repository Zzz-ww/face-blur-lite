# 闸门恢复判别力（fencing + qc 分级 + 顺手五件）设计 v1

- **日期**：2026-09-25
- **状态**：待用户评审
- **背景**：benchmark 复核（9 代理核实）两个"推荐下一波"项打包——控制面唯一真正
  确性洞（complete/fail 无持有者围栏，刀 4）+ 质量面判别力缺失（qc 单比特，刀 2）。
  用户已拍板两项：**分级只分流、任何违规仍隔离**（边缘自动转正=非目标）；
  **顺手五件全打包**。
- **证据基座**：5 真实样本 444 处违规 conf×尺寸分布已采（2026-09-25，当前代码
  确定性复现）——celebv_5min 9 处全在 [0.5,0.6)、street 361 处 42% ≥0.7、
  仅有的 2 个视觉定性标签（真半糊）在 0.838/0.849；尺寸在各 conf 带间不分离
  → 分级轴取**纯 conf 单轴**。

## 1. fencing 复合围栏（契约 v1.2）

**围栏键 = (worker_id, attempt) 复合**。裸 worker_id 不够：orchestrator 把 Worker
命名为 f"w{n}"，gocp 共享队列模式下跨轮同名转世，上一轮僵尸 w0 的迟到 ack 能命中
下一轮 w0 的 running 行。attempt 在 fail/reclaim 每次+1、claim 不加——唯一标识
一个持有纪元，是 SQS receipt handle（每次投递唯一，非消费者身份）的正确等价物。

**签名（三实现同步，必填关键字，不留 None 过渡后门）**：

- `complete_chunk(chunk_id: str, metrics: dict, *, worker_id: str, attempt: int) -> bool`
- `fail_chunk(chunk_id: str, error: str, *, worker_id: str, attempt: int, max_attempts: int = 3) -> None`

**实现**：

- state.py：complete 的 UPDATE WHERE 追加 `AND worker_id IS ? AND attempt IS ?`；
  fail_chunk 两个 UPDATE 分支（耗尽→failed / 未耗尽→pending 重排）同款追加。
- remote.py：两 POST body 加 `worker_id`/`attempt` 字段透传。
- gocp handleComplete/handleFail：req 加 `WorkerID`/`Attempt`，**缺失即 400**
  （防新旧二进制混跑时围栏静默失效）；UPDATE WHERE 同步。
- worker.py：传 `worker_id=worker`（闭包现成）、`attempt=task["attempt"]`
  （claim 应答 dict(row) 已含 attempt）。
- 仓内既有调用点同 commit 补参：tests/test_contract.py ×5、test_progress.py ×1、
  test_go_cp.py ×1。

**语义边界（写进契约文档）**：围栏修的是状态机不被假 done 污染、merge 不被僵尸
偷认领、迟到 fail 不弹回现任任务；**不修**数据面 at-least-once 双写（僵尸对同一
chunk 输出文件的迟到写仍在，attempt 上限兜底）——PR 里不许超卖。

**契约测试 +2 场景（参数化双跑）**：

1. 刀 4 原时序：w1 claim→lease 过期→reclaim→w2 再 claim→w1 迟到
   complete(worker_id=w1, 旧 attempt) 返回 False；w2 complete 返回 True。
2. 同名转世：w1 claim→过期→reclaim→**w1 同名**再 claim→用**旧 attempt** ack
   仍拒（裸 worker_id 挡不住、复合挡住的对照）。
3. gocp 独有：缺 worker_id/attempt 的 complete POST → 400。

## 2. qc 分级（conf 单轴，T_FATAL=0.7 临时）

- qc.py：具名常量 `T_FATAL_CONF = 0.7`（注释：临时待校准——依据与重校准触发条件
  见 docs/qc-severity-calibration.md）；`Violation.severity: str` 构造时按
  `conf >= T_FATAL_CONF` 分 "fatal"/"marginal"；`QCReport.fatal_count /
  marginal_count` property；`passed` 语义不变（not violations）。
- **隔离语义零变动**：任何违规仍隔离 qc_failed/、status 仍二值 done/qc_failed
  （severity 不进 ControlPlane 契约）。severity 只做三件事：
  1. stderr/qc_result 事件分列"致命 X · 边缘 Y"；
  2. 复核清单（cli qc JSON）按 severity 可排序；
  3. 退出码分层。
- **退出码（两层一致）**：`0` 全过 / `1` 仅边缘违规（或批内存在非 done 视频
  且无致命）/ `3` 存在致命违规 / `2` 沿用=环境/输入错误。优先级 2 > 3 > 1。
  - cli.py qc：JSON 顶层加 `fatal`/`marginal` 计数（violations 元素经
    v.__dict__ 自然带 severity）；退出码经小助手 `_qc_exit_code(rep)`（可单测）。
  - cli.py pipeline：`any(r.qc.fatal_count > 0 for r in results)` → 3，
    否则 any 非 done → 1。
  - blur.sh qc_one：python 解析行加读 fatal 计数；`QC_BAD` 之外拆
    `QC_FATAL`/`QC_MARGINAL` 全局量；❌ 行分列"致命 X · 边缘 Y"；rc 改写链
    `2 > 3 > 1`；头部退出码注释同步。
- 校准文档 `docs/qc-severity-calibration.md`（精简一页）：444 处分带表 +
  T=0.7 依据（celebv 全边缘 vs street 42% 高置信的结构分离；仅有的 2 个视觉
  标签 ≥0.84；0.5-0.7 段零标签=已知局限）+ 触发重校准条件（SCRFD 升级 /
  新视觉定性标签 ≥20 处）。

## 3. 顺手五件

1. **全密度 CLI**：`--sample-fps` 自定义 type（`"none"`/`"all"` → None）——
   刀 1 尾句"CLI 不可达"闭合。
2. **box 语义标注**：qc.py `box` 字段注释 `(x1, y1, x2, y2) 左上/右下像素
   坐标——非 xywh`；pipeline-guide qc 节同句（2026-09-25 取证已实际误读一次）。
3. **blur.sh mv 容错**：`mv` 失败 `|| echo 警告` 不砸收尾——与 Python 侧
   "隔离失败不砸批次、缺陷可见"对齐（set -e 语义两套行为的收口）。
4. **隔离件防同名覆盖**：`_quarantine` dst 已存在 → `_1/_2…` 后缀递增；
   blur.sh qc_one 同逻辑（`while [ -e ]` 探测）。保住隔离波"复核证据留存"承诺
   （POSIX rename/mv 静默覆盖是上波 record-only 遗留）。
5. **chunk_retry 事件**：orchestrator 巡检环 attempt 增加 && pending 处
   （现 `↻` prog.line 旁）补 `prog.event("chunk_retry", video=…, idx=…,
   attempt=…)`；pipeline-guide §7.2 事件枚举同步。

## 4. 测试

- Python 全 TDD：契约 +2 场景双跑（§1）、qc 分桶合成 conf（0.69/0.70 边界）、
  `_qc_exit_code` 单测、cli qc JSON 形状、`_quarantine` 二次隔离双文件、
  chunk_retry 事件入 log（test_progress）、blur.sh `bash -n` + demo 回归。
- 活体对抗验证（终审内嵌，沿用上波惯例）：rc 优先级链 2>3>1、mv 容错、
  同名后缀 shell 侧、gocp 400 拒收。

## 5. 文档

- docs/control-plane-contract.md：v1.2——complete/fail 行加持有者语义、
  §4 不变量加"lease 失效后原持有者的 ack/fail 不受理"。
- docs/pipeline-guide.md：§6.5 分级处置行、§7.2 chunk_retry、qc 节 xyxy 句、
  退出码表。
- scripts/blur.sh 头部退出码注释。
- docs/qc-severity-calibration.md（新建，§2）。

## 6. 非目标

- 边缘自动转正 / marginal 自动放行（"qc 是信号，人是裁判"）；
- severity 入 ControlPlane 契约或 videos.status；
- resume、SCRFD、fail_chunk attempt 上限调整、数据面双写消除。
