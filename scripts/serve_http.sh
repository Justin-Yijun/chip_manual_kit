#!/usr/bin/env bash
# 形态 B：把 chip-manual-kit 作为常驻 HTTP(streamable-http) MCP 服务启动（供远程/多客户端连接）。
# 形态 A（本地 stdio）无需此脚本——由客户端按 examples/mcp.json.template 自行拉起。
#
# 用法：
#   CHIP_KB_PATH=... CHIP_VECTORS_PATH=... CHIP_EMBED_MODEL=... ./scripts/serve_http.sh [--host 0.0.0.0] [--port 8000]
#   CHIP_PYTHON=/path/to/.venv/bin/python ./scripts/serve_http.sh --host 0.0.0.0 --port 8000
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"

export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

py="${CHIP_PYTHON:-python}"
# 对外服务默认监听 0.0.0.0；仅本机可用则传 --host 127.0.0.1
exec "$py" "$repo/server/chip_server.py" --http --host "${CHIP_MCP_HOST:-0.0.0.0}" --port "${CHIP_MCP_PORT:-8000}" "$@"
