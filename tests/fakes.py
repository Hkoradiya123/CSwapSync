"""Test doubles. No real tokens anywhere: every credential string is a dummy."""
from __future__ import annotations

import copy
import itertools
import json
import re
import subprocess

import httplib2
from googleapiclient.errors import HttpError

FOLDER = "application/vnd.google-apps.folder"


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


# ----------------------------------------------------------------------------- Drive
class FakeRequest:
    def __init__(self, svc: "FakeDriveService", op: str, fn):
        self.svc, self.op, self.fn = svc, op, fn

    def execute(self):
        self.svc.calls.append(self.op)
        if self.svc.offline:
            raise ConnectionError("network down")
        for i, (op, exc, after) in enumerate(self.svc.failures):
            if op == self.op or op == "*":
                self.svc.failures.pop(i)
                if after:  # perform the op, then lose the response
                    self.fn()
                raise exc
        return self.fn()


class FakeFiles:
    def __init__(self, svc: "FakeDriveService"):
        self.svc = svc

    def list(self, q, **_):
        def run():
            name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
            parent = re.search(r"'([^']+)' in parents", q)
            mime = re.search(r"mimeType = '([^']+)'", q)
            out = []
            for f in self.svc.ordered():
                if f["trashed"]:
                    continue
                if name and f["name"] != name.group(1).replace("\\'", "'"):
                    continue
                if parent and parent.group(1) not in f["parents"]:
                    continue
                if mime and f["mimeType"] != mime.group(1):
                    continue
                out.append(self.svc.meta(f))
            return {"files": out}
        return FakeRequest(self.svc, "list", run)

    def create(self, body, media_body=None, fields=None):
        def run():
            fid = f"id{next(self.svc.ids)}"
            data = _read_media(media_body)
            self.svc.store[fid] = {"id": fid, "name": body["name"],
                                   "mimeType": body.get("mimeType", "application/octet-stream"),
                                   "parents": body.get("parents", []), "data": data,
                                   "trashed": False, "seq": next(self.svc.seq)}
            return self.svc.meta(self.svc.store[fid])
        return FakeRequest(self.svc, "create", run)

    def update(self, fileId, media_body=None, fields=None):
        def run():
            f = self.svc.get(fileId)
            f["data"] = _read_media(media_body)
            return self.svc.meta(f)
        return FakeRequest(self.svc, "update", run)

    def get(self, fileId, fields=None):
        return FakeRequest(self.svc, "get", lambda: self.svc.meta(self.svc.get(fileId)))

    def get_media(self, fileId):
        return FakeRequest(self.svc, "get_media", lambda: self.svc.get(fileId)["data"])

    def delete(self, fileId):
        def run():
            self.svc.get(fileId)
            del self.svc.store[fileId]
            return ""
        return FakeRequest(self.svc, "delete", run)


def _read_media(media) -> bytes:
    if media is None:
        return b""
    return media.getbytes(0, media.size())


class FakeDriveService:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.seq = itertools.count(1)
        self.failures: list[tuple[str, Exception, bool]] = []
        self.offline = False
        self.calls: list[str] = []

    def files(self):  # noqa: D401 - mimics googleapiclient
        return FakeFiles(self)

    def fail(self, op: str, exc: Exception, times: int = 1, after: bool = False) -> None:
        self.failures.extend([(op, exc, after)] * times)

    def get(self, fid: str) -> dict:
        if fid not in self.store:
            raise http_error(404)
        return self.store[fid]

    def ordered(self):
        return sorted(self.store.values(), key=lambda f: f["seq"])

    @staticmethod
    def meta(f: dict) -> dict:
        import hashlib
        return {"id": f["id"], "name": f["name"], "size": str(len(f["data"])),
                "sha256Checksum": hashlib.sha256(f["data"]).hexdigest()
                if f["mimeType"] != FOLDER else None,
                "trashed": f["trashed"], "createdTime": str(f["seq"])}

    def by_name(self, name: str) -> list[dict]:
        return [f for f in self.ordered() if f["name"] == name]

    def names_in(self, folder_name: str) -> list[str]:
        folder = [f for f in self.ordered() if f["name"] == folder_name][0]["id"]
        return [f["name"] for f in self.ordered() if folder in f["parents"]]


# ----------------------------------------------------------------------------- cswap
def make_envelope(tag: str = "v1", count: int = 2, active: int = 1) -> dict:
    accounts = []
    for n in range(1, count + 1):
        accounts.append({
            "number": n, "email": f"user{n}@example.test", "uuid": f"uuid-{n}",
            "organizationUuid": "", "organizationName": "", "added": "2026-01-01T00:00:00Z",
            "credentials": {"claudeAiOauth": {"accessToken": f"dummy-access-{n}-{tag}",
                                              "refreshToken": f"dummy-refresh-{n}-{tag}",
                                              "expiresAt": 0}},
            "config": {"oauthAccount": {"emailAddress": f"user{n}@example.test"}},
        })
    return {"version": 1, "exportedAt": "2026-11-16T00:00:00Z", "exportedFrom": "linux",
            "swapVersion": "0.29.0", "encrypted": False, "activeAccountNumber": active,
            "accounts": accounts}


class FakeCswap:
    """Callable replacing subprocess.run for the cswap binary and systemctl."""

    def __init__(self, envelope: dict | None = None):
        self.envelope = envelope if envelope is not None else make_envelope()
        self.health = {1: "ok", 2: "ok"}
        self.refresh_state = "inactive"
        self.calls: list[list[str]] = []
        self.fail_import = False
        self.export_counter = 0

    def set_tag(self, tag: str) -> None:
        self.envelope = make_envelope(tag)

    def tag(self) -> str:
        token = self.envelope["accounts"][0]["credentials"]["claudeAiOauth"]["accessToken"]
        return token.split("-", 3)[-1]  # dummy-access-<n>-<tag>

    def __call__(self, cmd, input=None, **_):
        args = list(cmd[1:])
        self.calls.append(args)
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, self.refresh_state.encode(), b"")
        out, rc, err = b"", 0, b""
        if args == ["--version"]:
            out = b"cswap 0.29.0\n"
        elif args == ["export", "-"] and self.envelope is None:
            rc, err = 1, b"Error: no accounts to export"
        elif args == ["export", "-"]:
            self.export_counter += 1
            env = copy.deepcopy(self.envelope)
            env["exportedAt"] = f"2026-11-16T00:00:{self.export_counter:02d}Z"
            out = json.dumps(env).encode()
        elif args == ["import", "-", "--force"]:
            if self.fail_import:
                self.fail_import = False
                rc, err = 1, b"disk full"
            else:
                self.envelope = json.loads(input)
        elif args == ["list", "--json"]:
            out = json.dumps({"schemaVersion": 1, "accounts": [
                {"number": n, "email": f"user{n}@example.test", "usageStatus": s}
                for n, s in self.health.items()]}).encode()
        elif args == ["status", "--json"]:
            total = len(self.envelope["accounts"]) if self.envelope else 0
            out = json.dumps({"schemaVersion": 1, "active": {"number": 1 if total else None},
                              "totalManagedAccounts": total}).encode()
        elif args[:1] == ["switch"]:
            pass
        else:
            rc, err = 2, b"unknown command"
        return subprocess.CompletedProcess(cmd, rc, out, err)
