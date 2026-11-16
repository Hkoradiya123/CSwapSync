# One-line installer (Windows PowerShell):
#   irm https://raw.githubusercontent.com/Hkoradiya123/CSwapSync/main/install.ps1 | iex
# Also enable bare `uv tool install cswapsync`:
#   $env:CSWAPSYNC_USE_INDEX=1; irm .../install.ps1 | iex
# Needs uv (preferred) or pipx. Data lives in %USERPROFILE%\CSwapSync.
$ErrorActionPreference = "Stop"
# zip archive instead of git+https: works without Git installed
$Repo = "cswapsync @ https://github.com/Hkoradiya123/CSwapSync/archive/refs/heads/main.zip"
$IndexUrl = "https://hkoradiya123.github.io/CSwapSync/simple/"

if (Get-Command uv -ErrorAction SilentlyContinue) {
  uv tool install --force --python 3.12 $Repo
  if ($LASTEXITCODE -ne 0) { throw "uv tool install failed" }
  $Bin = Join-Path (uv tool dir --bin) "cswapsync.exe"
} elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
  pipx install --force $Repo
  if ($LASTEXITCODE -ne 0) { throw "pipx install failed" }
  $Bin = "cswapsync"
} else {
  throw "Install uv first:  powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`""
}

if ($env:CSWAPSYNC_USE_INDEX -eq "1") {
  $Cfg = Join-Path $env:APPDATA "uv\uv.toml"
  New-Item -ItemType Directory -Force -Path (Split-Path $Cfg) | Out-Null
  if ((Test-Path $Cfg) -and (Select-String -Path $Cfg -SimpleMatch $IndexUrl -Quiet)) {
    Write-Host "uv.toml already has the cswapsync index"
  } else {
    Add-Content -Path $Cfg -Value "`n[[index]]`nname = `"cswapsync`"`nurl = `"$IndexUrl`""
    Write-Host "added cswapsync index to $Cfg (bare 'uv tool install cswapsync' now works)"
  }
}

if (-not $env:CSWAPSYNC_NO_SETUP) {
  & $Bin setup
}
