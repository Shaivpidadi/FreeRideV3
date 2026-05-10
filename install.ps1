# FreeRide installer for Windows. Run with:
#
#   powershell -ExecutionPolicy ByPass -c "irm https://api.free-ride.xyz/install.ps1 | iex"
#
# What this does:
#   1. Installs `uv` (Astral's Python package manager) if it isn't already.
#   2. Uses `uv tool install` to install freeride-gateway into an isolated
#      venv and put the `freeride.exe` binary on PATH.
#   3. Verifies `freeride --version` works.
#
# Mirror of the POSIX `install.sh` — same install pattern as the Astral/uv
# Windows installer.

$ErrorActionPreference = "Stop"

function Print($msg) {
    Write-Host $msg
}

function Fail($msg) {
    Write-Host "error: $msg" -ForegroundColor Red
    exit 1
}

Print ""
Print "FreeRide installer (Windows)"
Print ""

# 1. Make sure we have uv. If not, install it via the official one-liner.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Print "uv (Python package manager) not found - installing it first..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Fail "Failed to install uv: $_"
    }

    # uv installs to %USERPROFILE%\.local\bin on Windows; load it onto PATH for this session.
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path (Join-Path $uvBin "uv.exe")) {
        $env:Path = "$uvBin;" + $env:Path
    }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        Fail "uv installed but not on PATH. Open a new PowerShell window and re-run this installer."
    }
}

Print ""
Print "Installing freeride-gateway..."
# --prerelease=allow because we ship 0.3.0a* alphas pre-stable; once 0.3.0
# final lands you can drop this flag and it'll still pick up the latest.
uv tool install --prerelease=allow freeride-gateway
if ($LASTEXITCODE -ne 0) {
    Fail "uv tool install failed (exit $LASTEXITCODE)"
}

Print ""
Print "Verifying..."
$freeride = Get-Command freeride -ErrorAction SilentlyContinue
if ($freeride) {
    & $freeride.Source --version
} else {
    $candidate = Join-Path $env:USERPROFILE ".local\bin\freeride.exe"
    if (Test-Path $candidate) {
        & $candidate --version
        Print ""
        Print "Note: $($env:USERPROFILE)\.local\bin is not on your PATH yet. Run:"
        Print "  `$env:Path = `"$($env:USERPROFILE)\.local\bin;`" + `$env:Path"
        Print "Or add it permanently via System Properties -> Environment Variables."
    } else {
        Fail "Install completed but the freeride binary couldn't be located. Open a new PowerShell window and try again."
    }
}

# ---------------------------------------------------------------------------
# Install-event beacon — fires once per installation, before the user has
# even run `freeride serve`. Best-effort: failure is silent and never
# breaks the install. Skipped when -NoTelemetry switch is passed or
# $env:FREERIDE_TELEMETRY = 'off'. Reuses
# %USERPROFILE%\.freeride\installation_id when present so re-installs
# don't generate a new id.
# ---------------------------------------------------------------------------
$telemetryDisabled = ($env:FREERIDE_TELEMETRY -eq "off")
if (-not $telemetryDisabled -and ($args -contains "-NoTelemetry" -or $args -contains "--no-telemetry")) {
    $telemetryDisabled = $true
}

if (-not $telemetryDisabled) {
    try {
        $freerideDir = Join-Path $env:USERPROFILE ".freeride"
        $installIdFile = Join-Path $freerideDir "installation_id"
        if (-not (Test-Path $freerideDir)) {
            New-Item -ItemType Directory -Path $freerideDir -Force | Out-Null
        }

        if (Test-Path $installIdFile) {
            $installId = (Get-Content $installIdFile -ErrorAction SilentlyContinue).Trim()
        }
        if (-not $installId) {
            $installId = ([guid]::NewGuid().ToString().ToLower())
            Set-Content -Path $installIdFile -Value $installId -NoNewline -ErrorAction SilentlyContinue
        }

        $installedVersion = "unknown"
        try {
            $verLine = (& freeride --version 2>$null) -join " "
            if ($verLine -match '(\d+\.\d+\.\d+[a-zA-Z0-9.+-]*)') {
                $installedVersion = $Matches[1]
            }
        } catch { }

        $payload = @{
            installation_id = $installId
            version         = $installedVersion
            os              = "windows"
            install_method  = "powershell"
        } | ConvertTo-Json -Compress

        Invoke-RestMethod `
            -Uri "https://api.free-ride.xyz/v1/install-event" `
            -Method POST `
            -ContentType "application/json" `
            -Body $payload `
            -TimeoutSec 5 `
            -ErrorAction SilentlyContinue | Out-Null
    } catch {
        # Best-effort: never break the install if telemetry POST fails.
    }
}

Print ""
Print "Done. Next:"
Print "  `$env:OPENROUTER_API_KEY = 'sk-or-v1-...'   # get a free one at https://openrouter.ai/keys"
Print "  freeride serve                              # start the gateway"
Print "  freeride bind continue                      # or aider / hermes / openclaw"
Print ""
