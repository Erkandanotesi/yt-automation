$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$automationRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$toolsRoot = Join-Path $automationRoot "tools"
$target = Join-Path $toolsRoot "ffmpeg"
$existing = Get-ChildItem -LiteralPath $target -Recurse -Filter "ffmpeg.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1

if ($existing) {
    Write-Host "FFmpeg already installed: $($existing.FullName)"
    exit 0
}

$downloadRoot = Join-Path ([IO.Path]::GetTempPath()) "news-studio-ffmpeg-$PID"
$zipPath = Join-Path $downloadRoot "ffmpeg-release-essentials.zip"
$checksumPath = "$zipPath.sha256"
$staging = Join-Path $toolsRoot "ffmpeg.installing-$PID"

$archiveUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$checksumUrl = "$archiveUrl.sha256"

try {
    if (Test-Path -LiteralPath $target) {
        throw "FFmpeg target exists but contains no ffmpeg.exe: $target"
    }

    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null

    Write-Host "Downloading the FFmpeg release essentials build..."
    & curl.exe --fail --location --output $zipPath $archiveUrl
    if ($LASTEXITCODE -ne 0) {
        throw "FFmpeg archive download failed with exit code $LASTEXITCODE."
    }

    & curl.exe --fail --location --output $checksumPath $checksumUrl
    if ($LASTEXITCODE -ne 0) {
        throw "FFmpeg checksum download failed with exit code $LASTEXITCODE."
    }

    $expected = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split "\s+")[0]
    $actual = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    if ($actual -ne $expected) {
        throw "FFmpeg SHA-256 verification failed."
    }

    Expand-Archive -LiteralPath $zipPath -DestinationPath $staging
    $ffmpeg = Get-ChildItem -LiteralPath $staging -Recurse -Filter "ffmpeg.exe" |
        Select-Object -First 1
    $ffprobe = Get-ChildItem -LiteralPath $staging -Recurse -Filter "ffprobe.exe" |
        Select-Object -First 1
    if (-not $ffmpeg -or -not $ffprobe) {
        throw "The verified archive did not contain ffmpeg.exe and ffprobe.exe."
    }

    Move-Item -LiteralPath $staging -Destination $target
    Write-Host "FFmpeg installed portably: $target"
}
finally {
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd("\") + "\"
    $resolvedDownload = [IO.Path]::GetFullPath($downloadRoot)
    if (
        (Test-Path -LiteralPath $resolvedDownload) -and
        $resolvedDownload.StartsWith(
            $tempRoot,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        Remove-Item -LiteralPath $resolvedDownload -Recurse -Force
    }
}
