$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
python -m pip install -e ".[desktop,audio,stt,browser,build,test,dev,ui-test]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed with exit code $LASTEXITCODE" }
python -m ruff format --check .
if ($LASTEXITCODE -ne 0) { throw "Format check failed with exit code $LASTEXITCODE" }
python -m ruff check .
if ($LASTEXITCODE -ne 0) { throw "Lint check failed with exit code $LASTEXITCODE" }
python -m pytest
if ($LASTEXITCODE -ne 0) { throw "Regression tests failed with exit code $LASTEXITCODE" }
python -m pytest learning_tests -q
if ($LASTEXITCODE -ne 0) { throw "Learning tests failed with exit code $LASTEXITCODE" }
python -m pytest -m ui tests/ui -q
if ($LASTEXITCODE -ne 0) { throw "UI tests failed with exit code $LASTEXITCODE" }
python -m PyInstaller --clean --noconfirm packaging/local-assistant.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
$isccCommand = Get-Command iscc -ErrorAction SilentlyContinue
$isccPath = if ($isccCommand) { $isccCommand.Source } else { $null }
if (-not $isccPath) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) { $isccPath = $candidate }
}
if ($isccPath) {
    & $isccPath packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
} else {
    Write-Warning "Inno Setup not found; installer was not compiled."
}
Write-Host "Unsigned build artifacts created under dist/. Sign before release."
