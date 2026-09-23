#!/usr/bin/env bash
# 下载模型与测试资产。产物已被 .gitignore 排除。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/models" "$ROOT/assets"

curl -L --fail -o "$ROOT/models/face_detection_yunet_2023mar.onnx" \
  "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

curl -L --fail -o "$ROOT/assets/lena.jpg" \
  "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/lena.jpg"

ls -la "$ROOT/models" "$ROOT/assets"
