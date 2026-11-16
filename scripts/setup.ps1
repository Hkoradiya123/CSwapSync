# CSwapSync bootstrap (Windows 10/11): venv + deps, then the interactive wizard.
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -NoWizard
param([switch]$NoWizard)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"

foreach ($d in "config","credentials","staging","logs","backups") {
  New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null
}
# credentials\ readable by this user only
icacls (Join-Path $Root "credentials") /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" /Q | Out-Null

Write-Host "==> python venv + dependencies"
if (-not (Test-Path $Py)) {
  if (Get-Command uv -ErrorAction SilentlyContinue) { uv venv -q $Venv --python 3.12 }
  elseif (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv $Venv }
  else { python -m venv $Venv }
}
if (Get-Command uv -ErrorAction SilentlyContinue) {
  uv pip install -q --python $Py -r (Join-Path $Root "requirements.txt")
} else {
  & $Py -m pip install -q -r (Join-Path $Root "requirements.txt")
}
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }

if (-not $NoWizard) {
  $env:CSWAPSYNC_HOME = $Root
  & $Py (Join-Path $Root "src\cli.py") setup
  exit $LASTEXITCODE
}
