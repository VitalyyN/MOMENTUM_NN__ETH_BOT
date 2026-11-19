$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Сначала подготовьте окружение через start.sh.'
}
if (Get-ScheduledTask -TaskName 'Momentum60_NN_bot' -ErrorAction SilentlyContinue) {
    throw 'Задание Momentum60_NN_bot уже зарегистрировано. Его настройки доступны в планировщике.'
}
$options = @{
    ExecutionTimeLimit = [TimeSpan]::Zero
    MultipleInstances = 'IgnoreNew'
    RestartCount = 3
    RestartInterval = New-TimeSpan -Minutes 1
    StartWhenAvailable = $true
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
}
$task = @{
    TaskName = 'Momentum60_NN_bot'
    Action = New-ScheduledTaskAction -Execute $python -Argument '-X utf8 -B main.py' -WorkingDirectory $root
    Trigger = New-ScheduledTaskTrigger -AtStartup
    Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
    Settings = New-ScheduledTaskSettingsSet @options
    Description = 'Momentum60 MAJORITY, Bybit ETHUSDT и Telegram'
}
Register-ScheduledTask @task
