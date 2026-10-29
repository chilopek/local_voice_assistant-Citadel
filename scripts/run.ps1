$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Сначала выполните .\scripts\setup.ps1"
}

$env:ASSISTANT_WORKSPACE = $projectRoot
& .venv\Scripts\python.exe -m assistant
