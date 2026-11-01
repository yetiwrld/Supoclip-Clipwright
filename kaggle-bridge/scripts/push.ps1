<#
.SYNOPSIS
    Push the private Kaggle kernel and start a run.

.DESCRIPTION
    Refuses to push when the metadata still has placeholders, the code file is
    missing, or the kernel is not private. Never prints credentials.

.EXAMPLE
    .\push.ps1
    .\push.ps1 -Wait
#>
[CmdletBinding()]
param(
    [switch]$Wait,
    [int]$PollSeconds = 60
)

$ErrorActionPreference = "Stop"
$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$KernelDir = Join-Path $BridgeRoot "kernel"
$MetadataPath = Join-Path $KernelDir "kernel-metadata.json"

function Write-Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

if (-not (Get-Command kaggle -ErrorAction SilentlyContinue)) {
    Write-Host "kaggle CLI not installed. Run: pip install kaggle" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $MetadataPath)) { Write-Host "missing kernel-metadata.json" -ForegroundColor Red; exit 1 }

$metadata = Get-Content $MetadataPath -Raw | ConvertFrom-Json
$blockers = @()
if ($metadata.id -like "*your-kaggle-username*") { $blockers += "kernel id still has the <your-kaggle-username> placeholder" }
if (-not (Test-Path (Join-Path $KernelDir $metadata.code_file))) { $blockers += "code_file not found: $($metadata.code_file)" }
if ($metadata.is_private -ne $true) { $blockers += "is_private must be true" }
foreach ($dataset in @($metadata.dataset_sources)) {
    if ($dataset -like "*your-kaggle-username*") { $blockers += "dataset_sources placeholder: $dataset" }
}
if ($blockers.Count -gt 0) {
    $blockers | ForEach-Object { Write-Host "  blocked: $_" -ForegroundColor Yellow }
    exit 1
}

Write-Step "Pushing $($metadata.id) ($($metadata.language), gpu=$($metadata.enable_gpu), shape=$($metadata.machine_shape))"
kaggle kernels push -p $KernelDir
if ($LASTEXITCODE -ne 0) { Write-Host "push failed" -ForegroundColor Red; exit $LASTEXITCODE }

if ($Wait) {
    Write-Step "Waiting for completion (Ctrl+C to stop watching; the kernel keeps running)"
    while ($true) {
        $statusOutput = kaggle kernels status $metadata.id 2>&1 | Out-String
        $statusOutput.Trim() | Write-Host
        if ($statusOutput -match "complete|error|failed|cancelled") { break }
        Start-Sleep -Seconds $PollSeconds
    }
    Write-Step "Fetching outputs"
    & (Join-Path $PSScriptRoot "output.ps1")
}

Write-Host ""
Write-Host "Check progress: .\status.ps1" -ForegroundColor DarkGray
Write-Host "Fetch results:  .\output.ps1" -ForegroundColor DarkGray
