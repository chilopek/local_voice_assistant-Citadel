[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl,

    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "LocalAssistant"),

    [ValidateSet("auto", "qwen3:1.7b-instruct", "qwen3:4b-instruct", "qwen3:8b-instruct")]
    [string]$Model = "auto",

    [ValidateSet("auto", "fast", "balanced", "power")]
    [string]$Mode = "auto",

    [ValidateSet("auto", "on", "off")]
    [string]$Voice = "auto",

    [switch]$Fast,
    [switch]$Update,
    [switch]$NoShortcut,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Step([string]$message) {
    Write-Host "`n==> $message" -ForegroundColor Cyan
}

function Find-Executable([string]$name, [string[]]$knownPaths = @()) {
    $fromPath = Get-Command $name -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }
    foreach ($path in $knownPaths) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return $null
}

function Install-WingetPackage([string]$id, [string]$label, [switch]$UserScope) {
    $winget = Find-Executable "winget.exe"
    if (-not $winget) {
        throw "Не найден winget. Установите App Installer из Microsoft Store и повторите запуск."
    }

    Step "Установка $label"
    $args = @("install", "--id", $id, "-e", "--accept-source-agreements", "--accept-package-agreements")
    if ($UserScope) { $args += @("--scope", "user") }
    & $winget @args
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить $label через winget." }
}

function Find-Python {
    $py = Find-Executable "py.exe"
    if ($py) { return @{ Path = $py; Launcher = $true } }
    $python = Find-Executable "python.exe" @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    )
    if ($python) { return @{ Path = $python; Launcher = $false } }
    return $null
}

function Find-Ollama {
    return Find-Executable "ollama.exe" @(
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "$env:LOCALAPPDATA\Ollama\ollama.exe",
        "$env:ProgramFiles\Ollama\ollama.exe"
    )
}

function Invoke-Python([hashtable]$python, [string[]]$arguments) {
    if ($python.Launcher) { & $python.Path @arguments }
    else { & $python.Path @arguments }
    if ($LASTEXITCODE -ne 0) { throw "Команда Python завершилась с ошибкой: $($arguments -join ' ')" }
}

function Select-Model {
    $computer = Get-CimInstance Win32_ComputerSystem
    $ramGb = [math]::Round($computer.TotalPhysicalMemory / 1GB, 1)
    $gpuNames = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)

    Write-Host "`nОбнаружено оборудование:" -ForegroundColor Yellow
    Write-Host "  RAM: $ramGb ГБ"
    Write-Host "  GPU: $($gpuNames -join ', ')"

    if ($Model -ne "auto") { return $Model }
    if ($Fast) { $Mode = "fast" }

    if (-not $NonInteractive -and $Mode -eq "auto") {
        Write-Host "`nВыберите режим помощника:" -ForegroundColor Yellow
        Write-Host "  [1] Быстрый          — qwen3:1.7b-instruct"
        Write-Host "  [2] Сбалансированный — qwen3:4b-instruct (рекомендуется)"
        Write-Host "  [3] Мощный           — qwen3:8b-instruct"
        Write-Host "  [4] Авто             — выбор по характеристикам"
        $choice = Read-Host "Введите номер [4]"
        switch ($choice) {
            "1" { $Mode = "fast" }
            "2" { $Mode = "balanced" }
            "3" { $Mode = "power" }
            default { $Mode = "auto" }
        }
    }

    switch ($Mode) {
        "fast" { return "qwen3:1.7b-instruct" }
        "balanced" { return "qwen3:4b-instruct" }
        "power" { return "qwen3:8b-instruct" }
        default {
            if ($ramGb -ge 16) { return "qwen3:8b-instruct" }
            return "qwen3:4b-instruct"
        }
    }
}

function Select-Voice {
    if ($Voice -eq "on") { return $true }
    if ($Voice -eq "off") { return $false }
    if ($NonInteractive) { return $true }

    Write-Host "`nВключить голосовой режим?" -ForegroundColor Yellow
    Write-Host "Голосовые зависимости добавят несколько сотен мегабайт и скачают модели при первом запуске."
    $choice = Read-Host "Включить голос [Y/n]"
    return [string]::IsNullOrWhiteSpace($choice) -or $choice.Trim().ToLowerInvariant() -in @("y", "yes", "д", "да")
}

function Wait-Ollama([string]$ollamaPath) {
    $healthy = $false
    try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null; $healthy = $true } catch {}
    if (-not $healthy) {
        Step "Запуск локального сервиса Ollama"
        Start-Process -FilePath $ollamaPath -ArgumentList "serve" -WindowStyle Hidden | Out-Null
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 2
            try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null; $healthy = $true; break } catch {}
        }
    }
    if (-not $healthy) { throw "Ollama не отвечает на http://127.0.0.1:11434." }
}

Step "Проверка Windows и рабочего каталога"
if (-not [Environment]::OSVersion.Version) { throw "Поддерживается только Windows." }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)

$git = Find-Executable "git.exe"
if (-not $git) {
    Install-WingetPackage "Git.Git" "Git" -UserScope
    $git = Find-Executable "git.exe" @("$env:ProgramFiles\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")
}
if (-not $git) { throw "Git установлен, но git.exe не найден." }

$python = Find-Python
if (-not $python) {
    Install-WingetPackage "Python.Python.3.12" "Python 3.12" -UserScope
    $python = Find-Python
}
if (-not $python) { throw "Python 3.12 установлен, но Python Launcher не найден." }

$ollama = Find-Ollama
if (-not $ollama) {
    Install-WingetPackage "Ollama.Ollama" "Ollama"
    $ollama = Find-Ollama
}
if (-not $ollama) { throw "Ollama установлена, но ollama.exe не найден." }

Step "Загрузка проекта из Git"
if (Test-Path -LiteralPath $InstallDir) {
    $gitFolder = Join-Path $InstallDir ".git"
    if (Test-Path -LiteralPath $gitFolder) {
        if ($Update) {
            & $git -C $InstallDir pull --ff-only
            if ($LASTEXITCODE -ne 0) { throw "Не удалось обновить существующий проект." }
        }
    } elseif (@(Get-ChildItem -LiteralPath $InstallDir -Force).Count -eq 0) {
        & $git clone --depth 1 $RepoUrl $InstallDir
        if ($LASTEXITCODE -ne 0) { throw "Не удалось клонировать репозиторий." }
    } else {
        throw "Каталог установки уже существует и не является пустым Git-репозиторием: $InstallDir"
    }
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallDir) | Out-Null
    & $git clone --depth 1 $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) { throw "Не удалось клонировать репозиторий." }
}

$selectedModel = Select-Model
Step "Выбор модели: $selectedModel"
$voiceEnabled = Select-Voice
$sttModel = if ($Mode -eq "fast" -or $selectedModel -eq "qwen3:1.7b-instruct") { "base" } else { "small" }
Step "Голосовой режим: $(if ($voiceEnabled) { 'включён' } else { 'выключен' })"

$localConfig = @{
    model = $selectedModel
    think = $false
    num_predict = 192
    num_ctx = 4096
    voice = $voiceEnabled
    stt_model = $sttModel
    tts_model = "v5_6_ru"
    tts_speaker = "aidar"
    voice_threads = 4
}
$localConfig | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallDir "assistant.local.json") -Encoding utf8

Step "Создание виртуального окружения Python"
$venv = Join-Path $InstallDir ".venv"
if (-not (Test-Path -LiteralPath (Join-Path $venv "Scripts\python.exe"))) {
    if ($python.Launcher) { & $python.Path -3.12 -m venv $venv }
    else { & $python.Path -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать виртуальное окружение Python." }
}
$venvPython = Join-Path $venv "Scripts\python.exe"
Step "Установка Python-зависимостей"
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Не удалось обновить pip." }
$basePackageSpec = $InstallDir
& $venvPython -m pip install -e $basePackageSpec
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости проекта." }

if ($voiceEnabled) {
    try {
        Step "Установка голосовых зависимостей"
        $voicePackageSpec = "$InstallDir[voice]"
        & $venvPython -m pip install -e $voicePackageSpec
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Голосовые зависимости не установились. Продолжаю без голосового режима."
            $voiceEnabled = $false
        }
    } catch {
        Write-Warning ("Голосовые зависимости пропущены: " + $_.Exception.Message)
        $voiceEnabled = $false
    }
}

if (-not $voiceEnabled) {
    $localConfig.voice = $false
    $localConfig | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallDir "assistant.local.json") -Encoding utf8
}

& $venvPython -c "from assistant import __version__; print('local-assistant', __version__)"
if ($LASTEXITCODE -ne 0) { throw "Файлы помощника не прошли проверку импорта." }

Wait-Ollama $ollama
$models = (Invoke-RestMethod "http://127.0.0.1:11434/api/tags").models
$alreadyInstalled = @($models | Where-Object { $_.name -eq $selectedModel }).Count -gt 0
if (-not $alreadyInstalled) {
    Step "Загрузка модели $selectedModel"
    & $ollama pull $selectedModel
    if ($LASTEXITCODE -ne 0) { throw "Не удалось загрузить модель $selectedModel." }
}

if (-not $NoShortcut) {
    Step "Создание ярлыка на рабочем столе"
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "Локальный помощник.lnk"
    $powershell = (Get-Command powershell.exe).Source
    $runScript = Join-Path $InstallDir "scripts\run.ps1"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $powershell
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runScript`""
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Description = "Локальный персональный помощник"
    $shortcut.Save()
}

Step "Установка завершена"
Write-Host "Каталог: $InstallDir" -ForegroundColor Green
Write-Host "Модель: $selectedModel" -ForegroundColor Green
Write-Host "Голос: $(if ($voiceEnabled) { 'включён' } else { 'выключен' })" -ForegroundColor Green
Write-Host "Запуск: $InstallDir\scripts\run.ps1" -ForegroundColor Green
