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
ensure_env() {
  command -v ffmpeg  >/dev/null 2>&1 || die2 "缺少 ffmpeg：brew install ffmpeg"
  command -v ffprobe >/dev/null 2>&1 || die2 "缺少 ffprobe（随 ffmpeg 安装）：brew install ffmpeg"
  if [ ! -x "$FACEBLUR" ]; then
    echo "[bootstrap] venv 缺失，创建并安装依赖..."
    command -v python3 >/dev/null 2>&1 || die2 "缺少 python3（brew install python 或装 Xcode CLT）"
    python3 -m venv venv || die2 "venv 创建失败"
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
  if [ -f "$1/pipeline.db" ]; then
    "$FACEBLUR" stats "$1/pipeline.db" || true
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
  if [ -n "$GO" ]; then die2 "--go 尚未实现（Task 3）"; fi

  ensure_env
  mkdir -p "$OUTDIR"
  ensure_outdir_ignored "$OUTDIR"
  expand_inputs "${raw[@]}"
  check_collisions

  local WORKDIR="$OUTDIR/work" rc=0 n=${#INPUTS[@]} eng=$MODE p out
  if [ "$eng" = "auto" ]; then
    if [ "$n" -ge 2 ]; then eng=pipeline; else eng=single; fi
  fi
  if [ "$eng" = "single" ] && { [ -n "$W_SET" ] || [ -n "$S_SET" ]; }; then
    echo "提示：single 模式忽略 --workers/--segment"
  fi

  if [ "$eng" = "pipeline" ]; then
    set +e
    "$FACEBLUR" pipeline "${INPUTS[@]}" --outdir "$OUTDIR" \
      --workdir "$WORKDIR" --workers "$WORKERS" --segment "$SEGMENT"
    rc=$?
    set -e
    report_pipeline "$WORKDIR"
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

main() {
  if [ $# -eq 0 ]; then usage >&2; exit 2; fi
  case "$1" in
    run)      shift; cmd_run "$@" ;;
    demo)     die2 "demo 尚未实现（Task 4）" ;;
    -h|--help) usage ;;
    *)        usage >&2; die2 "未知子命令：$1" ;;
  esac
}
main "$@"
