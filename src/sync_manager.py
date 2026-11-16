"""Backup / push / pull / restore orchestration.

Commit protocol for a push (each step must succeed before the next):
  1. export (memory) -> validate -> encrypt -> sign
  2. upload history/r<rev>-<id>.age (new immutable file) and verify by re-download
  3. upload history manifest
  4. re-check that cloud revision did not move (conflict guard)
  5. update current/backup.age in place (stable id) and verify
  6. update current/manifest.json  <- COMMIT POINT
A crash before 6 leaves the previous manifest valid; readers verify the
payload hash and fall back to the immutable history file the manifest names.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cswap_adapter import CswapAdapter, PayloadSummary, validate_export_payload
from encryption import Crypto
from google_drive import DriveStore
from lease import LeaseManager
from logging_config import get_logger
from manifest import (SCHEMA_VERSION, Manifest, content_digest, new_backup_id,
                      sha256_hex, utc_now_iso)
from settings import (Config, ConflictError, Device, IntegrityError, LeaseError,
                      Paths, RefreshNotHealthyError, State, SyncError, atomic_write_bytes)

log = get_logger("sync")
APP_VERSION = "1.0.0"
AGE_MIME = "application/octet-stream"
JSON_MIME = "application/json"
LOCAL_FAMILIES = {
    "": re.compile(r"^r\d{6}-[0-9a-f]{32}\.age$"),
    "conflict-": re.compile(r"^conflict-r\d{6}-[0-9a-f]{32}\.age$"),
    "rollback-": re.compile(r"^rollback-\d{8}T\d{6}Z-r\d{6}-[0-9a-f]{32}\.age$"),
}
REFRESH_BAD = re.compile(r"re-login needed|invalid_grant|belongs to another account|"
                         r"refresh deferred", re.I)


@dataclass
class VerifiedBackup:
    manifest: Manifest
    plaintext: bytes
    summary: PayloadSummary


class SyncManager:
    def __init__(self, paths: Paths, config: Config, device: Device, state: State,
                 adapter: CswapAdapter, crypto: Crypto,
                 drive_factory: Callable[[], DriveStore],
                 sleep: Callable[[float], None] = time.sleep):
        self.paths, self.config, self.device, self.state = paths, config, device, state
        self.adapter, self.crypto = adapter, crypto
        self._drive_factory = drive_factory
        self._drive: DriveStore | None = None
        self.sleep = sleep

    # ------------------------------------------------------------------ wiring
    @property
    def drive(self) -> DriveStore:
        if self._drive is None:
            self._drive = self._drive_factory()
            self._drive.ensure_layout()
            self.state.save()  # drive id cache lives inside state.data
        return self._drive

    @property
    def lease(self) -> LeaseManager:
        return LeaseManager(self.drive, self.device.device_id, self.config.lease_ttl_minutes,
                            self.config.lease_settle_seconds, sleep=self.sleep)

    # ------------------------------------------------------------------ building blocks
    def _export(self) -> tuple[bytes, PayloadSummary, str]:
        self.adapter.ensure_idle()
        plaintext = self.adapter.export_payload()
        summary = validate_export_payload(plaintext, self.config.expected_account_count)
        return plaintext, summary, content_digest(json.loads(plaintext))

    def _seal(self, plaintext: bytes, summary: PayloadSummary, digest: str, revision: int,
              parent: int | None, conflict: bool = False, self_only: bool = False
              ) -> tuple[bytes, Manifest]:
        ciphertext = self.crypto.encrypt(plaintext, self_only=self_only)
        # Prove the ciphertext round-trips before it goes anywhere.
        if sha256_hex(self.crypto.decrypt(ciphertext)) != sha256_hex(plaintext):
            raise IntegrityError("encryption round-trip check failed")
        backup_id = new_backup_id()
        manifest = Manifest(
            schema_version=SCHEMA_VERSION, backup_id=backup_id, revision=revision,
            parent_revision=parent, device_id=self.device.device_id,
            created_at_utc=utc_now_iso(), encrypted_file_sha256=sha256_hex(ciphertext),
            payload_sha256=sha256_hex(plaintext), content_sha256=digest,
            account_count=summary.account_count, application_version=APP_VERSION,
            cswap_version=summary.swap_version,
            history_file_name=f"r{revision:06d}-{backup_id}.age", conflict=conflict)
        return ciphertext, manifest

    def _sign(self, manifest: Manifest) -> Manifest:
        manifest.signature = self.crypto.sign(manifest.signing_bytes())
        return manifest

    def _verify(self, manifest: Manifest, ciphertext: bytes) -> VerifiedBackup:
        self.crypto.verify(manifest.device_id, manifest.signing_bytes(), manifest.signature)
        if sha256_hex(ciphertext) != manifest.encrypted_file_sha256:
            raise IntegrityError("encrypted file SHA256 does not match manifest")
        plaintext = self.crypto.decrypt(ciphertext)
        if sha256_hex(plaintext) != manifest.payload_sha256:
            raise IntegrityError("decrypted payload SHA256 does not match manifest")
        summary = validate_export_payload(plaintext, self.config.expected_account_count)
        if summary.account_count != manifest.account_count:
            raise IntegrityError("account count differs from manifest")
        return VerifiedBackup(manifest, plaintext, summary)

    # ------------------------------------------------------------------ local backups
    def _store_local(self, ciphertext: bytes, manifest: Manifest, prefix: str = "") -> Path:
        stem = prefix + manifest.history_file_name.removesuffix(".age")
        path = self.paths.backups_dir / f"{stem}.age"
        atomic_write_bytes(path, ciphertext)
        atomic_write_bytes(path.with_suffix(".manifest.json"), manifest.to_bytes())
        self._prune_local("rollback-" if prefix.startswith("rollback-") else prefix)
        return path

    def _prune_local(self, family: str) -> None:
        """Keep the newest ``local_keep`` files per family (backup/conflict/rollback)."""
        rx = LOCAL_FAMILIES[family]
        files = sorted((p for p in self.paths.backups_dir.glob("*.age") if rx.match(p.name)),
                       key=lambda p: p.stat().st_mtime)
        for old in files[:-self.config.local_keep]:
            old.unlink(missing_ok=True)
            old.with_suffix(".manifest.json").unlink(missing_ok=True)

    def load_local(self, path: Path) -> VerifiedBackup:
        manifest_path = path.with_suffix(".manifest.json")
        if not path.exists() or not manifest_path.exists():
            raise SyncError(f"backup or manifest missing for {path.name}")
        return self._verify(Manifest.from_bytes(manifest_path.read_bytes()), path.read_bytes())

    def latest_rollback(self) -> Path:
        files = sorted(self.paths.backups_dir.glob("rollback-*.age"),
                       key=lambda p: p.stat().st_mtime)
        if not files:
            raise SyncError("no rollback snapshot found in backups/")
        return files[-1]

    def backup(self) -> Path:
        """Local-only encrypted backup (no cloud, no lease)."""
        plaintext, summary, digest = self._export()
        base = self.state.get("last_synced_revision")
        ciphertext, manifest = self._seal(plaintext, summary, digest, (base or 0) + 1, base)
        path = self._store_local(ciphertext, self._sign(manifest))
        log.info("local encrypted backup written: %s (%d accounts)", path.name,
                 summary.account_count)
        return path

    # ------------------------------------------------------------------ cloud reads
    def cloud_manifest(self) -> Manifest | None:
        got = self.drive.get_stable("current", "manifest.json")
        return Manifest.from_bytes(got[1]) if got else None

    def fetch_cloud(self, manifest: Manifest) -> VerifiedBackup:
        got = self.drive.get_stable("current", "backup.age")
        if got and sha256_hex(got[1]) == manifest.encrypted_file_sha256:
            return self._verify(manifest, got[1])
        log.warning("current/backup.age does not match manifest (interrupted push?); "
                    "using immutable history copy")
        if not manifest.history_file_id:
            raise IntegrityError("manifest has no history file to fall back to")
        return self._verify(manifest, self.drive.download(manifest.history_file_id))

    def fetch_history(self, revision: int) -> VerifiedBackup:
        prefix = f"r{revision:06d}-"
        history = self.drive.folder("history")
        manifests = [f for f in self.drive.list_children(history)
                     if f["name"].startswith(prefix) and f["name"].endswith(".manifest.json")]
        if len(manifests) != 1:
            raise SyncError(f"expected one history manifest for r{revision}, "
                            f"found {len(manifests)}")
        manifest = Manifest.from_bytes(self.drive.download(manifests[0]["id"]))
        return self._verify(manifest, self.drive.download(manifest.history_file_id))

    # ------------------------------------------------------------------ push
    def push(self, release: bool = True, accept_conflict: bool = False) -> Manifest | None:
        self.lease.require_held()  # fail closed: Drive unreachable -> exception here
        plaintext, summary, digest = self._export()
        cloud = self.cloud_manifest()
        base = self.state.get("last_synced_revision")

        if cloud and cloud.content_sha256 == digest:
            log.info("cloud revision %d already holds this exact state; nothing to upload",
                     cloud.revision)
            self.state.set(last_synced_revision=cloud.revision, last_content_sha256=digest)
            self._maybe_release(release)
            return None

        if cloud and cloud.revision != base and not accept_conflict:
            self._record_conflict(plaintext, summary, digest, cloud, base)

        parent = cloud.revision if cloud else None
        revision = (cloud.revision if cloud else 0) + 1
        ciphertext, manifest = self._seal(plaintext, summary, digest, revision, parent)
        self._upload_history(ciphertext, manifest)
        self._store_local(ciphertext, manifest)

        latest = self.cloud_manifest()  # narrow race guard before the commit point
        if (latest.revision if latest else None) != parent:
            raise ConflictError("cloud revision changed during push; nothing committed. "
                                "Run 'cswapsync status' and retry.")
        current_meta = self.drive.put_stable("current", "backup.age", ciphertext, AGE_MIME)
        self.drive.verify_remote(current_meta, manifest.encrypted_file_sha256)
        self.drive.put_stable("current", "manifest.json", manifest.to_bytes(), JSON_MIME)
        self._clear_conflict()

        self.state.set(last_synced_revision=revision, last_content_sha256=digest,
                       last_backup_id=manifest.backup_id, last_push_utc=utc_now_iso())
        log.info("pushed revision %d (%d accounts), cloud checksum verified", revision,
                 summary.account_count)
        self._prune_cloud_history(manifest)
        self._maybe_release(release)
        return manifest

    def _upload_history(self, ciphertext: bytes, manifest: Manifest) -> None:
        meta = self.drive.create_unique("history", manifest.history_file_name, ciphertext,
                                        AGE_MIME)
        self.drive.verify_remote(meta, manifest.encrypted_file_sha256)
        manifest.history_file_id = meta["id"]
        self._sign(manifest)
        self.drive.create_unique("history",
                                 manifest.history_file_name.replace(".age", ".manifest.json"),
                                 manifest.to_bytes(), JSON_MIME)

    def _record_conflict(self, plaintext: bytes, summary: PayloadSummary, digest: str,
                         cloud: Manifest, base: int | None) -> None:
        """Keep BOTH states, touch nothing current, demand a human decision."""
        ciphertext, manifest = self._seal(plaintext, summary, digest, cloud.revision + 1,
                                          base, conflict=True)
        manifest.history_file_name = f"conflict-{manifest.history_file_name}"
        self._upload_history(ciphertext, manifest)
        self._store_local(ciphertext, manifest, prefix="conflict-")
        note = {"device_id": self.device.device_id, "backup_id": manifest.backup_id,
                "local_base_revision": base, "cloud_revision": cloud.revision,
                "cloud_device_id": cloud.device_id, "created_at_utc": utc_now_iso(),
                "resolved": False}
        self.drive.put_stable("status", "conflict.json",
                              json.dumps(note, indent=2).encode(), JSON_MIME)
        raise ConflictError(
            f"revision conflict: this device last synced r{base}, cloud is r{cloud.revision} "
            f"(from {cloud.device_id}). Both states preserved (local + history/conflict-*). "
            f"Reconcile: 'cswapsync pull --yes --accept-cloud' to take cloud, or "
            f"'cswapsync push --accept-conflict' to make this device's state current.")

    def _clear_conflict(self) -> None:
        if self.drive.get_stable("status", "conflict.json"):
            self.drive.put_stable("status", "conflict.json",
                                  json.dumps({"resolved": True,
                                              "resolved_at_utc": utc_now_iso()}).encode(),
                                  JSON_MIME)

    def _prune_cloud_history(self, current: Manifest) -> None:
        files = [f for f in self.drive.list_children(self.drive.folder("history"))
                 if f["name"].startswith("r") and f["name"].endswith(".age")]
        files.sort(key=lambda f: f["name"])
        for old in files[:-self.config.history_keep]:
            if old["id"] == current.history_file_id:
                continue
            manifest = self.drive.find(old["name"].replace(".age", ".manifest.json"),
                                       self.drive.folder("history"))
            self.drive.delete(old["id"])
            if manifest:
                self.drive.delete(manifest["id"])
            log.info("pruned cloud history %s", old["name"])

    def _maybe_release(self, release: bool) -> None:
        if release:
            self.lease.release()

    # ------------------------------------------------------------------ pull / restore
    def pull(self, apply: bool, accept_cloud: bool = False, break_lease: bool = False,
             bootstrap: bool = False) -> str:
        _, recovered = self.lease.acquire(break_foreign=break_lease)
        if recovered:
            log.warning("lease recovered from %s; its latest state may never have been "
                        "pushed", recovered)
        cloud = self.cloud_manifest()
        base = self.state.get("last_synced_revision")
        if cloud is None:
            return "lease acquired; cloud has no backup yet (run 'cswapsync push' first)"
        verified = self.fetch_cloud(cloud)  # verify even when we will not apply
        if bootstrap:
            local_digest = None  # fresh device: nothing local worth preserving
        else:
            _, _, local_digest = self._export()
        if local_digest == cloud.content_sha256:
            self.state.set(last_synced_revision=cloud.revision, last_content_sha256=local_digest)
            return f"lease acquired; local state already equals cloud r{cloud.revision}"
        if local_digest is not None and cloud.revision == base:
            # Cloud has nothing newer than what we last synced: local is simply ahead
            # (unpushed use/refresh). Restoring would roll fresh tokens back. Never do it.
            return (f"lease acquired; local is ahead of cloud r{base} (unpushed changes); "
                    f"run 'cswapsync push' when done")
        diverged = local_digest is not None and \
            local_digest != self.state.get("last_content_sha256")
        if diverged and not accept_cloud:
            path = self.backup()
            raise ConflictError(
                f"local accounts changed since last sync (r{base}) AND differ from cloud "
                f"r{cloud.revision}. Local state saved to backups/{path.name}. Re-run with "
                f"'--yes --accept-cloud' to take cloud, or 'cswapsync push --accept-conflict'.")
        if not apply:
            return (f"lease acquired; cloud r{cloud.revision} from {cloud.device_id} "
                    f"({cloud.created_at_utc}) verified and is newer than local r{base}. "
                    f"Re-run with --yes to restore it.")
        self._apply(verified, bootstrap=bootstrap)
        self.state.set(last_synced_revision=cloud.revision,
                       last_content_sha256=cloud.content_sha256)
        return f"restored cloud r{cloud.revision}; lease held by {self.device.device_id}"

    def restore(self, verified: VerifiedBackup) -> Path:
        return self._apply(verified)

    def _apply(self, verified: VerifiedBackup, bootstrap: bool = False) -> Path | None:
        """Replace CSwap account state. All checks first; rollback snapshot before writes."""
        self.adapter.ensure_idle(require_claude_stopped=True)
        if bootstrap:
            log.warning("bootstrap restore: no local accounts, no rollback snapshot")
            self.adapter.import_payload(verified.plaintext)
            return None
        current_plain = self.adapter.export_payload()
        # Snapshot whatever exists now, even if it does not match expected_account_count.
        cur_summary = validate_export_payload(current_plain, expected_count=None)
        rb_cipher, rb_manifest = self._seal(current_plain, cur_summary,
                                            content_digest(json.loads(current_plain)),
                                            self.state.get("last_synced_revision") or 1,
                                            self.state.get("last_synced_revision"),
                                            self_only=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rollback = self._store_local(rb_cipher, self._sign(rb_manifest),
                                     prefix=f"rollback-{stamp}-")
        log.info("rollback snapshot saved: %s", rollback.name)
        live = self.adapter.live_slot()
        try:
            self.adapter.import_payload(verified.plaintext)
            if live is not None and live in verified.summary.slots:
                self.adapter.activate(live)
        except SyncError:
            log.error("restore failed mid-way; re-importing rollback snapshot")
            self.adapter.import_payload(current_plain)
            if live is not None:
                self.adapter.activate(live)
            raise
        log.info("restore applied (%d accounts); rollback: %s",
                 verified.summary.account_count, rollback.name)
        return rollback

    # ------------------------------------------------------------------ 10 PM / refresh hook
    def check_refresh(self) -> None:
        deadline = time.monotonic() + self.config.refresh_wait_timeout_seconds
        while not self.adapter.inside_refresh_unit and \
                self.adapter.refresh_state() in ("active", "activating"):
            if time.monotonic() > deadline:
                raise RefreshNotHealthyError("refresh still running after wait timeout")
            self.sleep(10)
        log_path = self.config.refresh_log_path
        if not log_path.exists():
            raise RefreshNotHealthyError(f"refresh log not found: {log_path}")
        text = log_path.read_text(encoding="utf-8", errors="replace")
        start = text.rfind("\n=== ") + 1 if "\n=== " in text else text.find("=== ")
        if start < 0:
            raise RefreshNotHealthyError("refresh log has no run header")
        block = text[start:]
        header = block.splitlines()[0].strip("= ").strip()
        ran_at = datetime.fromisoformat(header)
        age_min = (datetime.now(timezone.utc) - ran_at).total_seconds() / 60
        if age_min > self.config.refresh_max_age_minutes:
            raise RefreshNotHealthyError(f"last refresh ran {age_min:.0f} min ago; too old")
        exits = re.findall(r"^exit=(\d+)$", block, re.M)
        if not exits or exits[-1] != "0":
            raise RefreshNotHealthyError("last refresh run did not exit 0")
        if REFRESH_BAD.search(block):  # never log the block itself
            raise RefreshNotHealthyError("last refresh reported an account needing attention")

    def check_health(self) -> dict[int, str]:
        health = self.adapter.health()
        bad = {slot: st for slot, st in health.items() if st != "ok"}
        if len(health) != self.config.expected_account_count or bad:
            raise RefreshNotHealthyError(f"account health not ok: {bad or health}")
        return health

    def post_refresh(self, inline: bool = False) -> Manifest | None:
        """inline=True: called as ExecStartPost of the refresh unit itself."""
        self.adapter.inside_refresh_unit = inline
        self.check_refresh()
        self.check_health()
        holder = self.lease.holder()
        if holder and holder.device_id != self.device.device_id:
            raise LeaseError(f"refresh ran here but '{holder.device_id}' holds the lease; "
                             f"NOT pushing. Tokens may now be rotated on both devices — "
                             f"run 'cswapsync doctor' on both.")
        own_session = holder is not None
        if not own_session:
            self.lease.acquire()
        try:
            return self.push(release=not own_session)
        except Exception:
            if not own_session:
                self.lease.release()  # never strand a lease we took only for this run
            raise
