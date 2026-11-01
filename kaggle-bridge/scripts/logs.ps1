<#
.SYNOPSIS
    Show the run log of the last execution.

.DESCRIPTION
    The Kaggle CLI has no dedicated "logs" command: the execution log is part of
    the kernel output bundle, so this script downloads the output to a scratch
    folder and prints the log file if Kaggle produced one.

.EXAMPLE
    .\logs.ps1
    .\logs.ps1 -Tail 200
#>
[CmdletBinding()]
param(
    [string]$KernelId,
    [int]$Tail = 120
)

$ErrorActionPreference = "Stop"
$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$MetadataPath = Join-Path $BridgeRoot "kernel\kernel-metadata.json"
if (-not $KernelId) {
    if (-not (Test-Path $MetadataPath)) { Write-Host "no kernel-metadata.json; pass -KernelId" -ForegroundColor Red; exit 1 }
    $KernelId = (Get-Content $MetadataPath -Raw | ConvertFrom-Json).id
}

$scratch = Join-Path $env:TEMP ("clipwright-logs-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $scratch | Out-Null

Write-Host "Downloading output bundle for $KernelId ..." -ForegroundColor Cyan
kaggle kernels output $KernelId -p $scratch
if ($LASTEXITCODE -ne 0) { Write-Host "could not fetch output (kernel may still be running)" -ForegroundColor Yellow; exit $LASTEXITCODE }

$log = Get-ChildItem $scratch -Recurse -File -Include "*.log", "*.txt" |
    Sort-Object Length -Descending |
    Select-Object -First 1
if ($log) {
    Write-Host ""
    Write-Host "--- last $Tail lines of $($log.FullName) ---" -ForegroundColor Cyan
    Get-Content $log.FullName -Tail $Tail
} else {
    Write-Host "no .log/.txt in the bundle; execution logs are only kept while the run is fresh." -ForegroundColor Yellow
    Write-Host "Falling back to status:" -ForegroundColor DarkGray
    kaggle kernels status $KernelId
}
Write-Host ""
Write-Host "Scratch folder: $scratch" -ForegroundColor DarkGray
