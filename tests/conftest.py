"""Shared fixtures: build isolated 'devices' sharing one fake Drive."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cswapsync import cswap_adapter  # noqa: E402
from cswapsync.cswap_adapter import CswapAdapter  # noqa: E402
from cswapsync.encryption import Crypto, KeyManager  # noqa: E402
from fakes import FakeCswap, FakeDriveService  # noqa: E402
from cswapsync.google_drive import DriveStore  # noqa: E402
from cswapsync.settings import Config, Device, Paths, State  # noqa: E402
from cswapsync.sync_manager import SyncManager  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_processes(monkeypatch):
    # Never inspect the developer's real processes (Claude Code runs the tests).
    monkeypatch.setattr(cswap_adapter, "_process_names", lambda: [])


@pytest.fixture
def drive_service():
    return FakeDriveService()


class DeviceHarness:
    def __init__(self, root: Path, device_id: str, service: FakeDriveService,
                 cswap: FakeCswap):
        self.paths = Paths(root / device_id)
        self.paths.ensure()
        self.keys = KeyManager(self.paths.credentials_dir, device_id)
        self.keys.generate()
        self.config = Config(expected_account_count=2, lease_settle_seconds=0,
                             refresh_log=str(root / device_id / "refresh.log"))
        self.device = Device(device_id, "test", "linux")
        self.state = State(self.paths.state_file)
        self.cswap = cswap
        self.service = service
        self.adapter = CswapAdapter("cswap", "cswap-refresh.service",
                                    data_dir=root / device_id / "cswapdata", runner=cswap)
        self._mgr: SyncManager | None = None

    def trust(self, *others: "DeviceHarness") -> None:
        self.config.trusted_devices = [o.keys.public().bundle() for o in others]
        self._mgr = None

    def store(self) -> DriveStore:
        return DriveStore(self.service, "CSwap-Sync", self.state.data["drive_ids"],
                          sleep=lambda s: None)

    @property
    def mgr(self) -> SyncManager:
        if self._mgr is None:
            crypto = Crypto(self.keys, self.config.trusted_devices)
            self._mgr = SyncManager(self.paths, self.config, self.device, self.state,
                                    self.adapter, crypto, self.store, sleep=lambda s: None)
        return self._mgr


@pytest.fixture
def make_device(tmp_path, drive_service):
    def factory(device_id: str = "dell-ubuntu", cswap: FakeCswap | None = None):
        return DeviceHarness(tmp_path, device_id, drive_service, cswap or FakeCswap())
    return factory


@pytest.fixture
def pair(make_device):
    dell = make_device("dell-ubuntu")
    hp = make_device("hp-win10")
    dell.trust(hp)
    hp.trust(dell)
    return dell, hp
