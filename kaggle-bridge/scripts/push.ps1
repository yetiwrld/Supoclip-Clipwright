<#
.SYNOPSIS
    Push the private Kaggle kernel and start a run.

.DESCRIPTION
    Refuses to push unless every one of these holds:

      * kernel-metadata.json is valid (privacy, GPU, T4 shape, internet, two
        datasets, no placeholders, worker file present) — checked by
        processor/packaging.py, the same code the tests exercise;
      * the Kaggle CLI is installed and its credentials are PRESENT;
      * those credentials actually authenticate against the Kaggle API;
      * both configured datasets exist in this account.

    Presence of ~/.kaggle/kaggle.json is never treated as proof of authentication.
    Credential values are never read or printed; the CLI's output passes through a
    redactor before it is shown.

.EXAMPLE
    .\push.ps1
    .\push.ps1 -Wait
#>
[CmdletBinding()]
param(
    [switch]$Wait,
    [int]$PollSeconds = 60,
    [switch]$SkipDatasetCheck
)

$ErrorActionPreference = "Stop"
$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$KernelDir = Join-Path $BridgeRoot "kernel"
$MetadataPath = Join-Path $KernelDir "kernel-metadata.json"

function Write-Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok($m) { Write-Host "  ok  $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "  warn $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  err $m" -ForegroundColor Red; exit 1 }

$Python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $Python) { $Python = (Get-Command python3 -ErrorAction SilentlyContinue) }
if (-not $Python) { Fail "Python 3 is required (the kaggle CLI is a Python package)." }
$PythonExe = $Python.Source

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

$kaggle = Get-Command kaggle -ErrorAction SilentlyContinue
if (-not $kaggle) { Fail "kaggle CLI not installed. Run: pip install kaggle" }

if (-not (Test-Path $MetadataPath)) { Fail "missing $MetadataPath" }

# --------------------------------------------------------------------------- #
# 1. metadata: privacy, GPU, shape, internet, datasets, placeholders, code file
# --------------------------------------------------------------------------- #
Write-Step "Validating kernel-metadata.json (strict)"
$metadataExit = Invoke-BridgePython processor.packaging check-metadata --metadata $MetadataPath --kernel-dir $KernelDir --strict
if ($metadataExit -ne 0) { Fail "metadata is not pushable; fix the errors above (nothing was uploaded)" }
$metadata = Get-Content $MetadataPath -Raw | ConvertFrom-Json

# --------------------------------------------------------------------------- #
# 2. credentials present, then actually valid
# --------------------------------------------------------------------------- #
Write-Step "Checking Kaggle credentials"
$describeExit = Invoke-BridgePython processor.kaggle_cli describe
if ($describeExit -ne 0) {
    Fail "no usable credentials file. Kaggle UI -> Account -> Settings -> API -> Create New Token, then save it as ~\.kaggle\kaggle.json"
}

Write-Step "Verifying authentication with the Kaggle API"
$authExit = Invoke-BridgePython processor.kaggle_cli verify-auth
if ($authExit -ne 0) {
    Fail "the Kaggle API rejected this session; nothing was uploaded. Re-create the API token and retry."
}

# --------------------------------------------------------------------------- #
# 3. both datasets exist, and the kernel is private
# --------------------------------------------------------------------------- #
if (-not $SkipDatasetCheck) {
    Write-Step "Checking the configured datasets exist in this account"
    $refs = @()
    foreach ($dataset in @($metadata.dataset_sources)) { $refs += $dataset }
    $datasetArgs = @("processor.kaggle_cli", "verify-datasets")
    foreach ($ref in $refs) { $datasetArgs += @("--ref", $ref) }
    $datasetExit = Invoke-BridgePython @datasetArgs
    if ($datasetExit -ne 0) {
        Fail "a configured dataset is missing from this account; nothing was uploaded"
    }
    Write-Warn "dataset privacy cannot be read from the API: confirm BOTH datasets are private in the Kaggle UI"
}

# --------------------------------------------------------------------------- #
# 4. push
# --------------------------------------------------------------------------- #
Write-Step "Pushing $($metadata.id) (private=$($metadata.is_private), gpu=$($metadata.enable_gpu), shape=$($metadata.machine_shape), internet=$($metadata.enable_internet))"
& kaggle kernels push -p $KernelDir
if ($LASTEXITCODE -ne 0) { Fail "kaggle kernels push failed" }

if ($Wait) {
    Write-Step "Waiting for completion (Ctrl+C stops watching; the kernel keeps running)"
    while ($true) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"   # PS 5.1 + redirected native stderr
        try {
            $statusOutput = kaggle kernels status $metadata.id 2>&1 | Out-String
        } finally {
            $ErrorActionPreference = $previous
        }
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
exit 0
