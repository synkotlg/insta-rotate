# setup.ps1 - installe les dependances et cree la tache planifiee.
#   .\setup.ps1                  installation + tache (a chaque ouverture de session et chaque jour a 09:00,
#                                puis toutes les heures jusqu'a 23:00 ; heures de Paris, quel que soit le fuseau du PC)
#   .\setup.ps1 -Time 18:30      autre heure (de Paris) pour le declenchement quotidien
#   A relancer apres tout deplacement du dossier : la tache contient le chemin complet du script.
#   .\setup.ps1 -DailyOnly       pas de declenchement a l'ouverture de session
#   .\setup.ps1 -NoTask          dependances seulement
#   .\setup.ps1 -Remove          supprime la tache planifiee
param(
    [string]$Time = "09:00",
    [switch]$DailyOnly,
    [switch]$NoTask,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$taskName = "InstaRotate"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Tache $taskName supprimee."
    return
}

Set-Location $root
if (-not (Test-Path ".venv")) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
}
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
if (-not (Test-Path "config.json")) { Copy-Item "config.example.json" "config.json" }

if (-not $NoTask) {
    $pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$root\insta_rotate.py`" run" -WorkingDirectory $root
    # $Time et 23:00 sont des heures de Paris (horloge du script). La tache garde un instant fixe (UTC) : un
    # changement de fuseau du PC ne la decale pas, l'heure d'ete de Paris si (une heure). Debut a $Time heure d'ete,
    # fin a 23:00 heure d'hiver : en hiver le premier passage a lieu une heure plus tot, le script ignore ceux qui
    # tombent hors de sa plage horaire.
    $paris = [TimeZoneInfo]::FindSystemTimeZoneById("Romance Standard Time")
    $offsets = @(1, 7) | ForEach-Object { $paris.GetUtcOffset((New-Object DateTime (Get-Date).Year, $_, 15)) } | Sort-Object
    $first = [TimeSpan]::Parse($Time) - $offsets[-1]
    $last = [TimeSpan]::Parse("23:00") - $offsets[0]
    $at = ([DateTime]::SpecifyKind([DateTime]::UtcNow.Date, "Utc") + $first).ToLocalTime()
    $daily = New-ScheduledTaskTrigger -Daily -At $at -RandomDelay (New-TimeSpan -Minutes 45)
    # relance toutes les heures jusqu'a 23:00 (fin de active_hours par defaut) : un passage rate (PC eteint ou en
    # veille), hors plage ou interrompu par une erreur reprend dans l'heure. Sans risque de rafale : temps du jour,
    # quotas et plafonds horaires sont cumules sur tous les passages, et un passage sans travail n'ouvre pas le navigateur.
    $hours = [Math]::Max(1, [Math]::Ceiling(($last - $first).TotalHours))
    $daily.Repetition = (New-ScheduledTaskTrigger -Once -At $at -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration (New-TimeSpan -Hours $hours)).Repetition
    $triggers = @($daily)
    if (-not $DailyOnly) {
        # PC allume plus tard dans la journee : le passage demarre 5 min apres l'ouverture de session
        $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
        $logon.Delay = "PT5M"
        $triggers += $logon
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 10)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
    $sched = "chaque jour a $($at.ToString('HH:mm')) heure du PC ($Time a Paris l'ete, une heure plus tot l'hiver), " +
        "plus un delai aleatoire jusqu'a 45 min, puis toutes les heures pendant $hours h"
    $when = if ($DailyOnly) { $sched } else { "5 min apres chaque ouverture de session et $sched" }
    Write-Host "Tache $taskName creee : $when."
}

Write-Host ""
Write-Host "Etape suivante (une seule fois) : connexion a Instagram"
Write-Host "  .\.venv\Scripts\python.exe insta_rotate.py login"
if (-not (Test-Path ".env")) {
    Write-Host ""
    Write-Host "Notifications Telegram (optionnel) : TELEGRAM_BOT_TOKEN=... et TELEGRAM_CHAT_ID=... dans .env"
}
