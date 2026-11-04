# Mirror src/<package> into the QGIS plugin directory, where QGIS expects
# metadata.txt and __init__.py at the plugin root. Paths come from paths.json.
# Refuses to write through a junction so a mirror run can never overwrite the
# source tree.
param([switch]$ListOnly)

$pluginRoot = Split-Path -Parent $PSScriptRoot
$config = Get-Content (Join-Path $pluginRoot 'paths.json') -Raw | ConvertFrom-Json

$source = Join-Path $pluginRoot (Join-Path 'src' $config.plugin.package)
$pluginsDir = [Environment]::ExpandEnvironmentVariables($config.qgis.pluginsDir)
$dest = Join-Path $pluginsDir $config.plugin.installAs

if (-not (Test-Path (Join-Path $source 'metadata.txt'))) {
    Write-Error "not a QGIS plugin package (metadata.txt missing): $source"
    exit 1
}

if ((Test-Path $dest) -and ((Get-Item $dest).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    Write-Error "destination is a junction or symlink, remove it first: $dest"
    exit 1
}

# robocopy exits 1 for 'files copied', so anything below 8 is success.
# A dry run keeps the file list; a real run only reports failures.
$flags = '/MIR', '/XD', '__pycache__', '.ruff_cache', '/NP'
if ($ListOnly) {
    $flags += '/L'
} else {
    $flags += '/NFL', '/NDL', '/NJH', '/NJS'
}

robocopy $source $dest @flags
if ($LASTEXITCODE -ge 8) { exit 1 }

Write-Host "deployed $source -> $dest"
Write-Host 'restart QGIS; enable "QGIS Agent" under Plugins > Manage and Install Plugins if it is not enabled yet'
exit 0
