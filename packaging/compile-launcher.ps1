param([Parameter(Mandatory=$true)][string]$PayloadDir)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$target = Join-Path $PayloadDir 'Citadel.exe'
if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
$source = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'Launcher.cs') -Raw
$icon = Join-Path $root 'assets\citadel.ico'
$provider = New-Object Microsoft.CSharp.CSharpCodeProvider
$parameters = New-Object System.CodeDom.Compiler.CompilerParameters
$parameters.GenerateExecutable = $true
$parameters.GenerateInMemory = $false
$parameters.OutputAssembly = $target
$parameters.CompilerOptions = "/target:winexe /platform:x64 /win32icon:`"$icon`""
$null = $parameters.ReferencedAssemblies.Add('System.dll')
$null = $parameters.ReferencedAssemblies.Add('System.Core.dll')
$null = $parameters.ReferencedAssemblies.Add('System.Windows.Forms.dll')
try {
    $result = $provider.CompileAssemblyFromSource($parameters, $source)
    if ($result.Errors.HasErrors) { throw ($result.Errors | Out-String) }
} finally { $provider.Dispose() }
if (-not (Test-Path -LiteralPath $target)) { throw 'Launcher compiler did not create Citadel.exe' }
