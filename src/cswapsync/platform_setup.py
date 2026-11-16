"""OS-specific setup helpers used by the setup wizard (Linux + Windows).

Everything here is idempotent and refuses to overwrite files it did not write.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

from .settings import IS_WINDOWS, ConfigurationError

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
REFRESH_UNIT = "cswap-refresh.service"
SYNC_DROPIN = "20-cswapsync-sync.conf"
HEARTBEAT_UNITS = ("cswapsync-heartbeat.service", "cswapsync-heartbeat.timer")
STRAY_EXPORTS = [Path.home() / "cswap-accounts-export.json"]


def default_device_id(role: str) -> str:
    host = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().lower()).strip("-")[:24]
    return f"{host or 'device'}-{role}"


def valid_device_id(value: str) -> bool:
    return bool(value) and len(value) <= 40 and value.replace("-", "").isalnum()


def restrict_to_owner(path: Path) -> None:
    """chmod 700/600 on POSIX; on Windows strip inherited ACLs, grant only this user."""
    if not path.exists():
        return
    if IS_WINDOWS:
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                            f"{user}:(OI)(CI)F" if path.is_dir() else f"{user}:F", "/T", "/Q"],
                           capture_output=True, check=False, timeout=30)
        return
    os.chmod(path, 0o700 if path.is_dir() else 0o600)


def find_cswap() -> str | None:
    found = shutil.which("cswap")
    if found:
        return found
    exe = "cswap.exe" if IS_WINDOWS else "cswap"
    candidate = Path.home() / ".local" / "bin" / exe
    return str(candidate) if candidate.exists() else None


def client_secret_candidates() -> list[Path]:
    home = Path.home()
    paths = [home / ".config" / "workspace-mcp" / "client_secret.json"]
    downloads = home / "Downloads"
    if downloads.is_dir():
        paths += sorted(downloads.glob("client_secret*.json"), reverse=True)
    return [p for p in paths if p.is_file() and is_desktop_client(p)]


def is_desktop_client(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("installed"), dict) \
        and bool(data["installed"].get("client_id"))


def client_project(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["installed"].get("project_id", "?")


# --------------------------------------------------------------------------- Linux hooks
GATE_DROPIN = "10-cswapsync-lease-gate.conf"


def cswapsync_bin() -> str:
    """Absolute path of the installed `cswapsync` entry point (uv tool / pipx / venv)."""
    import sys
    found = shutil.which("cswapsync")
    if found:
        return str(Path(found).resolve()) if not IS_WINDOWS else found
    candidate = Path(sys.argv[0])
    if candidate.name.startswith("cswapsync") and candidate.exists():
        return str(candidate.resolve())
    raise ConfigurationError("installed 'cswapsync' command not found on PATH; install with "
                             "'uv tool install' or 'pipx install' first")


def render_unit(name: str, binary: str) -> str:
    from importlib.resources import files
    text = (files("cswapsync") / "systemd" / name).read_text(encoding="utf-8")
    if " " in binary:
        binary = f'"{binary}"'
    return text.replace("@CSWAPSYNC_BIN@", binary)


def has_refresh_unit() -> bool:
    return not IS_WINDOWS and (UNIT_DIR / REFRESH_UNIT).is_file()


def _targets(gate: bool) -> dict[Path, str]:
    dropins = UNIT_DIR / f"{REFRESH_UNIT}.d"
    out = {dropins / SYNC_DROPIN: SYNC_DROPIN}
    out.update({UNIT_DIR / unit: unit for unit in HEARTBEAT_UNITS})
    if gate:
        out[dropins / GATE_DROPIN] = GATE_DROPIN
    return out


def refresh_hook_installed(gate: bool = False) -> bool:
    """True only when every unit exists AND points at the current binary."""
    try:
        binary = cswapsync_bin()
    except ConfigurationError:
        return False
    return all(dst.is_file() and dst.read_text(encoding="utf-8") == render_unit(src, binary)
               for dst, src in _targets(gate).items())


def lease_gate_installed() -> bool:
    return (UNIT_DIR / f"{REFRESH_UNIT}.d" / GATE_DROPIN).is_file()


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], capture_output=True, check=False, timeout=30)


def install_refresh_hook(gate: bool = False) -> list[str]:
    """Wire Drive sync into the existing cswap-refresh.service (drop-ins, no edits).

    Our own unit files are re-rendered (e.g. after the binary moved); foreign
    files with the same name are never overwritten.
    """
    if not has_refresh_unit():
        raise ConfigurationError("cswap-refresh.service not found; nothing to hook into")
    binary = cswapsync_bin()
    notes: list[str] = [f"units use {binary}"]
    for dst, src in _targets(gate).items():
        content = render_unit(src, binary)
        if dst.exists() and "CSwapSync" not in dst.read_text(encoding="utf-8") \
                and "cswapsync" not in dst.read_text(encoding="utf-8"):
            notes.append(f"{dst.name} exists and is not ours; left as is")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
    old = UNIT_DIR / "cswapsync-postrefresh.service"
    if old.exists():
        _systemctl("disable", old.name)
        old.unlink()
        notes.append("removed old separate cswapsync-postrefresh.service")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", "cswapsync-heartbeat.timer")
    return notes
