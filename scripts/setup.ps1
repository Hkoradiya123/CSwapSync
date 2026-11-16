# Install (or upgrade) the cswapsync tool on Windows, then run the interactive wizard.
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1          (from this checkout)
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Pypi    (from PyPI)
# Data (keys, config, backups) stays in %USERPROFILE%\CSwapSync (or $env:CSWAPSYNC_HOME).
param([switch]$Pypi, [switch]$NoWizard)
$ErrorActionPreference = "Stop"
$Src = if ($Pypi) { "cswapsync" } else { Split-Path -Parent $PSScriptRoot }
if (Get-Command uv -ErrorAction SilentlyContinue) {
  uv tool install --force --python 3.12 $Src
} elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
  pipx install --force $Src
} else {
  throw "need uv (https://docs.astral.sh/uv/) or pipx"
}
if ($LASTEXITCODE -ne 0) { throw "install failed" }
if (-not $NoWizard) {
  cswapsync setup
  exit $LASTEXITCODE
}
