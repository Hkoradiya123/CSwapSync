"""Google Drive storage for encrypted backups (scope: drive.file only).

drive.file means this app can only see files it created itself. Both devices
must use the SAME OAuth client (same Cloud project) and the SAME Google
account, otherwise each device sees an empty Drive.

Cloud layout (all under one app-created folder):
    CSwap-Sync/current/backup.age        stable file id, updated in place
    CSwap-Sync/current/manifest.json     stable file id; the commit point
    CSwap-Sync/history/r<rev>-<id>.age   immutable, one per backup
    CSwap-Sync/history/r<rev>-<id>.manifest.json
    CSwap-Sync/status/lease.json         device session lease
    CSwap-Sync/status/conflict.json      present while a conflict awaits reconciliation
"""
from __future__ import annotations

import io
import random
import socket
import time
from pathlib import Path
from typing import Any, Callable

from logging_config import get_logger
from manifest import sha256_hex
from settings import CloudUnavailableError, ConfigurationError, IntegrityError, \
    atomic_write_bytes

log = get_logger("drive")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,size,sha256Checksum,createdTime,modifiedTime"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
SUBFOLDERS = ("current", "history", "status")


# --------------------------------------------------------------------------- retry
def _is_retryable(exc: BaseException) -> bool:
    from googleapiclient.errors import HttpError
    try:
        from google.auth.exceptions import TransportError
    except ImportError:  # pragma: no cover
        TransportError = OSError  # type: ignore
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", 0)
        if status == 403 and b"RateLimitExceeded" in (exc.content or b""):
            return True
        return int(status) in RETRYABLE_STATUS
    return isinstance(exc, (TransportError, ConnectionError, TimeoutError, socket.timeout,
                            socket.gaierror, OSError))


def with_retry(op: Callable[[], Any], what: str, attempts: int = 5, base_delay: float = 1.0,
               sleep: Callable[[float], None] = time.sleep) -> Any:
    """Exponential backoff with full jitter; non-retryable errors raise immediately."""
    for attempt in range(1, attempts + 1):
        try:
            return op()
        except Exception as exc:  # noqa: BLE001 - classified below
            if not _is_retryable(exc):
                raise
            if attempt == attempts:
                raise CloudUnavailableError(f"Google Drive unreachable during {what} "
                                            f"after {attempts} attempts: "
                                            f"{type(exc).__name__}") from exc
            delay = random.uniform(0, base_delay * (2 ** (attempt - 1)))
            log.warning("drive %s failed (%s), retry %d/%d in %.1fs", what,
                        type(exc).__name__, attempt, attempts - 1, delay)
            sleep(delay)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- auth
def load_credentials(client_secret: Path, token_file: Path, interactive: bool):
    """Return valid user credentials, refreshing expired access tokens.

    Non-interactive callers (timers) never open a browser: a revoked or missing
    token is a hard, explicit failure telling the user to run ``cswapsync auth``.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except ValueError:
            log.warning("stored Google token unreadable; re-authorisation required")
            creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            with_retry(lambda: creds.refresh(Request()), "oauth token refresh")
            _save_token(token_file, creds)
            return creds
        except RefreshError:
            log.warning("Google refresh token rejected (revoked/expired)")
            creds = None
    if not interactive:
        raise ConfigurationError("Google Drive authorisation missing or revoked; "
                                 "run: cswapsync auth")
    if not client_secret.exists():
        raise ConfigurationError(f"OAuth client file missing: {client_secret}")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(token_file, creds)
    return creds


def _save_token(token_file: Path, creds) -> None:
    atomic_write_bytes(token_file, creds.to_json().encode())


def build_service(creds):
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------- store
def _q_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveStore:
    def __init__(self, service, root_name: str, id_cache: dict[str, str],
                 sleep: Callable[[float], None] = time.sleep):
        self.svc = service
        self.root_name = root_name
        self.ids = id_cache            # persisted by caller (state.json) for stable ids
        self._sleep = sleep

    def _call(self, request_factory: Callable[[], Any], what: str) -> Any:
        return with_retry(lambda: request_factory().execute(), what, sleep=self._sleep)

    # ---------------------------------------------------------------- folders
    def ensure_layout(self) -> dict[str, str]:
        root = self._ensure_folder(self.root_name, None, "root")
        for sub in SUBFOLDERS:
            self._ensure_folder(sub, root, sub)
        return dict(self.ids)

    def folder(self, key: str) -> str:
        if key not in self.ids:
            self.ensure_layout()
        return self.ids[key]

    def _ensure_folder(self, name: str, parent: str | None, key: str) -> str:
        cached = self.ids.get(key)
        if cached and self._exists(cached):
            return cached
        found = self.find(name, parent, mime=FOLDER_MIME)
        if found:
            folder_id = found["id"]
        else:
            body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
            if parent:
                body["parents"] = [parent]
            folder_id = self._call(lambda: self.svc.files().create(body=body, fields="id"),
                                   f"create folder {name}")["id"]
            log.info("created Drive folder %s", name)
        self.ids[key] = folder_id
        return folder_id

    def _exists(self, file_id: str) -> bool:
        from googleapiclient.errors import HttpError
        try:
            meta = self._call(lambda: self.svc.files().get(fileId=file_id,
                                                           fields="id,trashed"), "stat")
            return not meta.get("trashed", False)
        except HttpError as exc:
            if getattr(exc.resp, "status", 0) == 404:
                return False
            raise

    # ---------------------------------------------------------------- files
    def find(self, name: str, parent: str | None, mime: str | None = None) -> dict | None:
        q = [f"name = '{_q_escape(name)}'", "trashed = false"]
        if parent:
            q.append(f"'{_q_escape(parent)}' in parents")
        if mime:
            q.append(f"mimeType = '{mime}'")
        res = self._call(lambda: self.svc.files().list(
            q=" and ".join(q), spaces="drive", orderBy="createdTime",
            fields=f"files({FILE_FIELDS})", pageSize=10), f"find {name}")
        files = res.get("files", [])
        if len(files) > 1:
            log.warning("duplicate Drive entries named %s; using the oldest", name)
        return files[0] if files else None

    def list_children(self, parent: str) -> list[dict]:
        out: list[dict] = []
        token = None
        while True:
            res = self._call(lambda: self.svc.files().list(
                q=f"'{_q_escape(parent)}' in parents and trashed = false", spaces="drive",
                orderBy="createdTime", fields=f"nextPageToken,files({FILE_FIELDS})",
                pageSize=200, pageToken=token), "list")
            out.extend(res.get("files", []))
            token = res.get("nextPageToken")
            if not token:
                return out

    def _media(self, data: bytes, mime: str):
        from googleapiclient.http import MediaIoBaseUpload
        return MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)

    def create_unique(self, parent_key: str, name: str, data: bytes, mime: str) -> dict:
        """Create an immutable file, idempotent across retries.

        If a previous attempt reached Drive but the response was lost, the file
        already exists: adopt it when its content matches instead of creating
        a duplicate; refuse when it differs.
        """
        parent = self.folder(parent_key)
        existing = self.find(name, parent)
        if existing:
            if self.download(existing["id"]) != data:
                raise IntegrityError(f"Drive already holds a different {name}")
            return existing
        body = {"name": name, "parents": [parent]}

        def attempt():
            found = self.find(name, parent)
            if found:
                return found
            return self.svc.files().create(body=body, media_body=self._media(data, mime),
                                           fields=FILE_FIELDS).execute()
        return with_retry(attempt, f"upload {name}", sleep=self._sleep)

    def put_stable(self, parent_key: str, name: str, data: bytes, mime: str) -> dict:
        """Create-or-update by name so the file id never changes between uploads."""
        cache_key = f"{parent_key}/{name}"
        file_id = self.ids.get(cache_key)
        if not file_id or not self._exists(file_id):
            found = self.find(name, self.folder(parent_key))
            file_id = found["id"] if found else None
        if file_id is None:
            meta = self.create_unique(parent_key, name, data, mime)
        else:
            fid = file_id
            meta = self._call(lambda: self.svc.files().update(
                fileId=fid, media_body=self._media(data, mime), fields=FILE_FIELDS),
                f"update {name}")
        self.ids[cache_key] = meta["id"]
        return meta

    def get_stable(self, parent_key: str, name: str) -> tuple[str, bytes] | None:
        cache_key = f"{parent_key}/{name}"
        file_id = self.ids.get(cache_key)
        if not file_id or not self._exists(file_id):
            found = self.find(name, self.folder(parent_key))
            if not found:
                return None
            file_id = found["id"]
            self.ids[cache_key] = file_id
        return file_id, self.download(file_id)

    def download(self, file_id: str) -> bytes:
        return self._call(lambda: self.svc.files().get_media(fileId=file_id),
                          f"download {file_id[:8]}")

    def delete(self, file_id: str) -> None:
        self._call(lambda: self.svc.files().delete(fileId=file_id), "delete")

    def verify_remote(self, meta: dict, expected_sha: str) -> None:
        """Re-download and hash: the upload counts only once this passes."""
        remote_sha = meta.get("sha256Checksum")
        if remote_sha and remote_sha != expected_sha:
            raise IntegrityError(f"Drive checksum mismatch for {meta.get('name')}")
        if sha256_hex(self.download(meta["id"])) != expected_sha:
            raise IntegrityError(f"downloaded copy of {meta.get('name')} does not match")
