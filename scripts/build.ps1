$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
python -m pip install -e ".[desktop,audio,stt,build,test,dev,ui-test]"
python -m ruff format --check .
python -m ruff check .
python -m pytest
python -m pytest learning_tests -q
python -m pytest -m ui tests/ui -q
python -m PyInstaller --clean --noconfirm packaging/local-assistant.spec
$isccCommand = Get-Command iscc -ErrorAction SilentlyContinue
$isccPath = if ($isccCommand) { $isccCommand.Source } else { $null }
if (-not $isccPath) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) { $isccPath = $candidate }
}
if ($isccPath) {
    & $isccPath packaging\installer.iss
} else {
    Write-Warning "Inno Setup not found; installer was not compiled."
}
Write-Host "Unsigned build artifacts created under dist/. Sign before release."
