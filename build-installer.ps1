$ErrorActionPreference = "Stop"
Set-ExecutionPolicy -Scope Process Bypass -Force

if (-not (Get-Command Invoke-ps2exe -ErrorAction SilentlyContinue)) {
    Install-Module -Name ps2exe -Scope CurrentUser -Force -AllowClobber -Repository PSGallery
    Import-Module ps2exe
}

New-Item -ItemType Directory -Force -Path .\dist | Out-Null
Invoke-ps2exe `
    -inputFile .\bootstrap-installer.ps1 `
    -outputFile .\dist\LocalAssistantSetup.exe `
    -noConsole `
    -x64 `
    -DPIAware `
    -title "Local Assistant Setup" `
    -description "Установка локального голосового помощника" `
    -company "Local Assistant" `
    -product "Local Assistant" `
    -version "0.1.0.0"

Write-Host "Готово: .\dist\LocalAssistantSetup.exe" -ForegroundColor Green
