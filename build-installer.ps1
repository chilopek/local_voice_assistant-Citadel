param(
    [string]$PythonPath,
    [string]$ISCCPath,
    [switch]$TestBuild,
    [switch]$RefreshPayload,
    [switch]$SkipStage
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$payload = Join-Path $root 'dist\payload'
if (-not $PythonPath) { $PythonPath = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Prepare a Windows Python build environment first: python -m venv .venv; .venv\Scripts\python -m pip install -r packaging\requirements-win.lock'
}
if (-not $ISCCPath) {
    $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($compiler) { $ISCCPath = $compiler.Source }
    $locations = @("$env:LOCALAPPDATA\Programs\Inno Setup 6", "$env:ProgramFiles\Inno Setup 7", "${env:ProgramFiles(x86)}\Inno Setup 6")
    foreach ($key in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')) {
        if (Test-Path $key) {
            $locations += @(Get-ChildItem $key | ForEach-Object { Get-ItemProperty $_.PSPath } | Where-Object { $_.DisplayName -like 'Inno Setup*' } | Select-Object -ExpandProperty InstallLocation)
        }
    }
    foreach ($location in $locations) {
        $candidate = Join-Path $location 'ISCC.exe'
        if (-not $ISCCPath -and (Test-Path -LiteralPath $candidate)) { $ISCCPath = $candidate }
    }
}
if (-not $ISCCPath -or -not (Test-Path -LiteralPath $ISCCPath)) {
    throw 'Install the Inno Setup build tool (winget install --id JRSoftware.InnoSetup -e), then rerun this build.'
}
if (-not $SkipStage) {
    $stageArgs = @((Join-Path $root 'packaging\stage_payload.py'), '--output', $payload)
    if ($RefreshPayload) { $stageArgs += '--refresh' }
    & $PythonPath @stageArgs
    if ($LASTEXITCODE -ne 0) { throw 'Payload staging failed' }
}
if (-not (Test-Path -LiteralPath (Join-Path $payload '.citadel-payload.json'))) { throw 'Missing owned payload manifest' }
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'packaging\compile-launcher.ps1') -PayloadDir $payload
if ($LASTEXITCODE -ne 0) { throw 'Citadel.exe compilation failed' }
$arguments = @('/Qp', "/DPayloadDir=$payload", "/DOutputDir=$(Join-Path $root 'dist')")
if ($TestBuild) { $arguments += '/DTestBuild' }
$arguments += (Join-Path $root 'packaging\citadel.iss')
& $ISCCPath @arguments
if ($LASTEXITCODE -ne 0) { throw 'Native installer compilation failed' }
$name = if ($TestBuild) { 'LocalAssistantSetupTest.exe' } else { 'LocalAssistantSetup.exe' }
Write-Host "Built: $(Join-Path $root "dist\$name")"
