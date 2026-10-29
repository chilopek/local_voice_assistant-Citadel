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
$pythonFallbackUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$pythonFallbackMd5 = "5eddb0b6f12c852725de071ae681dde4"

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

function Install-PythonFallback {
    $installerPath = Join-Path ([IO.Path]::GetTempPath()) ("python-3.12.10-{0}.exe" -f ([guid]::NewGuid().ToString("N")))
    Step "Загрузка Python 3.12 с python.org"
    Invoke-WebRequest -Uri $pythonFallbackUrl -OutFile $installerPath -UseBasicParsing -ErrorAction Stop
    $actualMd5 = (Get-FileHash -LiteralPath $installerPath -Algorithm MD5).Hash.ToLowerInvariant()
    if ($actualMd5 -ne $pythonFallbackMd5) {
        throw "Проверка скачанного Python не пройдена. Ожидался MD5 $pythonFallbackMd5, получен $actualMd5."
    }

    Step "Установка Python 3.12 без прав администратора"
    $pythonInstall = Start-Process -FilePath $installerPath -ArgumentList @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=0",
        "Include_launcher=1",
        "Include_test=0",
        "SimpleInstall=1"
    ) -Wait -PassThru
    if ($pythonInstall.ExitCode -notin @(0, 3010)) {
        throw "Официальный установщик Python завершился с кодом $($pythonInstall.ExitCode)."
    }
}

function Test-PythonRuntime([string]$path, [switch]$Launcher, [string]$LauncherVersion = "3") {
    $savedErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        if ($Launcher) {
            & $path "-$LauncherVersion" -c "import sys; assert sys.version_info >= (3, 12)" 1>$null 2>$null
        } else {
            & $path -c "import sys; assert sys.version_info >= (3, 12)" 1>$null 2>$null
        }
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $savedErrorAction
    }
}

function Find-Python {
    $py = Find-Executable "py.exe"
    if ($py) {
        foreach ($launcherVersion in @("3.14", "3.13", "3.12", "3")) {
            if (Test-PythonRuntime $py -Launcher -LauncherVersion $launcherVersion) {
                return @{ Path = $py; Launcher = $true; LauncherVersion = $launcherVersion }
            }
        }
    }

    $candidates = @()
    $pathPython = Find-Executable "python.exe"
    if ($pathPython) { $candidates += $pathPython }
    $candidates += @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python314\python.exe"
    )
    $pythonRoots = @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:ProgramFiles\Python"
    )
    foreach ($root in $pythonRoots) {
        if (Test-Path -LiteralPath $root) {
            $candidates += @(Get-ChildItem -LiteralPath $root -Directory -Filter "Python3*" -ErrorAction SilentlyContinue | ForEach-Object {
                Join-Path $_.FullName "python.exe"
            })
        }
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        if (Test-PythonRuntime $candidate) { return @{ Path = $candidate; Launcher = $false } }
    }
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

function Download-ProjectZip([string]$destination, [string]$repositoryUrl) {
    $repositoryBase = $repositoryUrl.TrimEnd('/') -replace '\.git$', ''
    if ($repositoryBase -notmatch '^https://github\.com/[^/]+/[^/]+$') {
        throw "Git не найден, а ZIP-загрузка поддерживается только для GitHub-репозиториев: $repositoryUrl"
    }

    $archivePath = Join-Path ([IO.Path]::GetTempPath()) ("local-assistant-{0}.zip" -f ([guid]::NewGuid().ToString("N")))
    $extractPath = Join-Path ([IO.Path]::GetTempPath()) ("local-assistant-extract-{0}" -f ([guid]::NewGuid().ToString("N")))
    $archiveUrl = "$repositoryBase/archive/refs/heads/main.zip"
    Step "Загрузка проекта из GitHub ZIP"
    Invoke-WebRequest -Uri $archiveUrl -OutFile $archivePath -UseBasicParsing -ErrorAction Stop
    New-Item -ItemType Directory -Force -Path $extractPath | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath -Force
    $sourceRoot = Get-ChildItem -LiteralPath $extractPath -Directory | Select-Object -First 1
    if (-not $sourceRoot) { throw "GitHub ZIP не содержит папку проекта." }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Get-ChildItem -LiteralPath $sourceRoot.FullName -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $destination $_.Name) -Recurse -Force
    }
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
    try {
        Install-WingetPackage "Git.Git" "Git" -UserScope
    } catch {
        Write-Warning "Git не установлен, продолжу через загрузку ZIP: $($_.Exception.Message)"
    }
    $git = Find-Executable "git.exe" @("$env:ProgramFiles\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")
}

$python = Find-Python
if (-not $python) {
    try {
        Install-WingetPackage "Python.Python.3.12" "Python 3.12" -UserScope
    } catch {
        Write-Warning "Python через winget не установлен, использую официальный fallback: $($_.Exception.Message)"
        Install-PythonFallback
    }
    $python = Find-Python
}
if (-not $python) { throw "Совместимый Python 3.12 или новее не найден." }

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
            if ($git) {
                & $git -C $InstallDir pull --ff-only
                if ($LASTEXITCODE -ne 0) { throw "Не удалось обновить существующий проект." }
            } else {
                Download-ProjectZip $InstallDir $RepoUrl
            }
        }
    } elseif (@(Get-ChildItem -LiteralPath $InstallDir -Force).Count -eq 0) {
        if ($git) {
            & $git clone --depth 1 $RepoUrl $InstallDir
            if ($LASTEXITCODE -ne 0) { throw "Не удалось клонировать репозиторий." }
        } else {
            Download-ProjectZip $InstallDir $RepoUrl
        }
    } elseif ($Update) {
        Download-ProjectZip $InstallDir $RepoUrl
    } else {
        throw "Каталог установки уже существует и не является пустым Git-репозиторием: $InstallDir"
    }
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallDir) | Out-Null
    if ($git) {
        & $git clone --depth 1 $RepoUrl $InstallDir
        if ($LASTEXITCODE -ne 0) { throw "Не удалось клонировать репозиторий." }
    } else {
        Download-ProjectZip $InstallDir $RepoUrl
    }
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
    if ($python.Launcher) { & $python.Path "-$($python.LauncherVersion)" -m venv $venv }
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
