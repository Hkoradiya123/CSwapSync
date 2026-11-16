import json

import pytest
from google.auth.exceptions import RefreshError

import google_drive
from fakes import http_error
from google_drive import DriveStore, load_credentials, with_retry
from settings import CloudUnavailableError, ConfigurationError, IntegrityError

FAKE_TOKEN = {"token": "dummy-access", "refresh_token": "dummy-refresh",
              "client_id": "dummy.apps.googleusercontent.com", "client_secret": "dummy",
              "token_uri": "https://oauth2.googleapis.com/token",
              "scopes": google_drive.SCOPES}


def store(svc):
    return DriveStore(svc, "CSwap-Sync", {}, sleep=lambda s: None)


# ---------------------------------------------------------------- OAuth
def test_oauth_valid_token_used_without_refresh(tmp_path, monkeypatch):
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({**FAKE_TOKEN, "expiry": "2999-01-01T00:00:00Z"}))
    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh",
                        lambda *a: pytest.fail("should not refresh"))
    assert load_credentials(tmp_path / "cs.json", tok, interactive=False).valid


def test_oauth_expired_token_is_refreshed_and_saved(tmp_path, monkeypatch):
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({**FAKE_TOKEN, "expiry": "2000-01-01T00:00:00Z"}))
    calls = []

    def fake_refresh(self, request):
        calls.append(1)
        self.token = "dummy-new-access"
        import datetime
        self.expiry = datetime.datetime(2999, 1, 1)
    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fake_refresh)
    creds = load_credentials(tmp_path / "cs.json", tok, interactive=False)
    assert calls and creds.token == "dummy-new-access"
    assert json.loads(tok.read_text())["token"] == "dummy-new-access"
    assert oct(tok.stat().st_mode & 0o777) == "0o600"


def test_oauth_revoked_token_fails_closed_noninteractive(tmp_path, monkeypatch):
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({**FAKE_TOKEN, "expiry": "2000-01-01T00:00:00Z"}))

    def boom(self, request):
        raise RefreshError("invalid_grant")
    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", boom)
    with pytest.raises(ConfigurationError, match="cswapsync auth"):
        load_credentials(tmp_path / "cs.json", tok, interactive=False)


def test_oauth_missing_token_noninteractive_never_opens_browser(tmp_path):
    with pytest.raises(ConfigurationError):
        load_credentials(tmp_path / "cs.json", tmp_path / "none.json", interactive=False)


# ---------------------------------------------------------------- upload / download
def test_layout_created_once(drive_service):
    s = store(drive_service)
    s.ensure_layout()
    s2 = store(drive_service)  # fresh cache: must find, not recreate
    s2.ensure_layout()
    assert len(drive_service.store) == 4
    assert sorted(drive_service.names_in("CSwap-Sync")) == ["current", "history", "status"]


def test_put_stable_keeps_file_id_and_no_duplicates(drive_service):
    s = store(drive_service)
    a = s.put_stable("current", "backup.age", b"one", "application/octet-stream")
    b = s.put_stable("current", "backup.age", b"two", "application/octet-stream")
    c = store(drive_service).put_stable("current", "backup.age", b"three",
                                        "application/octet-stream")
    assert a["id"] == b["id"] == c["id"]
    assert len(drive_service.by_name("backup.age")) == 1
    assert s.download(a["id"]) == b"three"


def test_upload_download_roundtrip_and_verify(drive_service):
    import hashlib
    s = store(drive_service)
    meta = s.create_unique("history", "r000001-x.age", b"cipher", "application/octet-stream")
    s.verify_remote(meta, hashlib.sha256(b"cipher").hexdigest())
    with pytest.raises(IntegrityError):
        s.verify_remote(meta, hashlib.sha256(b"other").hexdigest())


# ---------------------------------------------------------------- retry / interruption
def test_transient_errors_retried(drive_service):
    s = store(drive_service)
    s.ensure_layout()
    drive_service.fail("create", http_error(503), times=2)
    drive_service.fail("get_media", ConnectionError("reset"), times=1)
    meta = s.create_unique("history", "f.age", b"data", "application/octet-stream")
    assert s.download(meta["id"]) == b"data"


def test_lost_create_response_does_not_duplicate(drive_service):
    s = store(drive_service)
    s.ensure_layout()
    drive_service.fail("create", http_error(502), after=True)  # server created, reply lost
    s.create_unique("history", "f.age", b"data", "application/octet-stream")
    assert len(drive_service.by_name("f.age")) == 1


def test_create_unique_refuses_different_content(drive_service):
    s = store(drive_service)
    s.create_unique("history", "f.age", b"data", "application/octet-stream")
    with pytest.raises(IntegrityError):
        s.create_unique("history", "f.age", b"tampered", "application/octet-stream")


def test_non_retryable_error_raises_immediately():
    calls = []

    def op():
        calls.append(1)
        raise http_error(403)
    from googleapiclient.errors import HttpError
    with pytest.raises(HttpError):
        with_retry(op, "x", sleep=lambda s: None)
    assert len(calls) == 1


def test_network_unavailable_becomes_cloud_unavailable(drive_service):
    drive_service.offline = True
    with pytest.raises(CloudUnavailableError):
        store(drive_service).ensure_layout()
