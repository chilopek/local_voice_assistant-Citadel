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
$progress.Style = [System.Windows.Forms.ProgressBarStyle]::Continuous
$progress.Minimum = 0
$progress.Maximum = 100
$progress.Value = 0
$progress.Visible = $false
$progress.Size = New-Object System.Drawing.Size(620, 18)
$progress.Location = New-Object System.Drawing.Point(27, 215)
$form.Controls.Add($progress)

$progressStatus = New-Object System.Windows.Forms.Label
$progressStatus.Text = "Готово: 0%"
$progressStatus.AutoSize = $true
$progressStatus.Location = New-Object System.Drawing.Point(27, 236)
$form.Controls.Add($progressStatus)

$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true
$log.ReadOnly = $true
$log.ScrollBars = [System.Windows.Forms.ScrollBars]::Vertical
$log.Font = New-Object System.Drawing.Font("Consolas", 9)
$log.Size = New-Object System.Drawing.Size(620, 174)
$log.Location = New-Object System.Drawing.Point(27, 260)
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

function Update-ProgressFromText([string]$text) {
    if ([string]::IsNullOrWhiteSpace($text)) { return }
    $stages = @(
        @{ Marker = "Проверка Windows"; Value = 5; Label = "Проверка системы" },
        @{ Marker = "Загрузка проекта"; Value = 12; Label = "Загрузка файлов проекта" },
        @{ Marker = "Проверка Python и Tcl/Tk"; Value = 16; Label = "Проверка графического окружения Python" },
        @{ Marker = "Обнаружено оборудование"; Value = 20; Label = "Оценка компьютера" },
        @{ Marker = "Выбор модели"; Value = 25; Label = "Подготовка конфигурации" },
        @{ Marker = "Создание виртуального окружения"; Value = 38; Label = "Подготовка Python" },
        @{ Marker = "Установка Python-зависимостей"; Value = 52; Label = "Установка зависимостей" },
        @{ Marker = "Установка голосовых зависимостей"; Value = 68; Label = "Установка голосового режима" },
        @{ Marker = "Запуск локального сервиса Ollama"; Value = 76; Label = "Запуск Ollama" },
        @{ Marker = "Загрузка модели"; Value = 86; Label = "Загрузка модели" },
        @{ Marker = "Создание ярлыка"; Value = 97; Label = "Создание ярлыка" },
        @{ Marker = "Установка завершена"; Value = 100; Label = "Завершено" }
    )
    foreach ($stage in $stages) {
        if ($text.IndexOf($stage.Marker, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            if ($stage.Value -gt $progress.Value) { $progress.Value = $stage.Value }
            $progressStatus.Text = "$($progress.Value)% — $($stage.Label)"
        }
    }
}

function Read-InstallerText([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return "" }
    try {
        $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $memory = New-Object IO.MemoryStream
            $stream.CopyTo($memory)
            $bytes = $memory.ToArray()
            $memory.Dispose()
        } finally {
            $stream.Dispose()
        }
        if ($bytes.Length -eq 0) { return "" }
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
        # A redirected child-process stream can be briefly unavailable between writes.
        return ""
    }
}

$script:installProcess = $null
$script:installTempScript = $null
$script:installStdoutPath = $null
$script:installStderrPath = $null
$script:installStdoutOffset = 0
$script:installStderrOffset = 0
$script:installLastOutputAt = [DateTime]::Now

function Read-NewInstallerText([string]$path, [ref]$offset) {
    $text = Read-InstallerText $path
    if ([string]::IsNullOrEmpty($text)) { return "" }
    if ($text.Length -lt $offset.Value) { $offset.Value = 0 }
    if ($text.Length -eq $offset.Value) { return "" }
    $chunk = $text.Substring($offset.Value)
    $offset.Value = $text.Length
    return $chunk
}

function Flush-InstallerOutput {
    $stdoutChunk = Read-NewInstallerText $script:installStdoutPath ([ref]$script:installStdoutOffset)
    $stderrChunk = Read-NewInstallerText $script:installStderrPath ([ref]$script:installStderrOffset)
    if (-not [string]::IsNullOrWhiteSpace($stdoutChunk)) {
        Add-Log $stdoutChunk.TrimEnd()
        Update-ProgressFromText $stdoutChunk
        $script:installLastOutputAt = [DateTime]::Now
    }
    if (-not [string]::IsNullOrWhiteSpace($stderrChunk)) {
        Add-Log $stderrChunk.TrimEnd()
        Update-ProgressFromText $stderrChunk
        $script:installLastOutputAt = [DateTime]::Now
    }
    if (([DateTime]::Now - $script:installLastOutputAt).TotalSeconds -ge 15) {
        Add-Log "Установка продолжается... ожидаем ответ от текущего шага."
        $script:installLastOutputAt = [DateTime]::Now
    }
}

function Start-InstallerProcess([string]$executable, [string]$arguments, [string]$stdoutPath, [string]$stderrPath) {
    $child = Start-Process -FilePath $executable -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
    # Pin the handle while the child is alive; .NET Framework otherwise loses ExitCode.
    $null = $child.Handle
    return $child
}

$installTimer = New-Object System.Windows.Forms.Timer
$installTimer.Interval = 500
$installTimer.Add_Tick({
    if ($null -eq $script:installProcess) { return }
    Flush-InstallerOutput
    if (-not $script:installProcess.HasExited) { return }

    $script:installProcess.WaitForExit()
    $script:installProcess.Refresh()
    $exitCode = $script:installProcess.ExitCode
    if ($null -eq $exitCode) {
        Add-Log "Не удалось получить код завершения процесса. Проверьте последние строки лога."
        $exitCode = -1
    }
    $stdout = Read-InstallerText $script:installStdoutPath
    $stderr = Read-InstallerText $script:installStderrPath

    $installTimer.Stop()

    if ($exitCode -eq 0) {
        $progress.Value = 100
        $progressStatus.Text = "100% — Завершено"
        Add-Log ""
        Add-Log "Установка завершена. Ярлык создан на рабочем столе."
        [System.Windows.Forms.MessageBox]::Show("Помощник установлен.", "Готово", "OK", "Information") | Out-Null
    } else {
        $progressStatus.Text = "Ошибка — см. подробности в логе"
        $details = if ($stderr) { $stderr.Trim() } elseif ($stdout) { $stdout.Trim() } else { "Процесс завершился с кодом $exitCode." }
        Add-Log ("ОШИБКА: " + $details)
        Add-Log ("Лог процесса: " + $script:installStderrPath)
        Add-Log ("Общий лог установщика: " + $script:sessionLogPath)
        $preview = if ($details.Length -gt 1800) { "..." + $details.Substring($details.Length - 1800) } else { $details }
        [System.Windows.Forms.MessageBox]::Show(
            "Установка завершилась с ошибкой (код $exitCode).`n`n$preview`n`nОбщий лог: $script:sessionLogPath",
            "Ошибка",
            "OK",
            "Error"
        ) | Out-Null
    }

    $installButton.Enabled = $true
    $closeButton.Enabled = $true
    $script:installProcess = $null
})

$installButton.Add_Click({
    $installButton.Enabled = $false
    $closeButton.Enabled = $false
    $progress.Visible = $true
    $progress.Value = 1
    $progressStatus.Text = "1% — Запуск установщика"
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
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
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
        $script:installStdoutOffset = 0
        $script:installStderrOffset = 0
        $script:installLastOutputAt = [DateTime]::Now
        $arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$tempScript`" -RepoUrl `"$repoUrl`" -Mode $mode -Voice $voice -Update -NonInteractive"
        $script:installProcess = Start-InstallerProcess $powershell $arguments $stdoutPath $stderrPath
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
