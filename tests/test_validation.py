import json

import pytest

from cswap_adapter import validate_export_payload
from fakes import make_envelope
from manifest import Manifest, content_digest
from settings import IntegrityError


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
