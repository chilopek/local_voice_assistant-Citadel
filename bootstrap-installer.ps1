Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

[System.Windows.Forms.Application]::EnableVisualStyles()

$repoUrl = "https://github.com/chilopek/local-assistant.git"
$rawInstallerUrl = "https://raw.githubusercontent.com/chilopek/local-assistant/main/install.ps1"

$form = New-Object System.Windows.Forms.Form
$form.Text = "Локальный помощник — установка"
$form.StartPosition = "CenterScreen"
$form.Size = New-Object System.Drawing.Size(690, 500)
$form.MinimumSize = New-Object System.Drawing.Size(690, 500)
$form.MaximizeBox = $false

$title = New-Object System.Windows.Forms.Label
$title.Text = "Установка локального помощника"
$title.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(24, 20)
$form.Controls.Add($title)

$description = New-Object System.Windows.Forms.Label
$description.Text = "Выберите режим. Установщик сам подготовит Python, Ollama, модель и ярлык."
$description.AutoSize = $true
$description.Location = New-Object System.Drawing.Point(27, 58)
$form.Controls.Add($description)

$modeLabel = New-Object System.Windows.Forms.Label
$modeLabel.Text = "Режим модели:"
$modeLabel.AutoSize = $true
$modeLabel.Location = New-Object System.Drawing.Point(27, 98)
$form.Controls.Add($modeLabel)

$modeBox = New-Object System.Windows.Forms.ComboBox
$modeBox.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
$modeBox.Items.AddRange(@(
    "Авто — подобрать по компьютеру",
    "Быстрый — минимум задержки",
    "Сбалансированный — рекомендуется",
    "Мощный — лучшее качество"
))
$modeBox.SelectedIndex = 0
$modeBox.Size = New-Object System.Drawing.Size(360, 28)
$modeBox.Location = New-Object System.Drawing.Point(145, 94)
$form.Controls.Add($modeBox)

$voiceCheck = New-Object System.Windows.Forms.CheckBox
$voiceCheck.Text = "Установить голосовой режим (дополнительные пакеты)"
$voiceCheck.Checked = $true
$voiceCheck.AutoSize = $true
$voiceCheck.Location = New-Object System.Drawing.Point(27, 130)
$form.Controls.Add($voiceCheck)

$installButton = New-Object System.Windows.Forms.Button
$installButton.Text = "Установить"
$installButton.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
$installButton.Size = New-Object System.Drawing.Size(150, 38)
$installButton.Location = New-Object System.Drawing.Point(27, 165)
$form.Controls.Add($installButton)

$closeButton = New-Object System.Windows.Forms.Button
$closeButton.Text = "Закрыть"
$closeButton.Size = New-Object System.Drawing.Size(100, 38)
$closeButton.Location = New-Object System.Drawing.Point(187, 165)
$closeButton.Add_Click({ $form.Close() })
$form.Controls.Add($closeButton)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
$progress.Visible = $false
$progress.Size = New-Object System.Drawing.Size(620, 18)
$progress.Location = New-Object System.Drawing.Point(27, 215)
$form.Controls.Add($progress)

$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true
$log.ReadOnly = $true
$log.ScrollBars = [System.Windows.Forms.ScrollBars]::Vertical
$log.Font = New-Object System.Drawing.Font("Consolas", 9)
$log.Size = New-Object System.Drawing.Size(620, 195)
$log.Location = New-Object System.Drawing.Point(27, 245)
$form.Controls.Add($log)

$desktopPath = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($desktopPath)) { $desktopPath = [IO.Path]::GetTempPath() }
$script:sessionLogPath = Join-Path $desktopPath "LocalAssistant-install.log"
$script:sessionLogInitialized = $false

function Add-Log([string]$text) {
    $log.AppendText($text + [Environment]::NewLine)
    try {
        if (-not $script:sessionLogInitialized) {
            [IO.File]::WriteAllText($script:sessionLogPath, $text + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($true)))
            $script:sessionLogInitialized = $true
        } else {
            [IO.File]::AppendAllText($script:sessionLogPath, $text + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
        }
    } catch {}
}

function Read-InstallerText([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return "" }
    try {
        $bytes = [IO.File]::ReadAllBytes($path)
        if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
            return [Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2)
        }
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            return [Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3)
        }
        if ($bytes.Length -ge 2 -and $bytes[1] -eq 0) {
            return [Text.Encoding]::Unicode.GetString($bytes)
        }
        return [Text.Encoding]::UTF8.GetString($bytes)
    } catch {
        return "Не удалось прочитать лог процесса: $($_.Exception.Message)"
    }
}

$script:installProcess = $null
$script:installTempScript = $null
$script:installStdoutPath = $null
$script:installStderrPath = $null

$installTimer = New-Object System.Windows.Forms.Timer
$installTimer.Interval = 500
$installTimer.Add_Tick({
    if ($null -eq $script:installProcess) { return }
    if (-not $script:installProcess.HasExited) { return }

    $exitCode = $script:installProcess.ExitCode
    $stdout = Read-InstallerText $script:installStdoutPath
    $stderr = Read-InstallerText $script:installStderrPath

    if ($stdout) { Add-Log $stdout.Trim() }
    if ($stderr) { Add-Log $stderr.Trim() }
    $installTimer.Stop()

    if ($exitCode -eq 0) {
        Add-Log ""
        Add-Log "Установка завершена. Ярлык создан на рабочем столе."
        [System.Windows.Forms.MessageBox]::Show("Помощник установлен.", "Готово", "OK", "Information") | Out-Null
    } else {
        $details = if ($stderr) { $stderr.Trim() } elseif ($stdout) { $stdout.Trim() } else { "Процесс завершился с кодом $exitCode." }
        Add-Log ("ОШИБКА: " + $details)
        Add-Log ("Лог процесса: " + $script:installStderrPath)
        Add-Log ("Общий лог установщика: " + $script:sessionLogPath)
        $preview = if ($details.Length -gt 1200) { $details.Substring(0, 1200) + "..." } else { $details }
        [System.Windows.Forms.MessageBox]::Show(
            "Установка завершилась с ошибкой (код $exitCode).`n`n$preview`n`nОбщий лог: $script:sessionLogPath",
            "Ошибка",
            "OK",
            "Error"
        ) | Out-Null
    }

    $progress.Visible = $false
    $installButton.Enabled = $true
    $closeButton.Enabled = $true
    $script:installProcess = $null
})

$installButton.Add_Click({
    $installButton.Enabled = $false
    $closeButton.Enabled = $false
    $progress.Visible = $true
    Add-Log "Подготовка установки..."
    Add-Log ("Подробный лог: " + $script:sessionLogPath)

    $mode = switch ($modeBox.SelectedIndex) {
        1 { "fast" }
        2 { "balanced" }
        3 { "power" }
        default { "auto" }
    }
    $voice = if ($voiceCheck.Checked) { "on" } else { "off" }

    try {
        $tempScript = Join-Path ([IO.Path]::GetTempPath()) ("local-assistant-install-{0}.ps1" -f $PID)
        $stdoutPath = Join-Path ([IO.Path]::GetTempPath()) ("local-assistant-install-{0}.out.log" -f $PID)
        $stderrPath = Join-Path ([IO.Path]::GetTempPath()) ("local-assistant-install-{0}.err.log" -f $PID)
        Invoke-WebRequest -Uri $rawInstallerUrl -OutFile $tempScript -UseBasicParsing -ErrorAction Stop

        # Windows PowerShell 5.1 treats UTF-8 without BOM as the system ANSI code page.
        # Normalize the downloaded script so Cyrillic source remains valid on every Windows host.
        $downloadedBytes = [IO.File]::ReadAllBytes($tempScript)
        $downloadedText = [Text.Encoding]::UTF8.GetString($downloadedBytes).TrimStart([char]0xFEFF)
        [IO.File]::WriteAllText($tempScript, $downloadedText, (New-Object System.Text.UTF8Encoding($true)))

        $powershell = (Get-Command powershell.exe -ErrorAction SilentlyContinue).Source
        if (-not $powershell) { $powershell = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source }
        if (-not $powershell) { throw "Не найден PowerShell (powershell.exe или pwsh.exe)." }

        $script:installTempScript = $tempScript
        $script:installStdoutPath = $stdoutPath
        $script:installStderrPath = $stderrPath
        $arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$tempScript`" -RepoUrl `"$repoUrl`" -Mode $mode -Voice $voice -NonInteractive"
        $script:installProcess = Start-Process -FilePath $powershell -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        $installTimer.Start()
    } catch {
        $message = $_.Exception.ToString()
        Add-Log ("ОШИБКА ЗАПУСКА: " + $message)
        [System.Windows.Forms.MessageBox]::Show("Не удалось запустить установку. Подробности показаны в окне.", "Ошибка", "OK", "Error") | Out-Null
        $progress.Visible = $false
        $installButton.Enabled = $true
        $closeButton.Enabled = $true
    }
})

$form.Add_Shown({ $form.Activate() })
[void]$form.ShowDialog()
