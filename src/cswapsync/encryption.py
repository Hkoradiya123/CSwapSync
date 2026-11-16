"""Device keys, age encryption and Ed25519 manifest signatures.

Why two primitives:
- age (X25519 + ChaCha20-Poly1305) gives confidentiality and tamper detection,
  but anyone holding a *public* recipient key can produce a valid ciphertext.
- Ed25519 signatures over the manifest prove *which device* produced a backup,
  so a compromised Drive account cannot inject credentials of its choosing.

Each device has its own identity. A payload is encrypted to every trusted
device so either side can decrypt. Private keys never leave this machine.

Public bundle (the one string exchanged with the other device):
    cswapsync1:<device_id>:<age1...>:<ed25519-public-key-base64url>
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import pyrage
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .settings import (
    ConfigurationError,
    IntegrityError,
    atomic_write_bytes,
    is_private,
)

BUNDLE_PREFIX = "cswapsync1"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class DevicePublicKey:
    device_id: str
    age_recipient: str
    verify_key: Ed25519PublicKey

    def bundle(self) -> str:
        raw = self.verify_key.public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)
        return f"{BUNDLE_PREFIX}:{self.device_id}:{self.age_recipient}:{_b64e(raw)}"


def parse_bundle(text: str) -> DevicePublicKey:
    parts = text.strip().split(":")
    if len(parts) != 4 or parts[0] != BUNDLE_PREFIX:
        raise ConfigurationError("invalid public key bundle format")
    _, device_id, age_pub, sig_pub = parts
    if not device_id.replace("-", "").isalnum():
        raise ConfigurationError("invalid device_id in public key bundle")
    try:
        pyrage.x25519.Recipient.from_str(age_pub)
        verify_key = Ed25519PublicKey.from_public_bytes(_b64d(sig_pub))
    except Exception as exc:  # library raises assorted types for bad input
        raise ConfigurationError(f"invalid key material in bundle for {device_id}") from exc
    return DevicePublicKey(device_id, age_pub, verify_key)


class KeyManager:
    """Owns this device's private keys on disk (0600, inside a 0700 dir)."""

    def __init__(self, credentials_dir: Path, device_id: str):
        self.device_id = device_id
        self.identity_path = credentials_dir / "age-identity.txt"
        self.signing_path = credentials_dir / "signing-key.pem"

    def exists(self) -> bool:
        return self.identity_path.exists() and self.signing_path.exists()

    def generate(self) -> None:
        if self.identity_path.exists() or self.signing_path.exists():
            raise ConfigurationError("device keys already exist; refusing to overwrite")
        identity = pyrage.x25519.Identity.generate()
        signing = Ed25519PrivateKey.generate()
        atomic_write_bytes(self.identity_path, (str(identity) + "\n").encode())
        atomic_write_bytes(self.signing_path, signing.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))

    def _read_private(self, path: Path) -> bytes:
        if not path.exists():
            raise ConfigurationError(f"{path.name} missing; run: cswapsync keys init")
        if not is_private(path):
            raise ConfigurationError(f"{path.name} permissions too open; chmod 600 it")
        return path.read_bytes()

    def identity(self) -> "pyrage.x25519.Identity":
        return pyrage.x25519.Identity.from_str(self._read_private(self.identity_path)
                                               .decode().strip())

    def signing_key(self) -> Ed25519PrivateKey:
        key = serialization.load_pem_private_key(self._read_private(self.signing_path), None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ConfigurationError("signing key is not Ed25519")
        return key

    def public(self) -> DevicePublicKey:
        return DevicePublicKey(self.device_id, str(self.identity().to_public()),
                               self.signing_key().public_key())


class Crypto:
    """Encrypt/decrypt payloads and sign/verify manifests for trusted devices."""

    def __init__(self, keys: KeyManager, trusted_bundles: list[str]):
        self.keys = keys
        self.own = keys.public()
        self.trusted: dict[str, DevicePublicKey] = {self.own.device_id: self.own}
        for bundle in trusted_bundles:
            peer = parse_bundle(bundle)
            if peer.device_id == self.own.device_id and peer.bundle() != self.own.bundle():
                raise ConfigurationError("trusted_devices contains a foreign key "
                                         "using this device's id")
            self.trusted.setdefault(peer.device_id, peer)

    def recipient_ids(self, self_only: bool = False) -> list[str]:
        return [self.own.device_id] if self_only else sorted(self.trusted)

    @property
    def peer_ids(self) -> list[str]:
        return sorted(d for d in self.trusted if d != self.own.device_id)

    def encrypt(self, plaintext: bytes, self_only: bool = False) -> bytes:
        devices = [self.own] if self_only else list(self.trusted.values())
        recipients = [pyrage.x25519.Recipient.from_str(d.age_recipient) for d in devices]
        return pyrage.encrypt(plaintext, recipients)

    def decrypt(self, ciphertext: bytes) -> bytes:
        try:
            return pyrage.decrypt(ciphertext, [self.keys.identity()])
        except Exception as exc:
            # Covers tampering (AEAD tag failure), truncation, wrong recipient.
            raise IntegrityError("decryption failed: payload corrupt, tampered, or "
                                 "not encrypted to this device") from exc

    def sign(self, data: bytes) -> str:
        return _b64e(self.keys.signing_key().sign(data))

    def verify(self, device_id: str, data: bytes, signature: str) -> None:
        peer = self.trusted.get(device_id)
        if peer is None:
            raise IntegrityError(f"backup signed by untrusted device '{device_id}'")
        try:
            peer.verify_key.verify(_b64d(signature), data)
        except (InvalidSignature, ValueError) as exc:
            raise IntegrityError(f"manifest signature invalid for device '{device_id}'") from exc
