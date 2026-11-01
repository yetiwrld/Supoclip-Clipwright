<#
ClipWright Studio - one-time setup for Windows 11.

Runs, in order:
  1. Prerequisite check (Python 3.11+, Docker Desktop, FFmpeg, Ollama)
  2. .env creation with local-AI defaults (existing values are never overwritten)
  3. Optional one-time model downloads (Whisper checkpoint, Ollama model)
  4. docker compose build + start
  5. Backend health and local-AI readiness verification via /local-ai/status

Model downloads only happen when you pass -InstallModels, so nothing is
fetched behind your back on a later launch.

Examples
  powershell -ExecutionPolicy Bypass -File scripts\setup-clipwright.ps1 -Check
  powershell -ExecutionPolicy Bypass -File scripts\setup-clipwright.ps1 -InstallModels
  powershell -ExecutionPolicy Bypass -File scripts\setup-clipwright.ps1 -InstallModels -WhisperModel base
#>

[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$InstallModels,
    [string]$WhisperModel = "",
    [string]$OllamaModel = "",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Write-Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "  [ ok ] $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "  [warn] $text" -ForegroundColor Yellow }
function Write-Bad($text) { Write-Host "  [fail] $text" -ForegroundColor Red }

$failures = 0

Write-Host "ClipWright Studio - Windows 11 setup" -ForegroundColor Magenta
Write-Host "Repository: $repo"

Write-Step "Checking prerequisites"

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    $pyVersion = & python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
    Write-Ok "Python $pyVersion"
} else {
    Write-Bad "Python 3.11+ not found. Install from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
    $script:failures++
}

if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Ok "FFmpeg on PATH"
} else {
    Write-Warn2 "FFmpeg not on PATH. The Docker images include it; only native runs need it."
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    try {
        $null = & docker compose version 2>&1
        Write-Ok "Docker Desktop with the compose plugin"
    } catch {
        Write-Bad "docker compose is unavailable. Update Docker Desktop."
        $script:failures++
    }
} else {
    Write-Bad "Docker Desktop not found: https://www.docker.com/products/docker-desktop/"
    $script:failures++
}

if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Write-Ok "Ollama installed"
} else {
    Write-Warn2 "Ollama not found. Install https://ollama.com/download, then run 'ollama serve'."
}

Write-Step "Writing local-AI defaults to .env"
if ($python) {
    $envArgs = @("scripts\setup_local_ai.py", "--write-env")
    if ($WhisperModel) { $envArgs += @("--whisper-model", $WhisperModel) }
    & python @envArgs | Out-Host
} else {
    Write-Warn2 "Skipped: Python is required."
}

if ($InstallModels) {
    Write-Step "Downloading local models (one-time)"
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Bad "Ollama is required for -InstallModels."
        $script:failures++
    } else {
        & ollama list | Out-Host
        $pull = (Get-Content .env -ErrorAction SilentlyContinue | Select-String '^LLM=ollama:(.+)$' | Select-Object -First 1)
        $model = $OllamaModel
        if (-not $model -and $pull) { $model = $pull.Matches[0].Groups[1].Value }
        if (-not $model) { $model = "llama3.2:3b" }
        Write-Host "  Pulling $model ..."
        & ollama pull $model
    }

    $downloadArgs = @("scripts\setup_local_ai.py", "--install-whisper")
    if (-not $Check) { $downloadArgs += "--check" }
    if ($WhisperModel) { $downloadArgs += @("--whisper-model", $WhisperModel) }
    & python @downloadArgs | Out-Host
}

if ($Check) {
    Write-Step "Check only - not starting the stack"
    exit $(if ($failures -gt 0) { 1 } else { 0 })
}

if (-not $NoStart) {
    Write-Step "Building and starting services"
    Write-Host "  The first build takes several minutes (Python + Torch CPU wheels)."
    & docker compose up -d --build
    if ($LASTEXITCODE -ne 0) {
        Write-Bad "docker compose up failed. See the output above."
        exit 1
    }
    Write-Ok "Containers started"

    Write-Step "Waiting for the backend"
    $healthy = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8000/health" -TimeoutSec 3
            if ($response.StatusCode -eq 200) { $healthy = $true; break }
        } catch { Start-Sleep -Seconds 3 }
    }
    if ($healthy) { Write-Ok "Backend healthy at http://localhost:8000" }
    else { Write-Bad "Backend did not become healthy. Run: docker compose logs backend"; $script:failures++ }
}

Write-Step "Verifying local AI readiness"
& python scripts\setup_local_ai.py --check | Out-Host
$ready = $LASTEXITCODE -eq 0

Write-Host ""
if ($ready -and $failures -eq 0) {
    Write-Host "ClipWright Studio is ready: http://localhost:3107" -ForegroundColor Green
    Write-Host "Open Settings -> Offline & local AI to see live model status."
    exit 0
}
Write-Host ""
Write-Host "Setup finished with warnings. Resolve the [warn]/[fail] lines above," -ForegroundColor Yellow
Write-Host "then re-run: python scripts\setup_local_ai.py --check"
exit 1
