param(
    [switch]$Console,
    [switch]$Check
)

# Keep this wrapper ASCII so Windows PowerShell 5.1 can always read it.
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$launchLog = Join-Path $projectRoot "assistant-launch.log"
try {
    Set-Location -LiteralPath $projectRoot
    $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Python environment missing. Run LocalAssistantSetup.exe again."
    }
    $env:ASSISTANT_WORKSPACE = $projectRoot
    $env:PYTHONUTF8 = "1"
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        if ($Console) { & $python -m assistant }
        elseif ($Check) { & $python -m assistant.launcher --check }
        else { & $python -m assistant.launcher }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    exit $code
} catch {
    $details = $_.ToString()
    Add-Content -LiteralPath $launchLog -Encoding UTF8 -Value "$(Get-Date -Format s) $details"
    if (-not $Check) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show("$details`n`nLog: $launchLog", "Assistant startup error", "OK", "Error") | Out-Null
    }
    [Console]::Error.WriteLine($details)
    exit 1
}
