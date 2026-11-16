import os

import pytest

from cswapsync.encryption import Crypto, KeyManager, parse_bundle
from cswapsync.settings import ConfigurationError, IntegrityError


def test_roundtrip_and_peer_can_decrypt(pair):
    dell, hp = pair
    blob = dell.mgr.crypto.encrypt(b"payload")
    assert dell.mgr.crypto.decrypt(blob) == b"payload"
    assert hp.mgr.crypto.decrypt(blob) == b"payload"


def test_self_only_not_decryptable_by_peer(pair):
    dell, hp = pair
    blob = dell.mgr.crypto.encrypt(b"rollback", self_only=True)
    with pytest.raises(IntegrityError):
        hp.mgr.crypto.decrypt(blob)


def test_tampered_ciphertext_rejected(pair):
    dell, _ = pair
    blob = bytearray(dell.mgr.crypto.encrypt(b"payload"))
    blob[-5] ^= 0xFF
    with pytest.raises(IntegrityError):
        dell.mgr.crypto.decrypt(bytes(blob))


def test_signature_verification(pair):
    dell, hp = pair
    sig = dell.mgr.crypto.sign(b"manifest")
    hp.mgr.crypto.verify("dell-ubuntu", b"manifest", sig)
    with pytest.raises(IntegrityError):
        hp.mgr.crypto.verify("dell-ubuntu", b"manifesT", sig)
    with pytest.raises(IntegrityError):
        hp.mgr.crypto.verify("evil-device", b"manifest", sig)


def test_keys_private_permissions_and_no_overwrite(tmp_path):
    km = KeyManager(tmp_path, "dev1")
    km.generate()
    assert oct(km.identity_path.stat().st_mode & 0o777) == "0o600"
    assert oct(km.signing_path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ConfigurationError):
        km.generate()
    os.chmod(km.identity_path, 0o644)
    with pytest.raises(ConfigurationError, match="permissions"):
        km.identity()


def test_bundle_roundtrip_and_rejects_garbage(tmp_path):
    km = KeyManager(tmp_path, "dev1")
    km.generate()
    bundle = km.public().bundle()
    assert parse_bundle(bundle).bundle() == bundle
    assert "AGE-SECRET-KEY" not in bundle
    for bad in ("nope", "cswapsync1:dev:age1xyz:abc", "cswapsync1:bad id:x:y"):
        with pytest.raises(ConfigurationError):
            parse_bundle(bad)


def test_foreign_key_with_own_device_id_rejected(tmp_path):
    a = KeyManager(tmp_path / "a", "same")
    b = KeyManager(tmp_path / "b", "same")
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    a.generate(); b.generate()
    with pytest.raises(ConfigurationError):
        Crypto(a, [b.public().bundle()])
