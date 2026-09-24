# blur.sh 本地运行脚本设计（spec）

> 2026-09-24 ｜ 状态：设计已确认（三问定向 + 七节提案 + qc_failed=1 裁决），已实施（feat/blur-sh）
>
> 配套：实施计划见 `docs/superpowers/plans/`（后续 writing-plans 产出）。

## 1. 背景与目标

`cli.py` 已有全部原子能力（single/pipeline/worker/split/merge/qc/stats/demo-video/status），但三件事它做不了或不该做：

1. **环境自举**：venv 是 cli.py 运行的前提，Python 无法创建自己要跑的环境（鸡生蛋）
2. **输入预处理与编排串联**：目录展开、同 stem 碰撞预检、跑完聚合报告
3. **演示外壳**：一键切换 gocp 模式、面试现场一条命令讲完整故事

**目标**：`scripts/blur.sh` 一个脚本补齐这三件事；**不重新实现任何业务逻辑**——所有媒体/控制面能力一律透传 `./venv/bin/faceblur-lite`，单一事实源保持在 cli.py。

**架构选择（已裁决：薄委托层）**：备选有单文件厚脚本（shell 里重写输出命名/碰撞语义，会和 Python 侧漂移——终审刚修过这类漂移，不再造）与 cli.py 加编排子命令（环境引导仍需 shell 垫片，demo 多进程编排放 Python 反而别扭）。

## 2. 用途定位（已裁决：双模式）

- `blur.sh run <inputs...>`：日常糊真实视频——薄、稳、少花活
- `blur.sh demo`：一条命令讲完整故事（合成视频 → single vs pipeline → stats → qc → 可选 gocp）

## 3. 命令面

```bash
scripts/blur.sh run <inputs...> [选项]
scripts/blur.sh demo [选项]

# run 选项：
#   --outdir DIR      输出目录，默认 outputs/
#   --workers N       Worker 数，默认 2（仅 pipeline 模式生效）
#   --segment S       切片秒数，默认 30（仅 pipeline 模式生效）
#   --mode M          auto（默认）| single | pipeline
#   --go              gocp 控制面模式（隐含 pipeline，见 §5）
# demo 选项：
#   --outdir DIR      默认 outputs/demo
#   --workers N       默认 2
#   --go              增加 gocp 模式环节
```

`--help` 打印用法；无子命令/未知选项报错 exit 2。

## 4. run 子命令流程

```
bootstrap（§6）→ 输入展开 → 碰撞预检 → 引擎选择 → 执行 → 聚合报告
```

**输入展开**：
- 参数为文件：直接收（不校验扩展名——显式指定即尊重）
- 参数为目录：glob 视频扩展名白名单 `mp4 mov mkv avi m4v`（不区分大小写），按字母序排序；**默认不递归**
- 参数不存在/不可读：收集**全部**坏路径后一次性报错 exit 2（不挤牙膏）
- 展开结果为空（目录里没视频）→ 报错 exit 2

**同 stem 碰撞预检**：对展开后的全部输入按文件名 stem 分组（`sort | uniq -d` 实现，兼容 bash 3.2，不用关联数组）；任一组 >1 即碰撞 → **一次性列出所有碰撞组** + 处理建议（分目录多次 run）→ exit 2。语义与 run_batch 的"显式失败、不静默重命名"一致，但报得更早更友好（提交前而非跑到一半）。

**引擎选择**（--mode 覆盖；auto=按输入数）：
- 1 个输入 → `single`：脚本拼输出路径 `<outdir>/<stem>_blurred.mp4`（与 run_batch 命名一致），透传 `faceblur-lite single in out`
- ≥2 个输入 → `pipeline`：透传 `faceblur-lite pipeline <inputs> --outdir <outdir> --workdir <outdir>/work --workers N --segment S`
- `--mode single` + 多输入：逐个串行跑 single（语义=强制单进程引擎），逐个报告
- single 模式下 `--workers/--segment` 被忽略时打印一行提示（不静默吞参数）

**输出目录与仓库隔离（硬保证：生成结果不进远端）**：
- 默认 `--outdir outputs/`——`.gitignore` 自 Task 0 起整目录忽略（`outputs/`、`work-*/`）；workdir `<outdir>/work`、demo 的 `demo_src/` 均在其下，默认路径生成的任何文件天然不进 git
- 自定义 `--outdir` 保险：脚本对解析后的输出目录跑 `git check-ignore`——在仓库外 → 天然安全，无操作；在仓库内且未被忽略 → **自动追加一行到 `.gitignore`**（带 `# blur.sh 自动追加：输出目录` 注记）并打印提示——"生成结果不上传远端"是脚本保证，不靠用户记性

**聚合报告**：
- pipeline 模式：跑完自动 `faceblur-lite stats <workdir>/pipeline.db` + 每个输出一行 qc 摘要
- single 模式：kernel JSON 报告摘要（帧数/检测数/track 数/人脸时间段数/耗时）+ qc 一次
- qc 摘要**统一透传 `qc` 子命令**（而非解析 pipeline 的 JSON 输出）：single/pipeline 两种模式共用同一个报告器，shell 不做 JSON 解析；代价是对已 qc 过的输出多一次 1fps 抽帧复检（秒级，可接受）。行格式：`✅ <name> qc 通过（抽 N 帧，0 违规）` / `❌ <name> qc 违规（K 处）`

## 5. --go（gocp 控制面模式）

- `--go` **隐含 pipeline 引擎**（auto 下给 --go，即使单输入也走 pipeline——用户显式要控制面就是要走控制面）；若同时显式 `--mode single` → 报错 exit 2（矛盾组合）
- 流程：gocp 二进制缓存于 `/tmp/gocp-blur-sh`（源码 mtime 比二进制新则重建，否则复用）→ 找空闲端口起后台 gocp（db 放 workdir）→ `trap EXIT` 保证杀进程 + 清理 → pipeline `--cp-url` 透传
- go 工具链缺失：因是用户显式要求，**报错退出不静默降级**（提示 brew install go）

## 6. bootstrap（已裁决：检查+自动修复）

边界：**管仓库内状态，不管系统状态**。

| 检查 | 缺失时行为 |
|---|---|
| ffmpeg/ffprobe 不在 PATH | 报错 + `brew install ffmpeg` 指引，exit 2（不自动装） |
| `venv/bin/faceblur-lite` 不存在 | 选取 python≥3.10（python3→3.13…3.10 逐候选）建 venv → pip 升级 → pip install -e ".[dev]"（详见 docs/bugs/008） |
| `models/*.onnx` 或 `assets/*.jpg` 缺 | 自动跑 `bash scripts/download_assets.sh` |

幂等：环境全齐时只做 `which` + `test -f`，秒过进正题。新 clone 一条命令跑通。

## 7. demo 子命令流程（叙事线）

每步打印 `[step k/n] <标题>` + 实测耗时；**对比结论用当场数字，不写死**（README 诚实教训：本机 pipeline 无净加速就如实展示摊销）。任一步骤失败即停，明确指出卡在哪一步。

1. bootstrap
2. 合成两段 30s 视频（move + occlude）到 `<outdir>/demo_src/`
3. `single` 处理 occlude 段（计时）
4. `pipeline` 批量处理两段（计时）+ `stats`
5. qc 汇总（两段输出各一行 ✅/❌）
6. （`--go`）gocp 模式重跑批量 + stats
7. 收尾打印探索指引（kill -9 租约恢复怎么玩、--segment 调优、开放细化项指路）

## 8. 错误处理与退出码（已裁决：qc_failed=1）

`set -euo pipefail`；bash 3.2 兼容（macOS 自带，不用关联数组等 4.x 特性）。

| 码 | 含义 |
|---|---|
| 0 | 全部 done 且 qc 全过 |
| 1 | 存在 failed 或 **qc_failed**（漏检是严肃事件，脚本层面不轻放） |
| 2 | 环境/输入错误（ffmpeg/venv 无法自举、坏路径、空目录、stem 碰撞、矛盾参数组合） |

## 9. 测试策略

- **pytest 最小冒烟**（`tests/test_blur_sh.py`，`requires_assets` 标记）：
  1. 合成两段 2s 不同 stem 视频 → `subprocess blur.sh run <两视频> --outdir --workers 2 --segment 2` → 断言 exit 0、两输出存在
  2. 同 stem 两目录 → 断言 exit 2 且报"碰撞"
- demo 不进自动化（本质是人工演示叙事）
- 人工验收：`blur.sh demo` 全流程 + `blur.sh demo --go` + 新 clone 场景（删 venv/模型后 run）

## 10. 非目标

- 不重写任何业务逻辑（输出命名/碰撞/qc 判定全部以 cli.py 为准）
- 不自动安装系统依赖（ffmpeg/go）
- 默认不递归子目录（将来需要再加 -r）
- 不考虑 Windows/非 bash 环境
- 不做断点续跑/幂等提交（开放细化项 12 的地盘）
