"""`cswapsync doctor`: read-only diagnostics. Never prints secret values."""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .settings import IS_WINDOWS, Config, Paths, is_private

OK, WARN, FAIL = "OK", "WARN", "FAIL"
TESTED_CSWAP = "0.29."
STRAY_EXPORTS = [Path.home() / "cswap-accounts-export.json"]


def _perm(path: Path, label: str, required: bool) -> tuple[str, str, str]:
    if not path.exists():
        return (FAIL if required else WARN, label, "missing")
    return (OK if is_private(path) else FAIL, label,
            "private" if is_private(path) else "permissions too open (chmod 600/700)")


def _systemd(*args: str) -> str:
    if IS_WINDOWS:
        return "n/a"
    try:
        proc = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                              timeout=15, check=False)
        return proc.stdout.decode().strip() or proc.stderr.decode().strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def run_doctor(paths: Paths, config: Config | None, extra: list[Callable[[], tuple]]
               ) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    add = results.append
    add((OK if sys.version_info >= (3, 10) else FAIL, "python", sys.version.split()[0]))
    for mod in ("pyrage", "cryptography", "googleapiclient", "google_auth_oauthlib"):
        try:
            importlib.import_module(mod)
            add((OK, f"module {mod}", "importable"))
        except ImportError:
            add((FAIL, f"module {mod}", "missing; run scripts/setup.sh"))

    add(_perm(paths.credentials_dir, "credentials/ dir", True))
    add(_perm(paths.credentials_dir / "age-identity.txt", "age private key", True))
    add(_perm(paths.credentials_dir / "signing-key.pem", "signing private key", True))
    add(_perm(paths.client_secret_file, "google-client-secret.json", True))
    add(_perm(paths.google_token_file, "google-token.json (run cswapsync auth)", True))
    add(_perm(paths.backups_dir, "backups/ dir", True))

    if config is not None:
        peers = len(config.trusted_devices)
        add((OK if peers else WARN, "trusted peer devices",
             f"{peers} configured" if peers else
             "none: backups decryptable by THIS device only (add HP with cswapsync trust)"))
        cswap = Path(config.cswap_path)
        add((OK if cswap.exists() else FAIL, "cswap binary", str(cswap)))
        log = config.refresh_log_path
        add((OK if log.exists() else WARN, "refresh log", str(log)))

    if not IS_WINDOWS:
        enabled = _systemd("is-enabled", "cswap-refresh.timer")
        add((OK if enabled == "enabled" else WARN, "cswap-refresh.timer", enabled))
        dropin = Path.home() / ".config/systemd/user/cswap-refresh.service.d/20-cswapsync-sync.conf"
        add((OK if dropin.exists() else WARN, "sync hook in cswap-refresh.service",
             "installed (ExecStartPost)" if dropin.exists() else "missing; run scripts/setup.sh"))
        hb = _systemd("is-enabled", "cswapsync-heartbeat.timer")
        add((OK if hb == "enabled" else WARN, "cswapsync-heartbeat.timer", hb))

    for stray in STRAY_EXPORTS:
        if stray.exists():
            add((WARN, "plaintext export on disk",
                 f"{stray} holds raw refresh tokens; delete it once you trust cswapsync"))

    for check in extra:
        try:
            add(check())
        except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
            add((FAIL, getattr(check, "__name__", "check"), f"{type(exc).__name__}: {exc}"))
    return results


def cswap_version_check(adapter) -> Callable[[], tuple]:
    def cswap_version():
        version = adapter.version()
        return (OK if version.startswith(TESTED_CSWAP) else WARN, "cswap version",
                version + ("" if version.startswith(TESTED_CSWAP)
                           else f" (adapter verified against {TESTED_CSWAP}x)"))
    return cswap_version


def print_results(results: list[tuple[str, str, str]]) -> int:
    width = max(len(name) for _, name, _ in results)
    for status, name, detail in results:
        print(f"[{status:<4}] {name:<{width}}  {detail}")
    fails = sum(1 for s, _, _ in results if s == FAIL)
    warns = sum(1 for s, _, _ in results if s == WARN)
    print(f"\n{len(results)} checks: {fails} fail, {warns} warn")
    return 1 if fails else 0
