param(
    [switch]$Console
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Сначала выполните .\scripts\setup.ps1"
}

$env:ASSISTANT_WORKSPACE = $projectRoot
$module = if ($Console) { "assistant" } else { "assistant.gui" }
$launchLog = Join-Path $projectRoot "assistant-launch.log"

try {
    Add-Content -LiteralPath $launchLog -Value "[$(Get-Date -Format s)] Запуск модуля $module" -Encoding utf8
    if ($Console) {
        & .venv\Scripts\python.exe -m $module
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) { throw "Python завершился с кодом $exitCode." }
    } else {
        $output = & .venv\Scripts\python.exe -m $module 2>&1
        $exitCode = $LASTEXITCODE
        if ($output) { $output | Out-String | Add-Content -LiteralPath $launchLog -Encoding utf8 }
        if ($exitCode -ne 0) {
            $details = if ($output) { ($output | Out-String).Trim() } else { "Python завершился с кодом $exitCode." }
            throw $details
        }
    }
    Add-Content -LiteralPath $launchLog -Value "[$(Get-Date -Format s)] Завершено с кодом $exitCode" -Encoding utf8
} catch {
    $details = $_.Exception.ToString()
    Add-Content -LiteralPath $launchLog -Value "[$(Get-Date -Format s)] ОШИБКА: $details" -Encoding utf8
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            "Помощник не запустился.`n`n$details`n`nПодробный лог:`n$launchLog",
            "Ошибка запуска Local Assistant",
            "OK",
            "Error"
        ) | Out-Null
    } catch {}
    Write-Error "Помощник не запустился. Лог: $launchLog`n$details"
    if ($Host.Name -notmatch "ServerRemoteHost") { Read-Host "Нажмите Enter, чтобы закрыть окно" }
    exit 1
}
