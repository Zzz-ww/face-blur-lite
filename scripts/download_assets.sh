#!/usr/bin/env bash
# 下载模型与测试资产（含 sha256 校验）。产物已被 .gitignore 排除。
# det_500m（SCRFD/buffalo_s）默认走 hf-mirror（大陆可达，慢 ~15KB/s——
# curl -C - 断点续传是关键）；原址备选：
#   https://huggingface.co/immich-app/buffalo_s/resolve/main/detection/model.onnx
# 可用 FACEBLUR_SCRFD_URL 覆盖。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/models" "$ROOT/assets"

fetch() {  # $1=目标路径 $2=sha256 $3=URL
  local dst sum
  dst="$1"
  if [ -f "$dst" ] && [ "$(shasum -a 256 "$dst" | cut -d' ' -f1)" = "$2" ]; then
    echo "✓ 已存在且校验通过：$dst"
    return 0
  fi
  curl -L --fail -C - -o "$dst" "$3"
  sum=$(shasum -a 256 "$dst" | cut -d' ' -f1)
  if [ "$sum" != "$2" ]; then
    echo "sha256 校验失败：$dst（$sum ≠ $2）" >&2
    exit 1
  fi
}

fetch "$ROOT/models/face_detection_yunet_2023mar.onnx" \
  "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4" \
  "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

fetch "$ROOT/models/det_500m.onnx" \
  "5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a" \
  "${FACEBLUR_SCRFD_URL:-https://hf-mirror.com/immich-app/buffalo_s/resolve/main/detection/model.onnx}"

fetch "$ROOT/assets/lena.jpg" \
  "7de7ed51a1594fff247f4cae2301eceacf5313d6011e37b4a4c8733f7bb72c07" \
  "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/lena.jpg"

ls -la "$ROOT/models" "$ROOT/assets"
