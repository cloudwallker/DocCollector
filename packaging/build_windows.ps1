# Build a directory-based portable DocCollector for Windows.
# Run from a machine WITH internet access (PyInstaller is fetched from PyPI).
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Result:  dist\DocCollector\   (copy the whole folder; run DocCollector.exe)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

function Assert-NativeSuccess([string] $Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code $LASTEXITCODE). Build stopped."
    }
}

Write-Host "== Creating virtualenv .venv-build =="
if (-not (Test-Path ".venv-build")) {
    python -m venv .venv-build
    Assert-NativeSuccess "Create virtualenv"
}
& ".\.venv-build\Scripts\python.exe" -m pip install --upgrade pip
Assert-NativeSuccess "Upgrade pip"
& ".\.venv-build\Scripts\python.exe" -m pip install -r requirements.txt
Assert-NativeSuccess "Install runtime dependencies"
& ".\.venv-build\Scripts\python.exe" -m pip install "pyinstaller>=6.0"
Assert-NativeSuccess "Install PyInstaller"

Write-Host "== Building onedir portable package =="
& ".\.venv-build\Scripts\pyinstaller.exe" packaging\DocCollector.spec --noconfirm --distpath dist --workpath build
Assert-NativeSuccess "Package application"

$Out = Join-Path $Repo "dist\DocCollector"
if (Test-Path $Out) {
    $size = (Get-ChildItem -Recurse $Out | Measure-Object -Property Length -Sum).Sum
    Write-Host ("== Done. Package: {0}  ({1:N1} MB) ==" -f $Out, ($size / 1MB))
} else {
    Write-Error "Build finished but dist\DocCollector was not produced."
}
