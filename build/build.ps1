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

# 先检查目标 exe 是否被占用（还在运行 / 杀软扫描中）—— 给出人话提示
$_exe = Join-Path $root 'dist\MusicDecryptor.exe'
if (Test-Path $_exe) {
    try { $fs = [IO.File]::Open($_exe, 'Open', 'ReadWrite', 'None'); $fs.Close() }
    catch { throw "dist\MusicDecryptor.exe 正被占用：请先关闭正在运行的软件（或稍等几秒让杀软扫描结束）再重打包。" }
}

Write-Host '==> building (this can take a few minutes)' -ForegroundColor Cyan
# 注意：不要把 `@var` splat 用在原生命令上 —— PowerShell 会把经 if/管道等
# 表达式折叠后的单元素“数组”还原成字符串，再逐字符展开（历史事故：
# @cleanArg 把 "--clean" 变成 "- - c l e a n"，PyInstaller 只报
# "unrecognized arguments: ...spec"，且旧版脚本还会误报构建成功）。
# 显式构造数组传参，并在调用后检查退出码。
$pyArgs = @('--noconfirm')
if (-not $NoClean) { $pyArgs += '--clean' }
$pyArgs += @('--distpath', 'dist', '--workpath', 'build\work', 'build\music_decryptor.spec')
python -m PyInstaller $pyArgs
if ($LASTEXITCODE -ne 0) {
    throw "build failed: PyInstaller exited with code $LASTEXITCODE"
}

$exe = Join-Path $root 'dist\MusicDecryptor.exe'
if (Test-Path $exe) {
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "`n==> done: $exe  ($size MB)" -ForegroundColor Green
} else {
    throw "build failed: $exe not found"
}