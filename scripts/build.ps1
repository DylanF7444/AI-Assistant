$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
python -m pip install --upgrade -e ".[desktop,audio,stt,wakeword,integrations,browser,documents,vision,build,test,dev,ui-test,security]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed with exit code $LASTEXITCODE" }
python -m ruff format --check .
if ($LASTEXITCODE -ne 0) { throw "Format check failed with exit code $LASTEXITCODE" }
python -m ruff check .
if ($LASTEXITCODE -ne 0) { throw "Lint check failed with exit code $LASTEXITCODE" }
python -m mypy
if ($LASTEXITCODE -ne 0) { throw "Type check failed with exit code $LASTEXITCODE" }
python scripts/check_documentation_consistency.py
if ($LASTEXITCODE -ne 0) { throw "Documentation check failed with exit code $LASTEXITCODE" }
python scripts/run_evaluations.py --output .benchmark-data/evaluations
if ($LASTEXITCODE -ne 0) { throw "Deterministic evaluation failed with exit code $LASTEXITCODE" }
python scripts/audit_dependencies.py
if ($LASTEXITCODE -ne 0) { throw "Dependency audit failed with exit code $LASTEXITCODE" }
python -m coverage erase
if ($LASTEXITCODE -ne 0) { throw "Coverage reset failed with exit code $LASTEXITCODE" }
python -m pytest tests --ignore=tests/ui --cov=assistant --cov-branch --cov-report=
if ($LASTEXITCODE -ne 0) { throw "Regression tests failed with exit code $LASTEXITCODE" }
python -m pytest learning_tests -q --cov=assistant --cov-branch --cov-append --cov-report=
if ($LASTEXITCODE -ne 0) { throw "Learning tests failed with exit code $LASTEXITCODE" }
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest tests/ui -q --cov=assistant --cov-branch --cov-append --cov-report=
if ($LASTEXITCODE -ne 0) { throw "UI tests failed with exit code $LASTEXITCODE" }
python -m coverage json -o .benchmark-data/coverage.json
if ($LASTEXITCODE -ne 0) { throw "Coverage report failed with exit code $LASTEXITCODE" }
python scripts/check_coverage.py .benchmark-data/coverage.json
if ($LASTEXITCODE -ne 0) { throw "Coverage gate failed with exit code $LASTEXITCODE" }
python -m PyInstaller --clean --noconfirm packaging/local-assistant.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
python scripts/verify_packaged_media.py dist/LocalAssistant.exe
if ($LASTEXITCODE -ne 0) { throw "Packaged media verification failed with exit code $LASTEXITCODE" }
$isccCommand = Get-Command iscc -ErrorAction SilentlyContinue
$isccPath = if ($isccCommand) { $isccCommand.Source } else { $null }
$installerBuilt = $false
if (-not $isccPath) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) { $isccPath = $candidate }
}
if ($isccPath) {
    & $isccPath packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
    $installerBuilt = $true
} else {
    Write-Warning "Inno Setup not found; installer was not compiled."
}
python scripts/generate_sbom.py --output dist/sbom.cdx.json --extras desktop,audio,stt,wakeword,integrations,browser,documents,vision
if ($LASTEXITCODE -ne 0) { throw "SBOM generation failed with exit code $LASTEXITCODE" }
$artifactArguments = @("--artifact", "dist/LocalAssistant.exe", "--artifact", "dist/sbom.cdx.json")
$installerPath = "dist/LocalAssistant-0.1.0-Setup.exe"
if ($installerBuilt -and (Test-Path $installerPath)) { $artifactArguments += @("--artifact", $installerPath) }
python scripts/generate_release_manifest.py @artifactArguments
if ($LASTEXITCODE -ne 0) { throw "Release manifest generation failed with exit code $LASTEXITCODE" }
Write-Host "Unsigned build artifacts created under dist/. Sign before release."
