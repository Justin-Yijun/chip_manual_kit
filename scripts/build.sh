#!/usr/bin/env bash
# 一键构建（Linux/macOS）。转发参数给 scripts/build_kb.py。
# 用法：
#   ./scripts/build.sh --mineru-out ../mineru-work/out_acme ../mineru-work/out_other
#   CHIP_PYTHON=../mineru-work/.venv/bin/python ./scripts/build.sh --mineru-out ...
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 离线：嵌入模型用本地路径；解析阶段可设 MINERU_MODEL_SOURCE=modelscope
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

py="${CHIP_PYTHON:-python}"
exec "$py" "$here/build_kb.py" "$@"
