<#
    Start SubPlz Web on localhost.

        .\run.ps1              # start on http://127.0.0.1:8420
        .\run.ps1 -Port 9000
        .\run.ps1 -NoBrowser

    Creates .venv and installs dependencies on first run, including the
    alignment backend itself.
#>
[CmdletBinding()]
param(
    [int]$Port = 8420,
    [string]$BindHost = "127.0.0.1",
    [switch]$NoBrowser,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"

$Root   = $PSScriptRoot
$Venv   = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

# Where to install the alignment backend from. Override to use a fork or a
# local checkout, e.g. -e C:\path\to\SubPlz
$SubPlzSpec = $env:SUBPLZ_INSTALL_SPEC
if (-not $SubPlzSpec) {
    $SubPlzSpec = "git+https://github.com/kanjieater/SubPlz.git"
}

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }

# --- ffmpeg is not optional ---------------------------------------------------
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warn "ffmpeg is not on PATH. It is needed to read audio and build video."
    Write-Warn "Install it (winget install Gyan.FFmpeg) and reopen the terminal."
}

# --- venv ---------------------------------------------------------------------
# subplz pins requires-python >=3.10,<3.12, so 3.11 it is.
if (-not (Test-Path $Python)) {
    Write-Step "Creating .venv on Python 3.11"
    & py -3.11 --version 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.11 not found. Install it, then rerun. (py -0 lists what you have.)"
    }
    & py -3.11 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}

# --- dependencies -------------------------------------------------------------
& $Python -c "import fastapi, uvicorn, sqlalchemy, multipart, pydantic_settings" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Step "Installing web dependencies"
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }
}

& $Python -c "import subplz" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Step "Installing the alignment backend ($SubPlzSpec) - pulls torch, several minutes"
    & $Python -m pip install $SubPlzSpec
    if ($LASTEXITCODE -ne 0) { throw "alignment backend install failed" }
}

# --- language registry --------------------------------------------------------
if (-not (Test-Path (Join-Path $Root "backend\languages.json"))) {
    Write-Step "Generating the language registry"
    Push-Location $Root
    try { & $Python -m backend.gen_languages } finally { Pop-Location }
}

# --- run ----------------------------------------------------------------------
# The backend prints emoji; without UTF-8 these die on a cp1252/cp932 console.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# Put the venv's scripts first, so `subplz` resolves on PATH.
$env:PATH = (Join-Path $Venv "Scripts") + [IO.Path]::PathSeparator + $env:PATH

$url = "http://${BindHost}:${Port}"
Write-Step "Starting SubPlz Web on $url"
Write-Host "    Ctrl+C to stop." -ForegroundColor DarkGray

if (-not $NoBrowser) {
    Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 3; Start-Process $u } `
        -ArgumentList $url | Out-Null
}

$uvicornArgs = @("-m", "uvicorn", "backend.main:app", "--host", $BindHost, "--port", $Port)
if ($Reload) { $uvicornArgs += "--reload" }

Push-Location $Root
try { & $Python @uvicornArgs } finally { Pop-Location }
