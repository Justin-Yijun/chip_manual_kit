# 形态 B：把 chip-manual-kit 作为常驻 HTTP(streamable-http) MCP 服务启动（Windows）。
# 形态 A（本地 stdio）无需此脚本——由客户端按 examples/mcp.json.template 自行拉起。
# 用法：
#   $env:CHIP_KB_PATH="..."; $env:CHIP_VECTORS_PATH="..."; $env:CHIP_EMBED_MODEL="..."
#   .\scripts\serve_http.ps1 -Host 0.0.0.0 -Port 8000
#   $env:CHIP_PYTHON="..\mineru-work\.venv\Scripts\python.exe"; .\scripts\serve_http.ps1
param(
    [string]$Host = $(if ($env:CHIP_MCP_HOST) { $env:CHIP_MCP_HOST } else { "0.0.0.0" }),
    [int]$Port = $(if ($env:CHIP_MCP_PORT) { [int]$env:CHIP_MCP_PORT } else { 8000 })
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here

if (-not $env:HF_HUB_OFFLINE) { $env:HF_HUB_OFFLINE = "1" }
if (-not $env:TRANSFORMERS_OFFLINE) { $env:TRANSFORMERS_OFFLINE = "1" }

$py = $env:CHIP_PYTHON
if (-not $py) { $py = "python" }

& $py "$repo\server\chip_server.py" --http --host $Host --port $Port
exit $LASTEXITCODE
