# ================================================================
#  Windows タスクスケジューラへの登録スクリプト
#
#  方式 A (既定): 毎日 08:20 に「当日ループ (run)」を起動
#      - 開催がない日はレース情報を確認してすぐ終了するため、祝日開催にも対応
#  方式 B: -Mode Interval で 08:30～17:00 の間 10 分おきに「collect」を起動
#
#  実行例 (管理者 PowerShell):
#    powershell -ExecutionPolicy Bypass -File register_task.ps1
#    powershell -ExecutionPolicy Bypass -File register_task.ps1 -Mode Interval
# ================================================================
param(
    [ValidateSet("Daily", "Interval")] [string]$Mode = "Daily",
    [string]$TaskName = "JVOddsCollector",
    [string]$StartTime = "08:20"
)

$bat = Join-Path $PSScriptRoot "run_collector.bat"

if ($Mode -eq "Daily") {
    $action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`" run"
    $trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
    $limit   = New-TimeSpan -Hours 10
} else {
    $action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`" collect"
    $trigger = New-ScheduledTaskTrigger -Daily -At "08:30"
    $rep     = New-ScheduledTaskTrigger -Once -At "08:30" `
                 -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Hours 8 -Minutes 30)
    $trigger.Repetition = $rep.Repetition
    $limit   = New-TimeSpan -Minutes 9
}

# 多重起動しない・失敗時は 5 分後に最大 3 回再起動・ネットワーク接続時のみ
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit $limit `
    -StartWhenAvailable -RunOnlyIfNetworkAvailable

# JV-Link は対話ユーザーのセッションで動かす必要があるため、ログオン中ユーザーで実行
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "タスク '$TaskName' を登録しました (Mode=$Mode)"
