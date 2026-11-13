[CmdletBinding()]
param(
    [string]$StateFile,
    [string]$CreateVideoFile,
    [DateTimeOffset]$Now = [DateTimeOffset]::Now,
    [switch]$NoLaunch,
    [switch]$Report
)

$ErrorActionPreference = 'Stop'
$thresholdHours = 16

try {
    if ([string]::IsNullOrWhiteSpace($StateFile)) {
        $StateFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'data\youtube_schedule_state.json'
    }
    if ([string]::IsNullOrWhiteSpace($CreateVideoFile)) {
        $CreateVideoFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'create_video.bat'
    }

    if (-not (Test-Path -LiteralPath $StateFile -PathType Leaf)) {
        throw "YouTube schedule state was not found: $StateFile"
    }

    $state = Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $rawPlannedAt = [string]$state.last_scheduled_publish_at
    if ([string]::IsNullOrWhiteSpace($rawPlannedAt)) {
        throw 'last_scheduled_publish_at is missing from the YouTube schedule state.'
    }
    if ($rawPlannedAt -notmatch '(Z|[+-]\d{2}:\d{2})$') {
        throw 'last_scheduled_publish_at must include a timezone.'
    }

    $plannedAt = [DateTimeOffset]::Parse(
        $rawPlannedAt,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AllowWhiteSpaces
    )
    $hoursUntilPlanned = ($plannedAt - $Now).TotalHours
    $isDue = $hoursUntilPlanned -le $thresholdHours

    if ($Report) {
        [pscustomobject]@{
            action = if ($isDue) { 'launch' } else { 'nothing' }
            planned_at = $plannedAt.ToString('o')
            checked_at = $Now.ToString('o')
            hours_until_planned = $hoursUntilPlanned
            threshold_hours = $thresholdHours
        } | ConvertTo-Json -Compress
    }

    if ($isDue -and -not $NoLaunch) {
        if (-not (Test-Path -LiteralPath $CreateVideoFile -PathType Leaf)) {
            throw "Video launcher was not found: $CreateVideoFile"
        }

        Start-Process `
            -FilePath $env:ComSpec `
            -ArgumentList @('/d', '/c', ('call "{0}"' -f $CreateVideoFile)) `
            -WorkingDirectory (Split-Path -Parent $PSScriptRoot) `
            -WindowStyle Normal
    }

    exit 0
}
catch {
    # Scheduled checks are intentionally silent. Task Scheduler still records failure.
    exit 1
}
