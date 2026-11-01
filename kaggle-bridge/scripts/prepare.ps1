<#
.SYNOPSIS
    Prepare the Kaggle bridge package for upload.

.DESCRIPTION
    Validates kernel-metadata.json (failing immediately on errors), builds the two
    private datasets, and reports Kaggle CLI credential *presence*. It never reads,
    prints or copies your credential values.

    Archives are written by Python's zipfile module with ZIP64 enabled, because
    Compress-Archive cannot store a file larger than 2 GB. The input bundle has an
    explicit size ceiling ($MaxInputGb, default 4) that fails *before* anything is
    packaged.

.PARAMETER TestVideo
    Optional local video to bundle as the private input dataset.

.PARAMETER MaxInputGb
    Hard ceiling for the packaged input video, in GB. Default 4.

.PARAMETER AllowMetadataErrors
    Build the bundles even when kernel-metadata.json is invalid. Intended only for
    the chicken-and-egg first run, where you build the datasets before you know
    their ids. push.ps1 always refuses invalid metadata.

.EXAMPLE
    .\prepare.ps1
    .\prepare.ps1 -TestVideo "D:\clips\sample.mp4"
#>
[CmdletBinding()]
param(
    [string]$TestVideo,
    [string]$OutDir = (Join-Path $PSScriptRoot "..\dist"),
    [double]$MaxInputGb = 4,
    [switch]$AllowMetadataErrors
)

$ErrorActionPreference = "Stop"
$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "  ok  $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "  warn $message" -ForegroundColor Yellow }
function Fail($message) { Write-Host "  err $message" -ForegroundColor Red; exit 1 }

# --------------------------------------------------------------------------- #
# Python is required: the Kaggle CLI is a Python package, and the packaging
# safeguards live in processor/packaging.py so they can be unit-tested.
# --------------------------------------------------------------------------- #
$Python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $Python) { $Python = (Get-Command python3 -ErrorAction SilentlyContinue) }
if (-not $Python) { Fail "Python 3 is required (the kaggle CLI is a Python package). Install Python 3.9+ and retry." }
$PythonExe = $Python.Source
Write-Ok "python: $PythonExe"

# Runs a processor.* module entry point with cwd = bridge root and returns its
# exit code. Output goes straight to the console (Out-Host), so the only value on
# this function's output stream is the integer exit code. $ErrorActionPreference
# is relaxed around the call because PowerShell 5.1 turns a native command's
# stderr into a terminating error when it is redirected under `Stop`.
function Invoke-BridgePython {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$BridgeArgs)
    Push-Location $BridgeRoot
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $PythonExe -m @BridgeArgs 2>&1 | Out-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
        Pop-Location
    }
    return [int]$code
}

Write-Step "Packaging limits"
$limitsExit = Invoke-BridgePython processor.packaging limits
if ($limitsExit -ne 0) { Fail "could not read the packaging limits; is processor/ present?" }

Write-Step "Checking the Kaggle CLI"
$kaggle = Get-Command kaggle -ErrorAction SilentlyContinue
if (-not $kaggle) {
    Write-Warn "kaggle CLI not found. Install it with: pip install kaggle"
} else {
    Write-Ok "kaggle at $($kaggle.Source)"
    $authExit = Invoke-BridgePython processor.kaggle_cli describe
    if ($authExit -ne 0) {
        Write-Warn "credentials missing or incomplete — create an API token: Kaggle UI -> Account -> Settings -> API"
    }
    Write-Host "     run 'python -m processor.kaggle_cli verify-auth' to prove the token actually works" -ForegroundColor DarkGray
}

# --------------------------------------------------------------------------- #
# metadata — an error stops the script here, it is not a warning
# --------------------------------------------------------------------------- #
Write-Step "Validating kernel-metadata.json"
$metadataPath = Join-Path $BridgeRoot "kernel\kernel-metadata.json"
if (-not (Test-Path $metadataPath)) { Fail "missing $metadataPath; run: python -m processor.packaging check-metadata --metadata kernel\kernel-metadata.json --kernel-dir kernel" }

$metadataExit = Invoke-BridgePython processor.packaging check-metadata --metadata $metadataPath --kernel-dir (Join-Path $BridgeRoot "kernel")
if ($metadataExit -ne 0) {
    if ($AllowMetadataErrors) {
        Write-Warn "kernel-metadata.json has errors; continuing because -AllowMetadataErrors was given"
        Write-Warn "push.ps1 will refuse this metadata until the errors above are fixed"
    } else {
        Fail "kernel-metadata.json is invalid (see the errors above). Fix them, or pass -AllowMetadataErrors if you are only building the datasets."
    }
} else {
    Write-Ok "metadata valid"
}

# --------------------------------------------------------------------------- #
# code bundle
# --------------------------------------------------------------------------- #
Write-Step "Building the code bundle"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stage = Join-Path $OutDir "stage"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $stage "kaggle-bridge") | Out-Null

foreach ($item in @("processor", "config", "README.md", "requirements.txt")) {
    $source = Join-Path $BridgeRoot $item
    if (-not (Test-Path $source)) { Fail "missing $source" }
    Copy-Item $source -Destination (Join-Path $stage "kaggle-bridge") -Recurse
}

$codeZip = Join-Path $OutDir "clipwright-bridge-code.zip"
$codeExit = Invoke-BridgePython processor.packaging zip-tree --source (Join-Path $stage "kaggle-bridge") --destination $codeZip --arc-root "kaggle-bridge"
if ($codeExit -ne 0) { Fail "code bundle could not be built" }
Write-Ok "code bundle: $codeZip"

# --------------------------------------------------------------------------- #
# input bundle
# --------------------------------------------------------------------------- #
if ($TestVideo) {
    Write-Step "Preparing the private input bundle"
    if (-not (Test-Path $TestVideo)) { Fail "test video not found: $TestVideo" }
    $video = Get-Item $TestVideo

    $videoGb = [math]::Round($video.Length / 1GB, 3)
    if ($videoGb -gt $MaxInputGb) {
        Fail ("test video is {0} GB, above the {1} GB ceiling. Use a shorter clip, or raise it explicitly with -MaxInputGb." -f $videoGb, $MaxInputGb)
    }

    $hash = (Get-FileHash -Algorithm SHA256 -Path $video.FullName).Hash.ToLower()
    Write-Ok "sha256 $hash"
    Write-Ok ("size {0:N2} MB" -f ($video.Length / 1MB))

    $jobTmp = Join-Path $OutDir "job.json"
    $job = Get-Content (Join-Path $BridgeRoot "config\sample-job.json") -Raw | ConvertFrom-Json
    $job.source.filename = $video.Name
    $job.source.size_bytes = $video.Length
    $job.source.sha256 = $hash
    # Write BOM-free UTF-8: PowerShell 5.1's Set-Content -Encoding UTF8 adds a BOM,
    # and a BOM breaks strict JSON parsers (the reader tolerates it, but a clean
    # file is the better default).
    $jobJson = $job | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($jobTmp, $jobJson, (New-Object System.Text.UTF8Encoding($false)))

    $inputZip = Join-Path $OutDir "clipwright-phase1-input.zip"
    # zip-input enforces the ceiling again (before writing) and uses ZIP64.
    $maxArg = $MaxInputGb.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    $inputExit = Invoke-BridgePython processor.packaging zip-input --video $video.FullName --job $jobTmp --destination $inputZip --max-gb $maxArg
    if ($inputExit -ne 0) { Fail "input bundle could not be built" }
    Write-Ok "input bundle: $inputZip"

    Remove-Item $jobTmp -Force -ErrorAction SilentlyContinue
} else {
    Write-Warn "no -TestVideo given; the private input dataset was not built"
}

Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $OutDir "input-stage") -Recurse -Force -ErrorAction SilentlyContinue

Write-Step "Next steps"
Write-Host @"
  1. Upload $codeZip  as a PRIVATE dataset 'clipwright-bridge-code'
  2. Upload $([System.IO.Path]::Combine($OutDir, 'clipwright-phase1-input.zip')) as a PRIVATE dataset 'clipwright-phase1-input'
     Dataset privacy is separate from kernel privacy: a public dataset is public
     even when the notebook that mounts it is private.
  3. Fill in your username and dataset ids in kernel\kernel-metadata.json
  4. .\push.ps1
"@
exit 0
