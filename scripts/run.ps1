$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$exitCode = 0

Push-Location -LiteralPath $projectRoot
try {
    & python -m assistant.app @args
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
