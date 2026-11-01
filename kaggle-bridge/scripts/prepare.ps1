<#
.SYNOPSIS
    Prepare the Kaggle bridge package for upload.

.DESCRIPTION
    Validates kernel-metadata.json, builds the dataset bundle that carries the
    `processor` package to Kaggle, and checks that the Kaggle CLI is installed
    and authenticated. It never reads, prints or copies your Kaggle credentials.

.EXAMPLE
    .\prepare.ps1
    .\prepare.ps1 -TestVideo "D:\clips\sample.mp4"
#>
[CmdletBinding()]
param(
    [string]$TestVideo,
    [string]$OutDir = (Join-Path $PSScriptRoot "..\dist")
)

$ErrorActionPreference = "Stop"
$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot = (Resolve-Path (Join-Path $BridgeRoot "..")).Path

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "  ok  $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "  warn $message" -ForegroundColor Yellow }
function Fail($message) { Write-Host "  err $message" -ForegroundColor Red; exit 1 }

Write-Step "Checking the Kaggle CLI"
$kaggle = Get-Command kaggle -ErrorAction SilentlyContinue
if (-not $kaggle) {
    Write-Warn "kaggle CLI not found. Install it with: pip install kaggle"
} else {
    Write-Ok "kaggle at $($kaggle.Source)"
    $credPath = Join-Path $HOME ".kaggle\kaggle.json"
    if (Test-Path $credPath) { Write-Ok "credentials file present (not read, not displayed)" }
    else { Write-Warn "no ~\.kaggle\kaggle.json — run 'kaggle' once or create an API token in the Kaggle UI" }
}

Write-Step "Validating kernel-metadata.json"
$metadataPath = Join-Path $BridgeRoot "kernel\kernel-metadata.json"
if (-not (Test-Path $metadataPath)) { Fail "missing $metadataPath" }
$metadata = Get-Content $metadataPath -Raw | ConvertFrom-Json

$problems = @()
if ($metadata.id -like "*your-kaggle-username*") { $problems += "kernel id still contains the placeholder '<your-kaggle-username>'" }
if ($metadata.code_file -ne "clipwright_worker.py") { $problems += "code_file should be clipwright_worker.py" }
if (-not (Test-Path (Join-Path $BridgeRoot "kernel\$($metadata.code_file)"))) { $problems += "code_file is missing on disk" }
if ($metadata.enable_gpu -ne $true) { $problems += "enable_gpu must be true for GPU benchmarks" }
if ($metadata.machine_shape -ne "NvidiaTeslaT4") { $problems += "machine_shape should be NvidiaTeslaT4" }
if ($metadata.is_private -ne $true) { $problems += "is_private must be true; this bridge handles your media" }
foreach ($dataset in @($metadata.dataset_sources)) {
    if ($dataset -like "*your-kaggle-username*") { $problems += "dataset_sources still contains a placeholder: $dataset" }
}
if (@($metadata.dataset_sources).Count -lt 2) {
    $problems += "expected two dataset_sources: the bridge code and the private input media"
}

if ($problems.Count -gt 0) {
    $problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    Write-Warn "fix the entries above before pushing (push.ps1 will refuse)"
} else {
    Write-Ok "metadata looks usable"
}

Write-Step "Building the dataset bundle"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stage = Join-Path $OutDir "stage"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $stage "kaggle-bridge") | Out-Null

Copy-Item (Join-Path $BridgeRoot "processor") -Destination (Join-Path $stage "kaggle-bridge") -Recurse
Copy-Item (Join-Path $BridgeRoot "config") -Destination (Join-Path $stage "kaggle-bridge") -Recurse
Copy-Item (Join-Path $BridgeRoot "README.md") -Destination (Join-Path $stage "kaggle-bridge") -Recurse
Copy-Item (Join-Path $BridgeRoot "requirements.txt") -Destination (Join-Path $stage "kaggle-bridge") -Recurse

# Never ship caches or bytecode to Kaggle.
Get-ChildItem $stage -Recurse -Directory -Include "__pycache__", ".pytest_cache" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$codeZip = Join-Path $OutDir "clipwright-bridge-code.zip"
if (Test-Path $codeZip) { Remove-Item $codeZip -Force }
Compress-Archive -Path (Join-Path $stage "kaggle-bridge") -DestinationPath $codeZip
Write-Ok "code bundle: $codeZip ($([math]::Round((Get-Item $codeZip).Length / 1MB, 2)) MB)"

if ($TestVideo) {
    Write-Step "Preparing the private input bundle"
    if (-not (Test-Path $TestVideo)) { Fail "test video not found: $TestVideo" }
    $video = Get-Item $TestVideo
    $sizeGb = [math]::Round($video.Length / 1GB, 2)
    if ($sizeGb -gt 2.5) { Write-Warn "video is $sizeGb GB; Kaggle dataset uploads are slow and the bridge caps downloads at 2 GiB by default" }

    $hash = (Get-FileHash -Algorithm SHA256 -Path $video.FullName).Hash.ToLower()
    Write-Ok "sha256 $hash"

    $inputStage = Join-Path $OutDir "input-stage"
    if (Test-Path $inputStage) { Remove-Item $inputStage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $inputStage | Out-Null
    Copy-Item $video.FullName -Destination (Join-Path $inputStage $video.Name)

    $job = Get-Content (Join-Path $BridgeRoot "config\sample-job.json") -Raw | ConvertFrom-Json
    $job.source.filename = $video.Name
    $job.source.size_bytes = $video.Length
    $job.source.sha256 = $hash
    $job | ConvertTo-Json -Depth 10 | Set-Content -Path (Join-Path $inputStage "job.json") -Encoding UTF8

    $inputZip = Join-Path $OutDir "clipwright-phase1-input.zip"
    if (Test-Path $inputZip) { Remove-Item $inputZip -Force }
    Compress-Archive -Path (Join-Path $inputStage "*") -DestinationPath $inputZip
    Write-Ok "input bundle: $inputZip"
    Write-Host "     upload it as a PRIVATE dataset named clipwright-phase1-input" -ForegroundColor DarkGray
}

Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $OutDir "input-stage") -Recurse -Force -ErrorAction SilentlyContinue

Write-Step "Next steps"
Write-Host @"
  1. Upload $codeZip as a private Kaggle dataset 'clipwright-bridge-code'
  2. Upload the input bundle as a private dataset 'clipwright-phase1-input'
  3. Fill in your username in kernel\kernel-metadata.json
  4. .\push.ps1
"@
