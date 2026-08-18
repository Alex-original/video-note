#!/bin/bash
# 双击启动「视频转笔记」小工具（Gradio 本地界面，自动打开浏览器）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
PYTHON="$SCRIPT_DIR/../../venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "未找到虚拟环境: $PYTHON"
    echo "请先确认 venv 存在。按回车退出。"
    read -r _
    exit 1
fi

echo "正在启动「视频转笔记」..."
exec "$PYTHON" app.py
