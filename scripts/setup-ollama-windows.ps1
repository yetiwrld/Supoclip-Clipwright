<#
.SYNOPSIS
    Make a native Windows Ollama service reachable from the ClipWright Studio
    Docker containers - without exposing it to the internet or to untrusted
    networks.

.DESCRIPTION
    Docker Desktop runs the backend, worker and ai-worker in a VM. Those
    containers reach the host as "host.docker.internal", which resolves to the
    host's vEthernet address, NOT to 127.0.0.1. Ollama's default bind is
    127.0.0.1:11434, so the containers get "connection refused" even though
    `ollama list` works perfectly in PowerShell.

    There are two supported fixes:

      1. Native Ollama (default here)
         Set the user environment variable OLLAMA_HOST=0.0.0.0:11434, restart
         Ollama, and add ONE inbound firewall rule scoped to the Docker/WSL
         subnet on the Private profile. The port is never opened to the public
         profile, to Any address, or to the internet.

      2. Ollama inside Docker  (-UseDockerOllama)
         Nothing is exposed on the host at all. The compose "ollama" profile
         runs the model server on the compose network, models persist in
         .\data\ollama, and .env is pointed at http://ollama:11434/v1.

    Nothing is changed without an explicit switch.

.PARAMETER Check
    Read-only report (default when no action switch is given).

.PARAMETER Configure
    Configure native Ollama for Docker: user env var, restart, scoped firewall
    rule. Requires an elevated PowerShell window for the firewall part.

.PARAMETER UseDockerOllama
    Start Ollama as a compose service instead, pull the model there, and point
    .env at it. No host port is exposed.

.PARAMETER RemoveFirewallRule
    Delete the "ClipWright Studio - Ollama for Docker" rule again.

.PARAMETER Model
    Ollama model to pull / verify. Default: llama3.2:3b.

.PARAMETER Port
    Ollama port. Default: 11434.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup-ollama-windows.ps1 -Check

.EXAMPLE
    # Elevated PowerShell, for the firewall rule
    powershell -ExecutionPolicy Bypass -File scripts\setup-ollama-windows.ps1 -Configure

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup-ollama-windows.ps1 -UseDockerOllama
#>

[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Configure,
    [switch]$UseDockerOllama,
    [switch]$RemoveFirewallRule,
    [string]$Model = "llama3.2:3b",
    [int]$Port = 11434
)

$ErrorActionPreference = "Stop"
$RuleName = "ClipWright Studio - Ollama for Docker"
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "   [ok]   $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "   [warn] $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host "   [fail] $Text" -ForegroundColor Red }

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-OllamaInstalled {
    return [bool](Get-Command ollama -ErrorAction SilentlyContinue)
}

function Get-OllamaModels {
    try {
        $lines = & ollama list 2>$null
        if (-not $lines) { return @() }
        return @($lines | Select-Object -Skip 1 | Where-Object { $_.Trim() } | ForEach-Object { ($_ -split '\s+')[0] })
    } catch {
        return @()
    }
}

function Get-DockerSubnets {
    # Docker Desktop uses vEthernet (WSL) or vEthernet (Default Switch); the
    # latter is also what host.docker.internal resolves to on current builds.
    $subnets = @()
    $adapters = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceAlias -match 'vEthernet|Docker' -and $_.IPAddress -notlike '169.254.*' }
    foreach ($adapter in $adapters) {
        $split = $adapter.IPAddress.Split('.')
        if ($split.Count -eq 4) {
            $subnets += "$($split[0]).$($split[1]).$($split[2]).0/24"
        }
    }
    if (-not $subnets) {
        # Deterministic fallback: RFC1918 space only, never a public range.
        $subnets = @("172.16.0.0/12", "192.168.65.0/24", "10.0.0.0/8")
        Write-Warn2 "No Docker/WSL adapter found; firewall scope falls back to private ranges only."
    }
    return $subnets | Select-Object -Unique
}

function Get-ListenerInfo {
    $connections = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    return @($connections | Select-Object -ExpandProperty LocalAddress -Unique)
}

function Show-Status {
    Write-Step "Ollama on this machine"

    if (-not (Test-OllamaInstalled)) {
        Write-Fail "Ollama is not installed or not on PATH. Install it from https://ollama.com/download"
    } else {
        Write-Ok "Ollama found: $((& ollama --version) -join ' ')"
        $models = Get-OllamaModels
        if ($models -contains $Model -or ($models | Where-Object { $_ -like "$Model*" })) {
            Write-Ok "Model '$Model' is installed"
        } elseif ($models.Count -gt 0) {
            Write-Warn2 "Model '$Model' is NOT installed. Installed: $($models -join ', ')"
            Write-Host "          Run: ollama pull $Model"
        } else {
            Write-Warn2 "No models installed. Run: ollama pull $Model"
        }
    }

    $userHost = [Environment]::GetEnvironmentVariable("OLLAMA_HOST", "User")
    $machineHost = [Environment]::GetEnvironmentVariable("OLLAMA_HOST", "Machine")
    $processHost = $env:OLLAMA_HOST
    Write-Host "   OLLAMA_HOST (user/machine/process): '$userHost' / '$machineHost' / '$processHost'"

    $listeners = Get-ListenerInfo
    if (-not $listeners) {
        Write-Fail "Nothing is listening on TCP $Port. Start Ollama (it normally runs in the tray)."
    } elseif ($listeners -contains "0.0.0.0" -or $listeners -contains "::") {
        Write-Ok "Listening on all interfaces (TCP $Port) - containers can reach it"
    } elseif ($listeners -contains "127.0.0.1" -or $listeners -contains "::1") {
        Write-Warn2 "Listening on loopback only ($($listeners -join ', ')) - Docker CANNOT reach this"
        Write-Host "          Fix: powershell -ExecutionPolicy Bypass -File scripts\setup-ollama-windows.ps1 -Configure"
    } else {
        Write-Warn2 "Listening on: $($listeners -join ', ')"
    }

    $rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($rule) {
        $addresses = ($rule | Get-NetFirewallAddressFilter).RemoteAddress -join ', '
        Write-Ok "Firewall rule exists, inbound remote scope: $addresses"
    } else {
        Write-Warn2 "No '$RuleName' firewall rule. Containers may be blocked even after binding."
    }

    Write-Step "How the containers see it"
    Write-Host "   Run both of these; they must PASS:"
    Write-Host "     python scripts\verify_local_ai.py"
    Write-Host "     docker compose exec -T backend .venv/bin/python -m src.local_ai --check"
    Write-Host ""
    Write-Host "   Docker Desktop alone is not enough: 127.0.0.1 inside a container is the"
    Write-Host "   container itself. The compose file points the services at"
    Write-Host "   http://host.docker.internal:11434/v1."
}

function Set-NativeOllama {
    Write-Step "1/4 Environment"
    $subnets = Get-DockerSubnets
    Write-Host "   Docker subnet(s) that will be allowed: $($subnets -join ', ')"
    [Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:$Port", "User")
    $env:OLLAMA_HOST = "0.0.0.0:$Port"
    Write-Ok "OLLAMA_HOST=0.0.0.0:$Port set for this user (not machine-wide)"

    Write-Step "2/4 Restart Ollama"
    foreach ($name in @("ollama app", "ollama")) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    $exe = (Get-Command ollama -ErrorAction SilentlyContinue)?.Source
    if ($exe) {
        Start-Process -FilePath $exe -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep -Seconds 4
        Write-Ok "Started 'ollama serve' with the new bind address"
    } else {
        Write-Warn2 "Could not find ollama.exe; start Ollama from the Start menu, then re-run -Check"
    }

    Write-Step "3/4 Firewall (Private profile, Docker subnet only)"
    if (-not (Test-IsAdmin)) {
        Write-Fail "Adding a firewall rule needs an elevated PowerShell."
        Write-Host "          Re-run this script as Administrator, or add the rule manually:"
        Write-Host "          New-NetFirewallRule -DisplayName '$RuleName' -Direction Inbound -Action Allow ``"
        Write-Host "            -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress $($subnets -join ',')"
    } else {
        Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress $subnets `
            -Description "Lets the ClipWright Studio Docker containers reach Ollama on this host." | Out-Null
        Write-Ok "Inbound TCP $Port allowed from $($subnets -join ', ') on the Private profile only"
        Write-Host "          Public/Domain profiles stay closed; the port is not reachable from the internet."
    }

    Write-Step "4/4 Verify from inside a container"
    Push-Location $RepoRoot
    try {
        $running = docker compose ps --services --status running 2>$null
        if ($running -match "backend") {
            & docker compose exec -T backend .venv/bin/python -m src.local_ai --check
            if ($LASTEXITCODE -ne 0) {
                Write-Fail "The backend still cannot use the local AI stack; see the rows above."
            }
        } else {
            Write-Warn2 "The stack is not running. Start it, then run the check below:"
            Write-Host "          docker compose up -d"
            Write-Host "          docker compose exec -T backend .venv/bin/python -m src.local_ai --check"
            Write-Host "          docker compose exec -T worker .venv/bin/python -m src.local_ai --check"
            Write-Host "          docker compose exec -T ai-worker .venv/bin/python -m src.local_ai --check"
            Write-Host "          python scripts/verify_local_ai.py   (all three at once)"
        }
    } finally {
        Pop-Location
    }
}

function Enable-DockerOllama {
    Write-Step "Ollama as a compose service (nothing exposed on the host)"
    Push-Location $RepoRoot
    try {
        docker compose --profile ollama up -d ollama
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "Could not start the ollama service. Check that Docker Desktop is running."
            return
        }
        Write-Ok "clipwright-ollama is starting (models persist in .\data\ollama)"
        Start-Sleep -Seconds 8
        docker compose exec -T ollama ollama pull $Model
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 "Pulling '$Model' failed; check `docker compose logs ollama`."
        } else {
            Write-Ok "Model '$Model' pulled inside the container"
        }

        $envPath = Join-Path $RepoRoot ".env"
        $line = "OLLAMA_BASE_URL=http://ollama:11434/v1"
        if (Test-Path $envPath) {
            $content = Get-Content $envPath
            if ($content -match '^\s*OLLAMA_BASE_URL=') {
                $content = $content -replace '^\s*OLLAMA_BASE_URL=.*', $line
                Set-Content -Path $envPath -Value $content -Encoding utf8
            } else {
                Add-Content -Path $envPath -Value $line -Encoding utf8
            }
        } else {
            Set-Content -Path $envPath -Value $line -Encoding utf8
        }
        Write-Ok ".env: $line"
        Write-Host "          Restart the services to pick it up: docker compose up -d backend worker ai-worker"
        Write-Host "          Verify: python scripts/verify_local_ai.py"
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------- main ------

Write-Host "ClipWright Studio - Ollama connectivity for Docker" -ForegroundColor White
Write-Host "Repository: $RepoRoot"

if ($RemoveFirewallRule) {
    if (-not (Test-IsAdmin)) {
        Write-Fail "Removing a firewall rule needs an elevated PowerShell."
        exit 1
    }
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Ok "Firewall rule '$RuleName' removed."
    exit 0
}

if ($UseDockerOllama) {
    Enable-DockerOllama
    exit 0
}

if ($Configure) {
    Set-NativeOllama
    Write-Step "Status"
    Show-Status
    exit 0
}

Show-Status
Write-Host ""
Write-Host "Actions:" -ForegroundColor White
Write-Host "  -Configure          native Ollama: bind 0.0.0.0, restart, scoped firewall rule"
Write-Host "  -UseDockerOllama    run Ollama in compose instead (no host exposure)"
Write-Host "  -RemoveFirewallRule undo the firewall rule"
exit 0
