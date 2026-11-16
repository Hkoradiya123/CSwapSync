"""Backup manifest: the signed, secret-free description of one encrypted backup.

The manifest is the commit record. A backup "exists" in the cloud only once
current/manifest.json points at it, which is why it is written last.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone

from settings import IntegrityError

SCHEMA_VERSION = 1


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_backup_id() -> str:
    return uuid.uuid4().hex


def canonical_json(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def content_digest(payload: dict) -> str:
    """Hash of the account state, ignoring the export timestamp.

    ``cswap export`` stamps ``exportedAt`` on every run, so the raw payload hash
    changes even when no token changed. Duplicate-upload prevention compares
    this digest instead.
    """
    stable = {k: v for k, v in payload.items() if k != "exportedAt"}
    return sha256_hex(canonical_json(stable))


@dataclass
class Manifest:
    schema_version: int
    backup_id: str
    revision: int
    parent_revision: int | None
    device_id: str
    created_at_utc: str
    encrypted_file_sha256: str
    payload_sha256: str
    content_sha256: str
    account_count: int
    application_version: str
    cswap_version: str
    history_file_name: str
    history_file_id: str = ""
    conflict: bool = False
    signature: str = ""

    def signing_bytes(self) -> bytes:
        body = asdict(self)
        body.pop("signature")
        return canonical_json(body)

    def to_bytes(self) -> bytes:
        return (json.dumps(asdict(self), indent=2, sort_keys=True) + "\n").encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Manifest":
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise IntegrityError("manifest is not valid JSON") from exc
        if not isinstance(obj, dict):
            raise IntegrityError("manifest must be a JSON object")
        if obj.get("schema_version") != SCHEMA_VERSION:
            raise IntegrityError(f"unsupported manifest schema_version "
                                 f"{obj.get('schema_version')!r}")
        names = {f.name for f in fields(cls)}
        required = names - {"history_file_id", "conflict", "signature"}
        missing = required - set(obj)
        if missing:
            raise IntegrityError(f"manifest missing fields: {sorted(missing)}")
        manifest = cls(**{k: obj[k] for k in names if k in obj})
        manifest._validate_types()
        return manifest

    def _validate_types(self) -> None:
        checks = {
            "revision": isinstance(self.revision, int) and self.revision >= 1,
            "parent_revision": self.parent_revision is None or isinstance(self.parent_revision, int),
            "account_count": isinstance(self.account_count, int) and self.account_count >= 1,
            "encrypted_file_sha256": _is_sha(self.encrypted_file_sha256),
            "payload_sha256": _is_sha(self.payload_sha256),
            "content_sha256": _is_sha(self.content_sha256),
            "backup_id": isinstance(self.backup_id, str) and len(self.backup_id) == 32,
            "device_id": isinstance(self.device_id, str) and bool(self.device_id),
        }
        bad = [k for k, ok in checks.items() if not ok]
        if bad:
            raise IntegrityError(f"manifest has invalid fields: {bad}")


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        c in "0123456789abcdef" for c in value)
