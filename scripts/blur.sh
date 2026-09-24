#!/usr/bin/env bash
# blur.sh —— face-blur-lite 本地运行脚本（薄委托层）
# spec：docs/superpowers/specs/2026-09-24-blur-sh-design.md
# 退出码：0=全 done 且 qc 全过；1=存在 failed/qc_failed；2=环境/输入/参数错误
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

FACEBLUR=./venv/bin/faceblur-lite
PY=./venv/bin/python

usage() { cat <<'EOF'
用法：
  scripts/blur.sh run <inputs...> [选项]   视频脱敏（文件/目录混合；≥2 个自动批量）
  scripts/blur.sh demo [选项]              一条命令演示完整故事

run 选项：
  --outdir DIR    输出目录（默认 outputs/；生成结果不进 git）
  --workers N     Worker 数，默认 2（仅 pipeline 模式）
  --segment S     切片秒数，默认 30（仅 pipeline 模式）
  --mode M        auto（默认）| single | pipeline
  --go            gocp 控制面模式（隐含 pipeline；需 go 工具链）
demo 选项：
  --outdir DIR    默认 outputs/demo
  --workers N     默认 2
  --go            增加 gocp 模式环节
EOF
}

die2() { echo "错误：$*" >&2; exit 2; }

# ---------- bootstrap（spec §6：管仓库内状态，不管系统状态） ----------
# 选版：项目 requires-python >=3.10，且 pyproject-only（PEP 660 可编辑安装需 pip>=21.3）。
# 默认 python3 可能过老（BUG-008）：按 python3→python3.13→…→python3.10 取首个 ≥3.10。
pick_python() {
  local p v
  for p in python3 python3.13 python3.12 python3.11 python3.10; do
    command -v "$p" >/dev/null 2>&1 || continue
    v=$("$p" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null) || continue
    [ "$v" -ge 310 ] 2>/dev/null || continue
    echo "$p"
    return 0
  done
  return 1
}

ensure_env() {
  command -v ffmpeg  >/dev/null 2>&1 || die2 "缺少 ffmpeg：brew install ffmpeg"
  command -v ffprobe >/dev/null 2>&1 || die2 "缺少 ffprobe（随 ffmpeg 安装）：brew install ffmpeg"
  if [ ! -x "$FACEBLUR" ]; then
    echo "[bootstrap] venv 缺失，创建并安装依赖..."
    local PYBIN
    PYBIN=$(pick_python) || die2 "未找到 python ≥3.10（项目 requires-python >=3.10；brew install python@3.11）"
    "$PYBIN" -m venv venv || die2 "venv 创建失败"
    ./venv/bin/pip install --upgrade pip >/dev/null || die2 "pip 升级失败"
    ./venv/bin/pip install -e ".[dev]" || die2 "依赖安装失败（pip install -e .[dev]）"
  fi
  if [ ! -f models/face_detection_yunet_2023mar.onnx ] || [ ! -f assets/lena.jpg ]; then
    echo "[bootstrap] 模型/测试资产缺失，下载..."
    bash scripts/download_assets.sh || die2 "资产下载失败（网络？）"
  fi
}

# ---------- 输出隔离（spec §4：硬保证，先于输入校验） ----------
ensure_outdir_ignored() {  # $1=目录（须已存在）
  local od_abs
  od_abs=$(cd "$1" && pwd -P)
  [ "$od_abs" = "$REPO" ] && die2 "--outdir 不能是仓库根（输出会污染仓库且无法安全忽略）"
  case "$od_abs" in
    "$REPO"/*)
      if ! git check-ignore -q "$od_abs"; then
        printf '\n# blur.sh 自动追加：输出目录\n%s/\n' "${od_abs#"$REPO"/}" >> .gitignore
        echo "[隔离] 已将 ${od_abs#"$REPO"/}/ 自动追加到 .gitignore（生成结果不进仓库）"
      fi ;;
  esac
}

# ---------- 输入展开与碰撞预检（spec §4） ----------
VIDEO_EXTS="mp4 mov mkv avi m4v"
INPUTS=()

stem_of() { local b; b=$(basename "$1"); echo "${b%.*}"; }

expand_inputs() {
  local p ext f bad=() found
  for p in "$@"; do
    if [ -f "$p" ]; then
      INPUTS+=("$p")
    elif [ -d "$p" ]; then
      found=0
      for ext in $VIDEO_EXTS; do
        while IFS= read -r f; do INPUTS+=("$f"); found=1; done \
          < <(find "$p" -maxdepth 1 -iname "*.$ext" | sort)
      done
      if [ "$found" -eq 0 ]; then bad+=("${p}（目录内无视频）"); fi
    else
      bad+=("${p}（不存在或不可读）")
    fi
  done
  if [ "${#bad[@]}" -gt 0 ]; then
    echo "输入问题：" >&2
    printf '  %s\n' "${bad[@]}" >&2
    exit 2
  fi
  if [ "${#INPUTS[@]}" -eq 0 ]; then die2 "没有可处理的输入"; fi
}

check_collisions() {
  local dups s p st
  dups=$(for p in "${INPUTS[@]}"; do stem_of "$p"; done | sort | uniq -d)
  if [ -n "$dups" ]; then
    echo "同 stem 输出碰撞（不静默重命名）：" >&2
    for s in $dups; do
      echo "  stem「${s}」：" >&2
      for p in "${INPUTS[@]}"; do
        st=$(stem_of "$p")
        if [ "$st" = "$s" ]; then echo "    $p" >&2; fi
      done
    done
    echo "建议：分目录分批运行，或先重命名使其唯一" >&2
    exit 2
  fi
}

# ---------- 聚合报告（spec §4：qc 统一透传 qc 子命令，shell 不解析管线 JSON） ----------
QC_BAD=0

qc_one() {  # $1=输出文件
  local tmp rc summary sampled viol
  if [ ! -f "$1" ]; then
    echo "  ❌ $(basename "$1") 未生成（该视频 failed）"
    QC_BAD=1
    return 0
  fi
  tmp=$(mktemp)
  set +e
  "$FACEBLUR" qc "$1" >"$tmp" 2>/dev/null
  rc=$?
  set -e
  summary=$("$PY" -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["sampled"],len(d["violations"]))' "$tmp")
  sampled=${summary% *}; viol=${summary#* }
  rm -f "$tmp"
  if [ "$rc" -eq 0 ]; then
    echo "  ✅ $(basename "$1") qc 通过（抽 ${sampled} 帧，0 违规）"
  else
    echo "  ❌ $(basename "$1") qc 违规（抽 ${sampled} 帧，${viol} 处）"
    QC_BAD=1
  fi
}

report_pipeline() {  # $1=workdir；INPUTS/OUTDIR 来自 cmd_run
  echo ""
  echo "── 批量指标 ──"
  local db="$1/pipeline.db"
  if [ ! -f "$db" ] && [ -f "$1/gocp.db" ]; then db="$1/gocp.db"; fi
  if [ -f "$db" ]; then
    "$FACEBLUR" stats "$db" || true
  else
    echo "（无 pipeline.db——批前提交即失败）"
  fi
  echo ""
  echo "── 输出质检 ──"
  local p
  for p in "${INPUTS[@]}"; do
    qc_one "$OUTDIR/$(stem_of "$p")_blurred.mp4"
  done
}

# ---------- gocp（spec §5） ----------
GOCP_PID=""

stop_gocp() {
  if [ -n "$GOCP_PID" ]; then
    kill "$GOCP_PID" 2>/dev/null || true
    GOCP_PID=""
  fi
}

start_gocp() {  # $1=workdir；就绪后设置全局 GO_URL（勿用 $() 调用——子 shell 会丢 GOCP_PID 且 die2 只退出子 shell）
  command -v go >/dev/null 2>&1 || die2 "--go 需要 go 工具链（≥1.25）：brew install go"
  local bin=/tmp/gocp-blur-sh
  if [ ! -x "$bin" ] || [ gocp/main.go -nt "$bin" ] || [ gocp/go.mod -nt "$bin" ]; then
    echo "[go] 构建 gocp..." >&2
    (cd gocp && go build -o "$bin" .)
  fi
  mkdir -p "$1"
  local port i
  port=$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
  "$bin" "$1/gocp.db" "127.0.0.1:$port" >/dev/null 2>&1 &
  GOCP_PID=$!
  for i in $(seq 1 50); do
    if "$PY" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:$port/videos',timeout=0.2)" 2>/dev/null; then
      GO_URL="http://127.0.0.1:$port"
      return 0
    fi
    sleep 0.1
  done
  stop_gocp
  die2 "gocp 启动超时"
}

# ---------- run ----------
cmd_run() {
  local OUTDIR=outputs WORKERS=2 SEGMENT=30 MODE=auto GO=""
  local W_SET="" S_SET="" raw=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --outdir)  [ $# -ge 2 ] && [ -n "$2" ] || die2 "--outdir 需要一个非空值"; OUTDIR=$2;  shift 2 ;;
      --workers) [ $# -ge 2 ] && [ -n "$2" ] || die2 "--workers 需要一个非空值"; WORKERS=$2; W_SET=1; shift 2 ;;
      --segment) [ $# -ge 2 ] && [ -n "$2" ] || die2 "--segment 需要一个非空值"; SEGMENT=$2; S_SET=1; shift 2 ;;
      --mode)    [ $# -ge 2 ] && [ -n "$2" ] || die2 "--mode 需要一个非空值"; MODE=$2;    shift 2 ;;
      --go)      GO=1;       shift ;;
      -h|--help) usage; return 0 ;;
      --*) die2 "未知选项：$1" ;;
      *)   raw+=("$1"); shift ;;
    esac
  done
  if [ "${#raw[@]}" -eq 0 ]; then usage >&2; die2 "run 至少需要一个输入"; fi
  case "$MODE" in
    auto|single|pipeline) ;;
    *) die2 "--mode 仅支持 auto|single|pipeline" ;;
  esac
  if [ -n "$GO" ] && [ "$MODE" = "single" ]; then
    die2 "--go 与 --mode single 矛盾（--go 隐含 pipeline）"
  fi

  ensure_env
  mkdir -p "$OUTDIR"
  ensure_outdir_ignored "$OUTDIR"
  expand_inputs "${raw[@]}"
  check_collisions

  local WORKDIR="$OUTDIR/work" rc=0 n=${#INPUTS[@]} eng=$MODE p out
  if [ "$eng" = "auto" ]; then
    if [ -n "$GO" ] || [ "$n" -ge 2 ]; then eng=pipeline; else eng=single; fi
  fi
  if [ "$eng" = "single" ] && { [ -n "$W_SET" ] || [ -n "$S_SET" ]; }; then
    echo "提示：single 模式忽略 --workers/--segment"
  fi

  if [ "$eng" = "pipeline" ]; then
    local GO_URL=""
    if [ -n "$GO" ]; then
      start_gocp "$WORKDIR"
      trap stop_gocp EXIT INT TERM
      echo "[go] gocp 控制面：${GO_URL}"
    fi
    set +e
    if [ -n "$GO_URL" ]; then
      "$FACEBLUR" pipeline "${INPUTS[@]}" --outdir "$OUTDIR" \
        --workdir "$WORKDIR" --workers "$WORKERS" --segment "$SEGMENT" \
        --cp-url "$GO_URL"
    else
      "$FACEBLUR" pipeline "${INPUTS[@]}" --outdir "$OUTDIR" \
        --workdir "$WORKDIR" --workers "$WORKERS" --segment "$SEGMENT"
    fi
    rc=$?
    set -e
    report_pipeline "$WORKDIR"
    stop_gocp
    trap - EXIT INT TERM
  else
    echo "── single 模式（$n 个输入，串行）──"
    for p in "${INPUTS[@]}"; do
      out="$OUTDIR/$(stem_of "$p")_blurred.mp4"
      local tmp info
      tmp=$(mktemp)
      set +e
      "$FACEBLUR" single "$p" "$out" >"$tmp" 2>/dev/null
      rc=$?
      set -e
      if [ "$rc" -eq 0 ]; then
        info=$("$PY" -c 'import json,sys;d=json.load(open(sys.argv[1]));print("{} 帧，检测 {} 次，track {} 个，耗时 {:.1f}s".format(d["frames"],d["detections"],d["track_count"],d["wall_s"]))' "$tmp")
        echo "  🎬 $(basename "$out")：$info"
        qc_one "$out"
      else
        echo "  ❌ $p 处理失败"
        break
      fi
      rm -f "$tmp"
    done
  fi
  if [ "$QC_BAD" -ne 0 ]; then rc=1; fi
  return "$rc"
}

# ---------- demo ----------
cmd_demo() {
  local OUTDIR=outputs/demo WORKERS=2 GO=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --outdir)  [ $# -ge 2 ] && [ -n "$2" ] || die2 "--outdir 需要一个非空值"; OUTDIR=$2;  shift 2 ;;
      --workers) [ $# -ge 2 ] && [ -n "$2" ] || die2 "--workers 需要一个非空值"; WORKERS=$2; shift 2 ;;
      --go)      GO=1;       shift ;;
      -h|--help) usage; return 0 ;;
      --*)       die2 "未知选项：$1" ;;
      *)         die2 "demo 不接受该参数：$1" ;;
    esac
  done

  ensure_env
  mkdir -p "$OUTDIR/demo_src"
  ensure_outdir_ignored "$OUTDIR"

  local total=5 t0
  if [ -n "$GO" ]; then total=6; fi

  echo "[1/$total] 合成演示视频（30s move + 30s occlude）"
  t0=$(date +%s)
  "$FACEBLUR" demo-video "$OUTDIR/demo_src/move.mp4" --duration 30 --scenario move >/dev/null
  "$FACEBLUR" demo-video "$OUTDIR/demo_src/occlude.mp4" --duration 30 --scenario occlude >/dev/null
  echo "  完成（$(( $(date +%s) - t0 ))s）"

  echo "[2/$total] single 单进程处理 occlude.mp4"
  t0=$(date +%s)
  "$FACEBLUR" single "$OUTDIR/demo_src/occlude.mp4" \
    "$OUTDIR/single_occlude_blurred.mp4" >/dev/null
  echo "  耗时 $(( $(date +%s) - t0 ))s"

  echo "[3/$total] pipeline 批量处理两段（共享 Worker 池 ×${WORKERS}）"
  t0=$(date +%s)
  set +e
  "$FACEBLUR" pipeline "$OUTDIR/demo_src/move.mp4" "$OUTDIR/demo_src/occlude.mp4" \
    --outdir "$OUTDIR" --workdir "$OUTDIR/work" --workers "$WORKERS" --segment 30 >/dev/null
  set -e
  echo "  耗时 $(( $(date +%s) - t0 ))s（对比结论看当场数字：本机单进程若已吃满 CPU，分片无净加速、收益在批量摊销）"

  echo "[4/$total] 观测与质检"
  "$FACEBLUR" stats "$OUTDIR/work/pipeline.db" || true
  echo ""
  qc_one "$OUTDIR/single_occlude_blurred.mp4"
  qc_one "$OUTDIR/move_blurred.mp4"
  qc_one "$OUTDIR/occlude_blurred.mp4"

  local step=5
  if [ -n "$GO" ]; then
    echo "[5/6] gocp 控制面模式重跑批量（业界形态：Go 控制面 + Python Worker）"
    t0=$(date +%s)
    local GO_URL=""
    start_gocp "$OUTDIR/work-go"
    trap stop_gocp EXIT INT TERM
    echo "[go] gocp 控制面：${GO_URL}"
    set +e
    "$FACEBLUR" pipeline "$OUTDIR/demo_src/move.mp4" "$OUTDIR/demo_src/occlude.mp4" \
      --outdir "$OUTDIR/go-mode" --workdir "$OUTDIR/work-go" \
      --workers "$WORKERS" --segment 30 --cp-url "$GO_URL" >/dev/null
    set -e
    "$FACEBLUR" stats "$OUTDIR/work-go/gocp.db" || true
    stop_gocp
    trap - EXIT INT TERM
    echo "  耗时 $(( $(date +%s) - t0 ))s"
    step=6
  fi

  echo "[$step/$total] 探索指引"
  cat <<'TIP'
  · 换真实视频：scripts/blur.sh run 你的视频.mp4（或整个目录）
  · 容错演示：pipeline 跑批时 kill -9 一个 worker 进程，观察 lease 回收重派（docs/design.md §5）
  · 调优：--segment 切片粒度 / --workers 并行度
  · 深入：docs/design.md（四大视图）｜docs/platform-mapping.md（业界平台映射）
TIP
}

main() {
  if [ $# -eq 0 ]; then usage >&2; exit 2; fi
  case "$1" in
    run)      shift; cmd_run "$@" ;;
    demo)     shift; cmd_demo "$@" ;;
    -h|--help) usage ;;
    *)        usage >&2; die2 "未知子命令：$1" ;;
  esac
}
main "$@"
