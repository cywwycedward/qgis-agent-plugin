# Run pytest inside the QGIS python named by paths.json.
# Extra arguments are passed to pytest; the default is '-q tests'.
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PytestArgs)

$pluginRoot = Split-Path -Parent $PSScriptRoot
$config = Get-Content (Join-Path $pluginRoot 'paths.json') -Raw | ConvertFrom-Json
$python = [Environment]::ExpandEnvironmentVariables($config.qgis.python)

if (-not $PytestArgs) { $PytestArgs = @('-q', 'tests') }

Push-Location $pluginRoot
& $python -m pytest @PytestArgs
$code = $LASTEXITCODE
Pop-Location
exit $code
