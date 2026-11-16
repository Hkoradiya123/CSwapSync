"""Wires config, device, keys, CSwap adapter and Drive into one context.

Shared by the CLI and the setup wizard.
"""
from __future__ import annotations

from .cswap_adapter import CswapAdapter
from .encryption import Crypto, DevicePublicKey, KeyManager, parse_bundle
from .google_drive import DriveStore, build_service, load_credentials
from .logging_config import setup_logging
from .settings import (Config, ConfigurationError, Device, Paths, State, atomic_write_json,
                      read_json)
from .sync_manager import SyncManager


class App:
    def __init__(self, verbose: bool = False, paths: Paths | None = None, runner=None):
        self.paths = paths or Paths()
        self.paths.ensure()
        self.log = setup_logging(self.paths.logs_dir, verbose)
        self.config = Config.load(self.paths.config_file)
        self.device = Device.load(self.paths.device_file)
        self.state = State(self.paths.state_file)
        self.keys = KeyManager(self.paths.credentials_dir, self.device.device_id)
        kwargs = {"runner": runner} if runner is not None else {}
        self.adapter = CswapAdapter(self.config.cswap_path, self.config.refresh_service,
                                    **kwargs)

    def drive(self, interactive: bool = False) -> DriveStore:
        creds = load_credentials(self.paths.client_secret_file, self.paths.google_token_file,
                                 interactive)
        return DriveStore(build_service(creds), self.config.drive_folder_name,
                          self.state.data["drive_ids"])

    def manager(self) -> SyncManager:
        crypto = Crypto(self.keys, self.config.trusted_devices)
        return SyncManager(self.paths, self.config, self.device, self.state, self.adapter,
                           crypto, self.drive)


def trust_bundle(paths: Paths, own_device_id: str, bundle: str) -> DevicePublicKey:
    """Validate a peer's public bundle and store it in config.json (replacing same id)."""
    peer = parse_bundle(bundle)
    if peer.device_id == own_device_id:
        raise ConfigurationError("that is this device's own key")
    raw = read_json(paths.config_file, default={})
    trusted = [b for b in raw.get("trusted_devices", [])
               if parse_bundle(b).device_id != peer.device_id]
    trusted.append(peer.bundle())
    raw["trusted_devices"] = trusted
    atomic_write_json(paths.config_file, raw)
    return peer
