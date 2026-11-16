"""The only module that talks to CSwap (claude-swap). Verified against v0.29.0.

Supported operations used (read from claude_swap/cli.py + transfer.py source):
  cswap --version
  cswap export -            JSON envelope v1 on stdout (slim: oauthAccount + claudeAiOauth)
  cswap import - --force    JSON envelope v1 on stdin; validates every account before writing
  cswap switch <n> --force  activate the stored credentials of slot n as the live login
  cswap list --json         per-account usageStatus (no token material)
  cswap status --json       live account identity (no token material)

Plaintext credentials only ever exist in this process's memory and in the
pipe to/from cswap. They are never written to disk and never logged.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from logging_config import get_logger
from settings import IS_WINDOWS, CswapBusyError, IntegrityError, SyncError

log = get_logger("cswap")

SUPPORTED_EXPORT_VERSION = 1
BUSY_REFRESH_STATES = {"active", "activating", "reloading", "deactivating"}

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class PayloadSummary:
    account_count: int
    slots: tuple[int, ...]
    active_slot: int | None
    swap_version: str


def default_cswap_data_dir() -> Path:
    if IS_WINDOWS or os.uname().sysname == "Darwin":
        return Path.home() / ".claude-swap-backup"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg and os.path.isabs(os.path.expanduser(xdg)) \
        else Path.home() / ".local" / "share"
    return base / "claude-swap"


def validate_export_payload(raw: bytes, expected_count: int | None) -> PayloadSummary:
    """Validate a cswap export envelope structurally. Never echoes values."""
    try:
        env = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IntegrityError("backup payload is not valid JSON") from exc
    if not isinstance(env, dict):
        raise IntegrityError("backup payload must be a JSON object")
    if env.get("version") != SUPPORTED_EXPORT_VERSION:
        raise IntegrityError(f"unsupported cswap export version {env.get('version')!r}")
    if env.get("encrypted") is not False:
        raise IntegrityError("cswap payload flagged as encrypted; unsupported")
    accounts = env.get("accounts")
    if not isinstance(accounts, list):
        raise IntegrityError("payload has no accounts list")
    if not accounts:
        raise IntegrityError("payload has no accounts")
    if expected_count is not None and len(accounts) != expected_count:
        raise IntegrityError(f"payload has {len(accounts)} accounts, expected {expected_count}")

    slots: list[int] = []
    for idx, acc in enumerate(accounts):
        if not isinstance(acc, dict):
            raise IntegrityError(f"account #{idx} is not an object")
        number = acc.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise IntegrityError(f"account #{idx} has invalid slot number")
        if not isinstance(acc.get("email"), str) or "@" not in acc["email"]:
            raise IntegrityError(f"account slot {number} has invalid email")
        config = acc.get("config")
        if not isinstance(config, dict) or not isinstance(config.get("oauthAccount"), dict):
            raise IntegrityError(f"account slot {number} missing config.oauthAccount")
        creds = acc.get("credentials")
        if acc.get("kind") == "api_key":
            if not isinstance(creds, str) or not creds:
                raise IntegrityError(f"account slot {number} has empty API key")
        else:
            oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
            if not isinstance(oauth, dict) or not oauth.get("accessToken") \
                    or not oauth.get("refreshToken"):
                raise IntegrityError(f"account slot {number} missing OAuth access/refresh token")
        slots.append(number)
    if len(set(slots)) != len(slots):
        raise IntegrityError("payload has duplicate slot numbers")
    active = env.get("activeAccountNumber")
    return PayloadSummary(len(accounts), tuple(sorted(slots)),
                          active if isinstance(active, int) else None,
                          str(env.get("swapVersion", "")))


class CswapAdapter:
    def __init__(self, cswap_bin: str, refresh_service: str,
                 data_dir: Path | None = None, runner: Runner = subprocess.run):
        self.cswap_bin = cswap_bin
        self.refresh_service = refresh_service
        self.data_dir = data_dir or default_cswap_data_dir()
        self._run_proc = runner
        # True when we run INSIDE the refresh unit (ExecStartPost): its state is
        # "activating" then, which is us, not a concurrent refresh.
        self.inside_refresh_unit = False

    # ------------------------------------------------------------------ plumbing
    def _run(self, *args: str, stdin: bytes | None = None, timeout: int = 180,
             secret_output: bool = False) -> bytes:
        cmd = [self.cswap_bin, *args]
        try:
            proc = self._run_proc(cmd, input=stdin, capture_output=True, timeout=timeout,
                                  check=False)
        except FileNotFoundError as exc:
            raise SyncError(f"cswap binary not found at {self.cswap_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SyncError(f"cswap {args[0]} timed out after {timeout}s") from exc
        if proc.returncode != 0:
            # stderr of export/import is status text (no tokens), but keep it short
            # and never include stdout, which may hold credentials.
            err = (proc.stderr or b"").decode(errors="replace").strip().splitlines()[-3:]
            raise SyncError(f"cswap {args[0]} failed (exit {proc.returncode}): {' | '.join(err)}")
        if not secret_output:
            log.debug("cswap %s ok", args[0])
        return proc.stdout or b""

    # ------------------------------------------------------------------ read-only
    def version(self) -> str:
        return self._run("--version", timeout=30).decode().strip().split()[-1]

    def health(self) -> dict[int, str]:
        """slot -> usageStatus via ``cswap list --json`` (network: usage fetch)."""
        raw = self._run("list", "--json", timeout=180)
        try:
            data = json.loads(raw)
            return {int(a["number"]): str(a.get("usageStatus", "unknown"))
                    for a in data["accounts"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise SyncError("could not parse cswap list --json output") from exc

    def live_slot(self) -> int | None:
        raw = self._run("status", "--json", timeout=60)
        try:
            active = json.loads(raw).get("active") or {}
            number = active.get("number")
            return number if isinstance(number, int) else None
        except ValueError as exc:
            raise SyncError("could not parse cswap status --json output") from exc

    def refresh_state(self) -> str:
        if IS_WINDOWS:
            return "n/a"
        try:
            proc = self._run_proc(["systemctl", "--user", "is-active", self.refresh_service],
                                  capture_output=True, timeout=15, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "unknown"
        return (proc.stdout or b"").decode().strip() or "unknown"

    def busy_reasons(self, require_claude_stopped: bool = False) -> list[str]:
        reasons: list[str] = []
        procs = _process_names()
        if any(_is_cswap_process(argv) for _, argv in procs):
            reasons.append("a cswap process (tui/watch/auto/switch) is running")
        if require_claude_stopped and any(name == "claude" for name, _ in procs):
            reasons.append("Claude Code is running (close it before restoring)")
        if not self.inside_refresh_unit and self.refresh_state() in BUSY_REFRESH_STATES:
            reasons.append(f"{self.refresh_service} is running")
        if self._cswap_lock_held():
            reasons.append("cswap internal lock is held (switch/refresh in progress)")
        return reasons

    def ensure_idle(self, require_claude_stopped: bool = False) -> None:
        reasons = self.busy_reasons(require_claude_stopped)
        if reasons:
            raise CswapBusyError("CSwap busy: " + "; ".join(reasons))

    def _cswap_lock_held(self) -> bool:
        lock = self.data_dir / ".lock"
        if IS_WINDOWS or not lock.exists():
            return False
        import fcntl
        with open(lock, "a") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False

    # ------------------------------------------------------------------ credential I/O
    def export_payload(self) -> bytes:
        """Plaintext export envelope, in memory only."""
        data = self._run("export", "-", secret_output=True)
        log.debug("cswap export produced %d bytes", len(data))
        return data

    def import_payload(self, payload: bytes) -> None:
        self._run("import", "-", "--force", stdin=payload, secret_output=True)
        log.info("cswap import completed")

    def activate(self, slot: int) -> None:
        self._run("switch", str(slot), "--force", timeout=120)
        log.info("activated slot %s as live login", slot)


CSWAP_EXECUTABLES = {"cswap", "claude-swap", "cswap.exe", "claude-swap.exe", "cswap-refresh.sh"}


def _is_cswap_process(argv: list[str]) -> bool:
    """Exact executable match on argv[0] or the script argv[1] (python/bash launched).

    Substring matching would flag our own ``cswapsync`` wrapper as busy.
    """
    return any(Path(a).name in CSWAP_EXECUTABLES for a in argv[:2])


def _process_names() -> list[tuple[str, list[str]]]:
    """(comm, argv) for every other process. Linux /proc; Windows tasklist."""
    me = os.getpid()
    out: list[tuple[str, list[str]]] = []
    if IS_WINDOWS:
        try:
            raw = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                                 timeout=15, check=False).stdout.decode(errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return out
        for line in raw.splitlines():
            name = line.split(",")[0].strip('"').lower().removesuffix(".exe")
            out.append((name, [name]))
        return out
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in (me, os.getppid()):
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        out.append((comm, [a for a in argv if a]))
    return out
