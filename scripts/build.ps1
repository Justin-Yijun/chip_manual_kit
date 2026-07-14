# 一键构建（Windows / PowerShell）。转发参数给 scripts/build_kb.py。
# 用法：
#   .\scripts\build.ps1 --mineru-out ..\mineru-work\out_acme ..\mineru-work\out_other
#   $env:CHIP_PYTHON="..\mineru-work\.venv\Scripts\python.exe"; .\scripts\build.ps1 --mineru-out ...
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# 离线/受限网络：嵌入模型走本地路径即可；解析阶段可设 MINERU_MODEL_SOURCE=modelscope
if (-not $env:HF_HUB_OFFLINE) { $env:HF_HUB_OFFLINE = "1" }
if (-not $env:TRANSFORMERS_OFFLINE) { $env:TRANSFORMERS_OFFLINE = "1" }

$py = $env:CHIP_PYTHON
if (-not $py) { $py = "python" }

& $py "$here\build_kb.py" @args
exit $LASTEXITCODE
