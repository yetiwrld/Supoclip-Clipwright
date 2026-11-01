<#
.SYNOPSIS
    Show the status of the private Kaggle kernel.
#>
[CmdletBinding()]
param([string]$KernelId)

$ErrorActionPreference = "Stop"
$MetadataPath = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "kernel\kernel-metadata.json"
if (-not $KernelId) {
    if (-not (Test-Path $MetadataPath)) { Write-Host "no kernel-metadata.json; pass -KernelId" -ForegroundColor Red; exit 1 }
    $KernelId = (Get-Content $MetadataPath -Raw | ConvertFrom-Json).id
}
Write-Host "Kernel: $KernelId" -ForegroundColor Cyan
kaggle kernels status $KernelId
