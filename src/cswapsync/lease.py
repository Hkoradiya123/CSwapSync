"""Cloud session lease: only one device may use the Claude accounts at a time.

Why: CSwap refresh rotates OAuth refresh tokens. If Dell and HP both use the
same account, whichever refreshes second invalidates the other's token.

Drive has no compare-and-swap, so acquisition is write -> settle -> re-read:
if another device wrote in the same window, the later writer wins the file
and the loser sees a foreign lease_id and backs off. With one human driving
two laptops this window is practically never hit; expiry is the safety net.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .google_drive import DriveStore
from .logging_config import get_logger
from .settings import LeaseError

log = get_logger("lease")
LEASE_FILE = "lease.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@dataclass
class Lease:
    lease_id: str
    device_id: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    released: bool = False

    def active(self, now: datetime) -> bool:
        return not self.released and _parse(self.expires_at) > now


class LeaseManager:
    def __init__(self, drive: DriveStore, device_id: str, ttl_minutes: int,
                 settle_seconds: float = 3.0, clock: Callable[[], datetime] = _utcnow,
                 sleep: Callable[[float], None] = time.sleep):
        self.drive = drive
        self.device_id = device_id
        self.ttl = timedelta(minutes=ttl_minutes)
        self.settle = settle_seconds
        self.clock = clock
        self.sleep = sleep

    def read(self) -> Lease | None:
        got = self.drive.get_stable("status", LEASE_FILE)
        if got is None:
            return None
        try:
            obj = json.loads(got[1])
            return Lease(**{k: obj[k] for k in Lease.__dataclass_fields__ if k in obj})
        except (ValueError, TypeError) as exc:
            raise LeaseError("cloud lease.json is corrupt; inspect status/ in Drive") from exc

    def _write(self, lease: Lease) -> None:
        self.drive.put_stable("status", LEASE_FILE,
                              json.dumps(asdict(lease), indent=2).encode(), "application/json")

    def holder(self) -> Lease | None:
        lease = self.read()
        return lease if lease and lease.active(self.clock()) else None

    def acquire(self, break_foreign: bool = False) -> tuple[Lease, str | None]:
        """Returns (lease, recovered_from_device_or_None). Fails closed on any error."""
        now = self.clock()
        current = self.read()
        recovered = None
        if current and current.active(now):
            if current.device_id == self.device_id:
                return self._renew(current), None
            if not break_foreign:
                raise LeaseError(f"lease held by '{current.device_id}' until "
                                 f"{current.expires_at}; finish there with "
                                 f"'cswapsync push', or wait for expiry")
            log.warning("BREAKING active lease of %s on explicit request", current.device_id)
            recovered = current.device_id
        elif current and not current.released and current.device_id != self.device_id:
            log.warning("recovering EXPIRED lease abandoned by %s (expired %s); that device "
                        "may hold unpushed credentials", current.device_id, current.expires_at)
            recovered = current.device_id

        lease = Lease(uuid.uuid4().hex, self.device_id, _iso(now), _iso(now),
                      _iso(now + self.ttl))
        self._write(lease)
        self.sleep(self.settle)
        confirm = self.read()
        if confirm is None or confirm.lease_id != lease.lease_id:
            raise LeaseError("lost lease race to another device; retry later")
        log.info("lease acquired by %s until %s", self.device_id, lease.expires_at)
        return lease, recovered

    def _renew(self, lease: Lease) -> Lease:
        now = self.clock()
        lease.heartbeat_at = _iso(now)
        lease.expires_at = _iso(now + self.ttl)
        self._write(lease)
        return lease

    def require_held(self) -> Lease:
        lease = self.read()
        if not lease or not lease.active(self.clock()) or lease.device_id != self.device_id:
            who = f"'{lease.device_id}'" if lease and lease.active(self.clock()) else "nobody"
            raise LeaseError(f"this device does not hold the lease (holder: {who}); "
                             f"run 'cswapsync pull' first")
        return lease

    def heartbeat(self) -> Lease:
        return self._renew(self.require_held())

    def release(self) -> None:
        lease = self.require_held()
        lease.released = True
        lease.heartbeat_at = _iso(self.clock())
        self._write(lease)
        log.info("lease released by %s", self.device_id)
