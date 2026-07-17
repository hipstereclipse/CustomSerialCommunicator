<#
.SYNOPSIS
    Installs all dependencies for the Custom Serial Communicator project.

.DESCRIPTION
    Uses uv (fast, matches the committed uv.lock) when available, and falls
    back to a standard python -m venv + pip workflow otherwise. Installs the
    project in editable mode including the [dev] extras (pytest, ruff, mypy,
    pytest-qt) unless -SkipDev is passed.

.PARAMETER SkipDev
    Skip installing the [dev] optional-dependencies group (test/lint tools).

.PARAMETER PreferPip
    Use the pip + venv workflow even if uv is installed.

.EXAMPLE
    .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -SkipDev
#>

[CmdletBinding()]
param(
    [switch]$SkipDev,
    [switch]$PreferPip
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$MinMajor = 3
$MinMinor = 11

function Write-Step($Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-PythonCandidate($Cmd, $Args) {
    try {
        $output = & $Cmd @Args --version 2>&1
    } catch {
        return $false
    }
    if ($output -notmatch "Python (\d+)\.(\d+)") {
        return $false
    }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    return ($major -gt $MinMajor) -or ($major -eq $MinMajor -and $minor -ge $MinMinor)
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if (Test-PythonCandidate "py" @("-3.$MinMinor")) { return @{ Cmd = "py"; Args = @("-3.$MinMinor") } }
        if (Test-PythonCandidate "py" @("-3"))            { return @{ Cmd = "py"; Args = @("-3") } }
    }
    if ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-PythonCandidate "python" @())) {
        return @{ Cmd = "python"; Args = @() }
    }
    return $null
}

$extraArg = if ($SkipDev) { @() } else { @(".[dev]") }
$editableTarget = if ($SkipDev) { "." } else { ".[dev]" }

$useUv = -not $PreferPip -and (Get-Command uv -ErrorAction SilentlyContinue)

if ($useUv) {
    Write-Step "uv detected - installing via 'uv sync' (uses the committed uv.lock)"
    if ($SkipDev) {
        uv sync
    } else {
        uv sync --extra dev
    }
    Write-Host ""
    Write-Host "Done. uv manages .venv automatically." -ForegroundColor Green
    Write-Host "Activate it with:  .\.venv\Scripts\Activate.ps1"
    Write-Host "Or run commands directly with:  uv run python main.py"
    return
}

Write-Step "Locating a Python $MinMajor.$MinMinor+ interpreter"
$python = Find-Python
if (-not $python) {
    Write-Error "No Python $MinMajor.$MinMinor+ interpreter found on PATH. Install Python $MinMajor.$MinMinor or newer, or install uv (https://docs.astral.sh/uv/) and re-run this script."
    exit 1
}
Write-Host "Using: $($python.Cmd) $($python.Args -join ' ')"

$venvPath = Join-Path $RepoRoot ".venv"
if (-not (Test-Path $venvPath)) {
    Write-Step "Creating virtual environment at .venv"
    & $python.Cmd @($python.Args + @("-m", "venv", $venvPath))
} else {
    Write-Step "Reusing existing virtual environment at .venv"
}

$venvPython = Join-Path $venvPath "Scripts\python.exe"

Write-Step "Upgrading pip"
& $venvPython -m pip install --upgrade pip

Write-Step "Installing project dependencies (pip install -e $editableTarget)"
& $venvPython -m pip install -e $editableTarget

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Activate the environment with:  .\.venv\Scripts\Activate.ps1"
Write-Host "Then launch the app with:        python main.py"
