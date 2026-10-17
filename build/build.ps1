<#
    Build a single-file Windows executable with a custom icon.

        powershell -ExecutionPolicy Bypass -File build\build.ps1

    Output:  dist\MusicDecryptor.exe
#>
[CmdletBinding()]
param(
    [switch]$SkipIcon,
    [switch]$NoClean
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot      # repo root (parent of build/)
Set-Location $root

if (-not $SkipIcon) {
    Write-Host '==> generating icon' -ForegroundColor Cyan
    python assets\make_icon.py
}

Write-Host '==> checking dependencies' -ForegroundColor Cyan
python -c "import PySide6, mutagen, PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'missing deps, running: python -m pip install -r requirements.txt' -ForegroundColor Yellow
    python -m pip install -r requirements.txt
}

Write-Host '==> building (this can take a few minutes)' -ForegroundColor Cyan
$cleanArg = if ($NoClean) { @() } else { @('--clean') }
python -m PyInstaller --noconfirm @cleanArg --distpath dist --workpath build\work build\music_decryptor.spec

$exe = Join-Path $root 'dist\MusicDecryptor.exe'
if (Test-Path $exe) {
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "`n==> done: $exe  ($size MB)" -ForegroundColor Green
} else {
    throw "build failed: $exe not found"
}