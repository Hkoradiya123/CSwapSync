"""Paths, configuration, local sync state and filesystem primitives.

Every other module depends on this one, never the other way round. Nothing in
here talks to the network or to CSwap.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_HISTORY = 10  # hard floor: spec requires at least 10 verified historical backups
IS_WINDOWS = sys.platform.startswith("win")


# --------------------------------------------------------------------------- errors
class SyncError(Exception):
    """Base error. ``exit_code`` is what the CLI returns for this failure class."""

    exit_code = 1


class ConfigurationError(SyncError):
    exit_code = 2


class ConflictError(SyncError):
    exit_code = 4


class LeaseError(SyncError):
    exit_code = 5


class IntegrityError(SyncError):
    exit_code = 6


class CswapBusyError(SyncError):
    exit_code = 7


class CloudUnavailableError(SyncError):
    exit_code = 8


class RefreshNotHealthyError(SyncError):
    exit_code = 9


# --------------------------------------------------------------------------- paths
class Paths:
    def __init__(self, root: Path | None = None):
        # Data (keys, config, backups) lives OUTSIDE the installed code so that
        # `uv tool upgrade` / `pipx upgrade` never touch it.
        env_root = os.environ.get("CSWAPSYNC_HOME")
        self.root = Path(root or env_root or Path.home() / "CSwapSync").expanduser()
        self.config_dir = self.root / "config"
        self.credentials_dir = self.root / "credentials"
        self.staging_dir = self.root / "staging"
        self.logs_dir = self.root / "logs"
        self.backups_dir = self.root / "backups"
        self.config_file = self.config_dir / "config.json"
        self.device_file = self.config_dir / "device.json"
        self.state_file = self.config_dir / "state.json"
        self.client_secret_file = self.credentials_dir / "google-client-secret.json"
        self.google_token_file = self.credentials_dir / "google-token.json"
        self.lock_file = self.staging_dir / ".cswapsync.lock"

    def ensure(self) -> None:
        for d in (self.config_dir, self.credentials_dir, self.staging_dir,
                  self.logs_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)
            secure_chmod(d, 0o700)


# --------------------------------------------------------------------------- fs helpers
def secure_chmod(path: Path, mode: int) -> None:
    if not IS_WINDOWS:
        os.chmod(path, mode)


def is_private(path: Path) -> bool:
    """True when no group/other permission bits are set (always True on Windows)."""
    if IS_WINDOWS:
        return True
    return (path.stat().st_mode & 0o077) == 0


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write via same-directory temp file + fsync + rename.

    mkstemp creates the file 0600 from the start, so content is never briefly
    world-readable. A crash leaves either the old file or the new one, never
    a truncated mix.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        secure_chmod(Path(tmp), mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if not IS_WINDOWS:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode())


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{path.name} is not valid JSON: {exc}") from exc


class ProcessLock:
    """Non-blocking exclusive lock so a timer run and a manual run never overlap."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if IS_WINDOWS:
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise SyncError("another cswapsync operation is already running") from exc
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fh is not None:
            self._fh.close()  # closing the fd releases flock/msvcrt lock
            self._fh = None


# --------------------------------------------------------------------------- config
@dataclass
class Config:
    cswap_bin: str = "~/.local/bin/cswap"
    drive_folder_name: str = "CSwap-Sync"
    expected_account_count: int = 2
    history_keep: int = MIN_HISTORY
    local_keep: int = MIN_HISTORY
    lease_ttl_minutes: int = 720
    lease_settle_seconds: float = 3.0
    refresh_service: str = "cswap-refresh.service"
    refresh_log: str = "~/.local/state/cswap-refresh/refresh.log"
    refresh_max_age_minutes: int = 45
    refresh_wait_timeout_seconds: int = 900
    trusted_devices: list[str] = field(default_factory=list)

    def defaults_json(self) -> dict:
        from dataclasses import asdict
        return {"_comment": "No secrets belong in this file.", **asdict(self)}

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = read_json(path, default={})
        if not isinstance(raw, dict):
            raise ConfigurationError("config.json must be a JSON object")
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known - {"_comment"}
        if unknown:
            raise ConfigurationError(f"unknown config keys: {sorted(unknown)}")
        cfg = cls(**{k: v for k, v in raw.items() if k in known})
        cfg.validate()
        return cfg

    def validate(self) -> None:
        ints = ("expected_account_count", "history_keep", "local_keep", "lease_ttl_minutes",
                "refresh_max_age_minutes", "refresh_wait_timeout_seconds")
        for name in ints:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigurationError(f"config.{name} must be a positive integer")
        # Never allow history below the floor even if the user configures less.
        self.history_keep = max(self.history_keep, MIN_HISTORY)
        self.local_keep = max(self.local_keep, MIN_HISTORY)
        if not isinstance(self.trusted_devices, list) or not all(
                isinstance(x, str) for x in self.trusted_devices):
            raise ConfigurationError("config.trusted_devices must be a list of strings")

    @property
    def cswap_path(self) -> str:
        return os.path.expanduser(self.cswap_bin)

    @property
    def refresh_log_path(self) -> Path:
        return Path(os.path.expanduser(self.refresh_log))


@dataclass
class Device:
    device_id: str
    role: str
    platform: str

    @classmethod
    def load(cls, path: Path) -> "Device":
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{path} missing; run: cswapsync setup")
        device_id = raw.get("device_id", "")
        if not isinstance(device_id, str) or not device_id.replace("-", "").isalnum():
            raise ConfigurationError("device.json device_id must be alphanumeric/dashes")
        return cls(device_id=device_id, role=str(raw.get("role", "")),
                   platform=str(raw.get("platform", "")))


class State:
    """Local sync bookkeeping (no secrets): last synced revision, Drive IDs."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = read_json(path, default={}) or {}
        self.data.setdefault("drive_ids", {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, **values: Any) -> None:
        self.data.update(values)
        self.save()

    def save(self) -> None:
        atomic_write_json(self.path, self.data)
