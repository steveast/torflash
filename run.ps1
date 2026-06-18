# TorFlash launcher for Windows (PowerShell).
# Runs the GUI from source using the local virtualenv.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUTF8 = "1"
& (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "src\rutor_search.py") @args
