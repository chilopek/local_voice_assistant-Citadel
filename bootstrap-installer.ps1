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

$installButton = New-Object System.Windows.Forms.Button
$installButton.Text = "Установить"
$installButton.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
$installButton.Size = New-Object System.Drawing.Size(150, 38)
$installButton.Location = New-Object System.Drawing.Point(27, 140)
$form.Controls.Add($installButton)

$closeButton = New-Object System.Windows.Forms.Button
$closeButton.Text = "Закрыть"
$closeButton.Size = New-Object System.Drawing.Size(100, 38)
$closeButton.Location = New-Object System.Drawing.Point(187, 140)
$closeButton.Add_Click({ $form.Close() })
$form.Controls.Add($closeButton)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
$progress.Visible = $false
$progress.Size = New-Object System.Drawing.Size(620, 18)
$progress.Location = New-Object System.Drawing.Point(27, 195)
$form.Controls.Add($progress)

$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true
$log.ReadOnly = $true
$log.ScrollBars = [System.Windows.Forms.ScrollBars]::Vertical
$log.Font = New-Object System.Drawing.Font("Consolas", 9)
$log.Size = New-Object System.Drawing.Size(620, 220)
$log.Location = New-Object System.Drawing.Point(27, 225)
$form.Controls.Add($log)

function Add-Log([string]$text) {
    $log.AppendText($text + [Environment]::NewLine)
}

$installButton.Add_Click({
    $installButton.Enabled = $false
    $closeButton.Enabled = $false
    $progress.Visible = $true
    Add-Log "Подготовка установки..."

    $mode = switch ($modeBox.SelectedIndex) {
        1 { "fast" }
        2 { "balanced" }
        3 { "power" }
        default { "auto" }
    }

    $worker = New-Object System.ComponentModel.BackgroundWorker
    $worker.add_DoWork({
        param($sender, $eventArgs)
        try {
            $tempScript = Join-Path ([IO.Path]::GetTempPath()) "local-assistant-install.ps1"
            Invoke-WebRequest -Uri $rawInstallerUrl -OutFile $tempScript -UseBasicParsing

            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = "powershell.exe"
            $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$tempScript`" -RepoUrl `"$repoUrl`" -Mode $mode -NonInteractive"
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true

            $process = New-Object System.Diagnostics.Process
            $process.StartInfo = $psi
            [void]$process.Start()
            $stdout = $process.StandardOutput.ReadToEnd()
            $stderr = $process.StandardError.ReadToEnd()
            $process.WaitForExit()
            $eventArgs.Result = [PSCustomObject]@{
                ExitCode = $process.ExitCode
                Output = $stdout
                Error = $stderr
            }
        } catch {
            $eventArgs.Result = [PSCustomObject]@{
                ExitCode = 1
                Output = ""
                Error = $_.Exception.Message
            }
        }
    })
    $worker.add_RunWorkerCompleted({
        param($sender, $eventArgs)
        $result = $eventArgs.Result
        if ($result.Output) { Add-Log $result.Output.Trim() }
        if ($result.ExitCode -eq 0) {
            Add-Log ""
            Add-Log "Установка завершена. Ярлык создан на рабочем столе."
            [System.Windows.Forms.MessageBox]::Show("Помощник установлен.", "Готово", "OK", "Information") | Out-Null
        } else {
            if ($result.Error) { Add-Log ("ОШИБКА: " + $result.Error.Trim()) }
            [System.Windows.Forms.MessageBox]::Show("Установка завершилась с ошибкой. Подробности показаны в окне.", "Ошибка", "OK", "Error") | Out-Null
        }
        $progress.Visible = $false
        $installButton.Enabled = $true
        $closeButton.Enabled = $true
    })
    $worker.RunWorkerAsync()
})

$form.Add_Shown({ $form.Activate() })
[void]$form.ShowDialog()
