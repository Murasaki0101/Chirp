#!/usr/bin/env bash
# 打包「吱一声」为 macOS .app —— 分架构（Intel / Apple Silicon 各一个）
#
# 为什么分架构而不是 universal2：
#   本地 Laya 推理依赖 torch，而 torch 没有官方 universal2 wheel（只分 arm64/x86_64），
#   onnxruntime 同样分架构；laya 也不支持 gguf/llama.cpp 后端。所以含本地推理的
#   universal2 单包在当前 Python ML 生态打不出来。分架构两个 .app 是最稳妥方案。
#
# PyInstaller 只能打「当前机器架构」的 .app。要集齐两个架构：
#   - 在 Apple Silicon Mac 上跑一次 → 吱一声-arm64.app
#   - 在 Intel Mac 上跑一次        → 吱一声-x86_64.app
#   （或用 CI 的双架构 runner 各跑一次）
#
# 用法：
#   ./build_app.sh           # 打当前架构 .app（若装了 laya 则含本地推理）
#   ./build_app.sh --cloud   # 只打云端 Jev 版（不含 torch，包最小）
set -euo pipefail
cd "$(dirname "$0")"

NAME="吱一声"
BUNDLE_ID="com.radar.dingtalk-jev-radar"
MODE="${1:-full}"
HOST_ARCH=$(uname -m)
ARCH="${2:-$HOST_ARCH}"        # 第二参数指定目标架构（arm64|x86_64），默认当前架构

# 选 Python：目标架构==当前架构用 .venv；否则用 uv 建对应架构 venv（Rosetta 交叉打包）
if [ "$ARCH" = "$HOST_ARCH" ]; then
  PY=".venv/bin/python"
  [ -x "$PY" ] || PY="python3"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -q pyinstaller
  else
    "$PY" -m pip install -q pyinstaller
  fi
else
  VENV=".venv-${ARCH}"
  if [ "$ARCH" = "x86_64" ] && [ -x ".venv-x86/bin/python" ]; then
    VENV=".venv-x86"
  fi
  if [ ! -x "$VENV/bin/python" ]; then
    echo "==> 交叉打包 $ARCH：用 uv 建 $VENV（需已装 Rosetta）"
    UPY=$(uv python list 2>/dev/null | grep -i "$ARCH" | head -1 | awk '{print $NF}')
    [ -n "$UPY" ] || { echo "✗ 找不到 uv 的 $ARCH Python，先跑: uv python install 3.11"; exit 1; }
    uv venv "$VENV" --python "$UPY"
    uv pip install --python "$VENV/bin/python" -q PySide6 pyinstaller
  fi
  PY="$VENV/bin/python"
fi

echo "==> 目标架构: $ARCH   模式: $MODE   Python: $PY"

COLLECT=""
EXCLUDE=""
if [ "$MODE" = "--cloud" ]; then
  echo "==> 云端 Jev 模式：排除本地推理依赖（torch/laya/transformers），包最小"
  EXCLUDE="--exclude-module torch --exclude-module laya --exclude-module transformers --exclude-module modelscope --exclude-module tokenizers --exclude-module safetensors --exclude-module sympy --exclude-module networkx --exclude-module tilelang"
elif "$PY" -c "import laya" 2>/dev/null; then
  echo "==> 含本地 Laya 推理（laya + torch + transformers）"
  COLLECT="--collect-all laya --collect-all transformers --collect-all torch"
else
  echo "==> 未装 laya，按云端模式打包"
  EXCLUDE="--exclude-module torch --exclude-module laya --exclude-module transformers"
fi

"$PY" -m PyInstaller \
  --noconfirm --clean --windowed \
  --name "${NAME}-${ARCH}" \
  --target-arch "$ARCH" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --icon "src/chirp/assets/icon.icns" \
  --add-data "src/chirp/assets/icon.png:assets" \
  $COLLECT $EXCLUDE \
  src/chirp/app.py

echo ""
echo "✅ 完成：dist/${NAME}-${ARCH}.app"
echo "   另一个架构请在对应 CPU 的 Mac 上再跑一次本脚本。"
echo "   目标机需装钉钉官方 CLI：npm i -g dingtalk-workspace-cli && dws auth login"
echo "   首次用本地 Laya 会自动从 ModelScope 下模型（~650M）。"
