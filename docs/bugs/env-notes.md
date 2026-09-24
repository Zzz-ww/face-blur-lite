# 环境类问题记录（非代码 bug）

执行环境（macOS, darwin 25.6.0）踩过的坑与解法，复现/迁移时参考：

| 问题 | 现象 | 解法 |
|---|---|---|
| 系统 Python 太老 | `python3`=3.8.5，项目要求 ≥3.10；`/opt/homebrew/bin/python3*` 不存在 | 用 `~/.local/bin/python3.11`（3.11.15，uv 管理）建 venv；所有命令走 `./venv/bin/python` |
| GitHub 网络不通 | `github.com` / `raw.githubusercontent.com` 连接超时（curl 56/16；pypi 正常） | 模型与测试资产从本地老项目拷贝：`models/face_detection_yunet_2023mar.onnx`（232,589 B）+ `assets/lena.jpg`（91,814 B），均 gitignore 不入库 |
| HTTPS push 认证坏 | `could not read Username...: Device not configured`；无 gh CLI | SSH 远端 `git@github.com:Zzz-ww/face-blur-lite.git`（`ssh -T` 验证可用） |
| opencv 5.0 stderr 告警 | 首次 `FaceDetectorYN.create` 打一行 "Targets are not supported by the new graph engine" | opencv 库自身行为，非缺陷，不绕过；每个 spawn 的 Worker 进程各打一次属正常 |
| 沙箱 /tmp I/O 极慢 | ffmpeg 写 `/tmp` 比项目本地路径慢约 100× | 诊断/合成视频脚本放项目目录内跑 |
| pip 偶发失败 | Task 0 安装 3 次才成 | `--no-cache-dir` 重试即过（瞬时网络问题） |
| go 工具链过老 + 模块代理不通 | 系统 go 1.19.1 < 计划要求 ≥1.22；`proxy.golang.org` 连接失败（curl 000） | 升级 /usr/local/go 至 1.27.1；`go env -w GOPROXY=https://goproxy.cn,direct`（goproxy.cn 可达）；modernc.org/sqlite v1.59.0 及依赖已入本地模块缓存 |
