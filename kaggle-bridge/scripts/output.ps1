<#
.SYNOPSIS
    Download the kernel outputs and verify them against result.json.

.DESCRIPTION
    Fetches /kaggle/working into a timestamped folder, then re-computes every
    SHA-256 in result.json. A checksum mismatch means the transfer or the kernel
    is wrong and the artefacts must not be trusted.

.EXAMPLE
    .\output.ps1
#>
[CmdletBinding()]
param(
    [string]$KernelId,
    [string]$OutRoot = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "runs")
)

$ErrorActionPreference = "Stop"
$MetadataPath = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "kernel\kernel-metadata.json"
if (-not $KernelId) {
    if (-not (Test-Path $MetadataPath)) { Write-Host "no kernel-metadata.json; pass -KernelId" -ForegroundColor Red; exit 1 }
    $KernelId = (Get-Content $MetadataPath -Raw | ConvertFrom-Json).id
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $OutRoot $stamp
New-Item -ItemType Directory -Force -Path $target | Out-Null

Write-Host "Downloading outputs to $target ..." -ForegroundColor Cyan
kaggle kernels output $KernelId -p $target
if ($LASTEXITCODE -ne 0) { Write-Host "download failed" -ForegroundColor Red; exit $LASTEXITCODE }

$resultFile = Get-ChildItem $target -Recurse -File -Filter "result.json" | Select-Object -First 1
if (-not $resultFile) {
    Write-Host "no result.json in the bundle; the run probably failed before Gate F" -ForegroundColor Yellow
    Get-ChildItem $target -Recurse -File | Select-Object FullName, Length | Format-Table -AutoSize
    exit 0
}

$result = Get-Content $resultFile.FullName -Raw | ConvertFrom-Json
Write-Host ""
Write-Host "status: $($result.status)  job: $($result.job_id)" -ForegroundColor Cyan
if ($result.error) { Write-Host "error: $($result.error.message)" -ForegroundColor Red }

$root = $resultFile.Directory.FullName
$bad = 0
foreach ($item in @($result.outputs)) {
    $candidate = Join-Path $root $item.path
    if (-not (Test-Path $candidate)) {
        # Absolute Kaggle paths do not exist locally; fall back to the recorded name.
        $candidate = Join-Path $root $item.name
    }
    if (-not (Test-Path $candidate)) { Write-Host "  MISSING $($item.name)" -ForegroundColor Red; $bad++; continue }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $candidate).Hash.ToLower()
    if ($actual -ne $item.sha256) { Write-Host "  MISMATCH $($item.name)" -ForegroundColor Red; $bad++ }
    else { Write-Host "  ok $($item.name) ($([math]::Round((Get-Item $candidate).Length / 1MB, 2)) MB)" -ForegroundColor Green }
}
if ($bad -gt 0) { Write-Host "$bad artefact(s) failed verification" -ForegroundColor Red; exit 2 }
Write-Host "All artefacts verified." -ForegroundColor Green
Write-Host "Report: $(Join-Path $root 'REPORT.md')" -ForegroundColor DarkGray
