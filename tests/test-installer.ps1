# Run with Windows PowerShell 5.1 as well as PowerShell 7.
param([string]$Python = (Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe'))
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent

function Import-Function([string]$file, [string]$name) {
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput([IO.File]::ReadAllText($file), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if (-not $definition) { throw "Missing function: $name" }
    return [scriptblock]::Create($definition.Extent.Text)
}

. (Import-Function (Join-Path $projectRoot 'install.ps1') 'Invoke-CheckedCommand')
. (Import-Function (Join-Path $projectRoot 'install.ps1') 'Step')
. (Import-Function (Join-Path $projectRoot 'install.ps1') 'Test-PythonRuntime')
. (Import-Function (Join-Path $projectRoot 'install.ps1') 'Ensure-PythonEnvironment')
. (Import-Function (Join-Path $projectRoot 'bootstrap-installer.ps1') 'Read-InstallerText')
. (Import-Function (Join-Path $projectRoot 'bootstrap-installer.ps1') 'Read-NewInstallerText')
. (Import-Function (Join-Path $projectRoot 'bootstrap-installer.ps1') 'Start-InstallerProcess')

# Poll a redirected process only after it exits, exactly as the GUI timer does.
foreach ($expectedCode in @(0, 17)) {
    $prefix = Join-Path ([IO.Path]::GetTempPath()) ('assistant-exit-test-' + [guid]::NewGuid())
    $command = "Start-Sleep -Milliseconds 100; [Console]::WriteLine('child stdout'); exit $expectedCode"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $child = Start-InstallerProcess (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') "-NoProfile -EncodedCommand $encoded" "$prefix.out" "$prefix.err"
    try {
        Start-Sleep -Seconds 2
        if (-not $child.HasExited) { throw 'Child did not exit in time' }
        $child.WaitForExit()
        $child.Refresh()
        if ($null -eq $child.ExitCode -or $child.ExitCode -ne $expectedCode) { throw "Lost process exit code: expected $expectedCode; got $($child.ExitCode)" }
    } finally {
        $child.Dispose()
        Remove-Item -LiteralPath "$prefix.out", "$prefix.err" -Force -ErrorAction SilentlyContinue
    }
}

# stderr with exit 0 must not abort a pip/git command in Windows PowerShell 5.1.
Invoke-CheckedCommand $Python @('-c', "import sys; print('native stderr regression',file=sys.stderr); print('stdout ok')")
$failed = $false
try { Invoke-CheckedCommand $Python @('-c', "import sys; print('intentional failure',file=sys.stderr); sys.exit(17)") }
catch { $failed = $_.ToString().Contains('17') -and $_.ToString().Contains('intentional failure') }
if (-not $failed) { throw 'Native command failure was not preserved' }

$script:runtimeProbe = Join-Path $projectRoot 'assistant\tk_runtime.py'
if (-not (Test-PythonRuntime $Python)) { throw 'Healthy Python/Tcl runtime rejected' }
$testInstall = Join-Path ([IO.Path]::GetTempPath()) ('assistant-recovery-test-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path (Join-Path $testInstall '.venv') -Force | Out-Null
try {
    # A working python.exe with broken Tcl must be rejected by the actual probe.
    $badProbe = Join-Path $testInstall 'missing-tcl.py'
    [IO.File]::WriteAllText($badProbe, "import tkinter; raise tkinter.TclError('missing init.tcl')")
    $script:runtimeProbe = $badProbe
    if (Test-PythonRuntime $Python) { throw 'Python with failing Tcl was accepted' }
    $script:runtimeProbe = Join-Path $projectRoot 'assistant\tk_runtime.py'
    # An incomplete old venv must be backed up, then replaced by a working venv.
    [IO.File]::WriteAllText((Join-Path $testInstall '.venv\old-marker.txt'), 'preserve me')
    $newVenv = Ensure-PythonEnvironment $testInstall @{ Path = $Python; Launcher = $false }
    if (-not (Test-PythonRuntime (Join-Path $newVenv 'Scripts\python.exe'))) { throw 'Venv repair failed' }
    $backups = @(Get-ChildItem -LiteralPath $testInstall -Directory -Filter '.venv-backup-*')
    if ($backups.Count -ne 1 -or -not (Test-Path -LiteralPath (Join-Path $backups[0].FullName 'old-marker.txt'))) { throw 'Previous venv was not preserved' }
} finally {
    $resolvedTest = [IO.Path]::GetFullPath($testInstall)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ((Split-Path $resolvedTest -Parent) -eq $tempRoot -and (Split-Path $resolvedTest -Leaf) -like 'assistant-recovery-test-*') {
        Remove-Item -LiteralPath $resolvedTest -Recurse -Force
    }
}

# Read live output while a writer still holds it open, without losing or repeating text.
$testLog = Join-Path ([IO.Path]::GetTempPath()) ('assistant-log-test-' + [guid]::NewGuid() + '.txt')
$writer = [IO.File]::Open($testLog, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
try {
    $data = [Text.Encoding]::UTF8.GetBytes('first line' + [Environment]::NewLine)
    $writer.Write($data, 0, $data.Length)
    $writer.Flush()
    $offset = 0
    $first = Read-NewInstallerText $testLog ([ref]$offset)
    if ($first -notmatch 'first line') { throw 'Cannot read an open log' }
    $data = [Text.Encoding]::UTF8.GetBytes('second line' + [Environment]::NewLine)
    $writer.Write($data, 0, $data.Length)
    $writer.Flush()
    $second = Read-NewInstallerText $testLog ([ref]$offset)
    if ($second -notmatch 'second line' -or $second -match 'first line') { throw 'Live log duplicated or lost output' }
} finally {
    $writer.Dispose()
    Remove-Item -LiteralPath $testLog -Force
}

foreach ($file in @('scripts\run.ps1', 'install.ps1', 'bootstrap-installer.ps1', 'build-installer.ps1')) {
    $errors = $null
    $tokens = $null
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $projectRoot $file), [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count) { throw "Parse error in $file : $($errors | Out-String)" }
}
Write-Host 'PASS: Tcl runtime probe, venv recovery, exit codes, native stderr, live logs, Windows PowerShell parser'
exit 0
