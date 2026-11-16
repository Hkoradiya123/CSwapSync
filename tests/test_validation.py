import json

import pytest

from cswapsync.cswap_adapter import validate_export_payload
from fakes import make_envelope
from cswapsync.manifest import Manifest, content_digest
from cswapsync.settings import IntegrityError


def raw(env):
    return json.dumps(env).encode()


def test_valid_payload():
    s = validate_export_payload(raw(make_envelope()), 2)
    assert s.account_count == 2 and s.slots == (1, 2) and s.active_slot == 1


@pytest.mark.parametrize("mutate,msg", [
    (lambda e: e["accounts"].pop(), "expected 2"),
    (lambda e: e.update(version=2), "version"),
    (lambda e: e.update(encrypted=True), "encrypted"),
    (lambda e: e["accounts"][0]["credentials"]["claudeAiOauth"].pop("refreshToken"),
     "refresh token"),
    (lambda e: e["accounts"][1]["config"].pop("oauthAccount"), "oauthAccount"),
    (lambda e: e["accounts"][1].update(number=1), "duplicate"),
    (lambda e: e["accounts"][0].update(email="nope"), "email"),
])
def test_invalid_payload_rejected(mutate, msg):
    env = make_envelope()
    mutate(env)
    with pytest.raises(IntegrityError, match=msg):
        validate_export_payload(raw(env), 2)


def test_not_json_rejected():
    with pytest.raises(IntegrityError):
        validate_export_payload(b"\x00garbage", 2)


def test_error_messages_never_contain_tokens():
    env = make_envelope()
    env["accounts"][0]["email"] = "bad"
    with pytest.raises(IntegrityError) as exc:
        validate_export_payload(raw(env), 2)
    assert "dummy-" not in str(exc.value)


def test_content_digest_ignores_export_timestamp():
    a, b = make_envelope(), make_envelope()
    b["exportedAt"] = "2030-01-01T00:00:00Z"
    assert content_digest(a) == content_digest(b)
    assert content_digest(a) != content_digest(make_envelope("v2"))


def test_manifest_schema_rejections():
    with pytest.raises(IntegrityError):
        Manifest.from_bytes(b"not json")
    with pytest.raises(IntegrityError, match="schema_version"):
        Manifest.from_bytes(json.dumps({"schema_version": 99}).encode())
    with pytest.raises(IntegrityError, match="missing"):
        Manifest.from_bytes(json.dumps({"schema_version": 1}).encode())


def test_manifest_signed_before_recipients_field_still_verifies(pair):
    """Cloud r1 on the real Drive was signed before 'recipients' existed."""
    dell, _ = pair
    m = Manifest(schema_version=1, backup_id="a" * 32, revision=1, parent_revision=None,
                 device_id="dell-ubuntu", created_at_utc="2026-11-16T00:00:00Z",
                 encrypted_file_sha256="0" * 64, payload_sha256="1" * 64,
                 content_sha256="2" * 64, account_count=2, application_version="1.0.0",
                 cswap_version="0.29.0", history_file_name="r000001-x.age")
    legacy = json.loads(m.to_bytes())
    legacy.pop("recipients")
    body = {k: v for k, v in legacy.items() if k != "signature"}
    from cswapsync.manifest import canonical_json
    legacy["signature"] = dell.mgr.crypto.sign(canonical_json(body))
    parsed = Manifest.from_bytes(json.dumps(legacy).encode())
    dell.mgr.crypto.verify("dell-ubuntu", parsed.signing_bytes(), parsed.signature)


def test_config_example_matches_code_defaults():
    """config/config.example.json is documentation; it must not drift from Config()."""
    from pathlib import Path
    from cswapsync.settings import Config
    example = json.loads((Path(__file__).resolve().parent.parent / "config" /
                          "config.example.json").read_text())
    example.pop("_comment")
    defaults = Config().defaults_json()
    defaults.pop("_comment")
    assert example == defaults
