param(
    [switch]$NoVoice
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Не найден Python Launcher. Установите Python 3.12+ и повторите запуск."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & py -3.12 -m venv .venv
}

Write-Host "Проверка Python:" -ForegroundColor Cyan
& .venv\Scripts\python.exe --version

Write-Host "Установка Python-зависимостей проекта..." -ForegroundColor Cyan
& .venv\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Не удалось обновить pip." }
$packageSpec = if ($NoVoice) { $projectRoot } else { "$projectRoot[voice]" }
& .venv\Scripts\python.exe -m pip install -e $packageSpec
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости проекта." }

function Find-Ollama {
    $fromPath = Get-Command ollama -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }
    $knownPaths = @(
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "$env:LOCALAPPDATA\Ollama\ollama.exe",
        "$env:ProgramFiles\Ollama\ollama.exe"
    )
    foreach ($path in $knownPaths) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return $null
}

$ollama = Find-Ollama
if (-not $ollama) {
    Write-Warning "Ollama не найдена в PATH. Установите Ollama, затем запустите: ollama serve"
    Write-Host "После установки выполните: ollama pull qwen3:4b" -ForegroundColor Yellow
    exit 0
}

Write-Host "Загрузка лёгкой модели qwen3:4b-instruct..." -ForegroundColor Cyan
& $ollama pull qwen3:4b-instruct
Write-Host "Готово. Запустите .\scripts\run.ps1" -ForegroundColor Green
