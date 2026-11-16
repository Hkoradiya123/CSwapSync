import json

import pytest

from cswapsync.manifest import Manifest
from cswapsync.settings import (CloudUnavailableError, ConflictError, CswapBusyError, IntegrityError,
                      LeaseError, SyncError)


def first_push(dev):
    dev.mgr.pull(apply=False)  # takes the lease; cloud empty
    return dev.mgr.push()


def cloud_manifest(svc):
    return Manifest.from_bytes(svc.by_name("manifest.json")[0]["data"])


# ---------------------------------------------------------------- happy path handoff
def test_dell_push_then_hp_pull_restores_same_state(pair, drive_service):
    dell, hp = pair
    m = first_push(dell)
    assert m.revision == 1 and m.account_count == 2
    # raw tokens never reach Drive
    for f in drive_service.store.values():
        assert b"dummy-access" not in f["data"] and b"dummy-refresh" not in f["data"]

    hp.cswap.set_tag("hp-old")
    msg = hp.mgr.pull(apply=True, accept_cloud=True)
    assert "restored cloud r1" in msg
    assert hp.cswap.tag() == dell.cswap.tag()
    assert ["switch", "1", "--force"] in hp.cswap.calls  # live login activated
    assert list(hp.paths.backups_dir.glob("rollback-*.age"))  # rollback taken first


def test_pull_without_yes_never_imports(pair):
    dell, hp = pair
    first_push(dell)
    hp.cswap.set_tag("hp")
    msg = hp.mgr.pull(apply=False, accept_cloud=True)
    assert "Re-run with --yes" in msg
    assert not any(c[:1] == ["import"] for c in hp.cswap.calls)


# ---------------------------------------------------------------- lease
def test_lease_blocks_other_device(pair):
    dell, hp = pair
    dell.mgr.pull(apply=False)
    with pytest.raises(LeaseError, match="held by 'dell-ubuntu'"):
        hp.mgr.pull(apply=False)
    with pytest.raises(LeaseError):
        hp.mgr.push()


def test_lease_released_only_after_successful_push(pair, drive_service):
    dell, hp = pair
    dell.mgr.pull(apply=False)
    drive_service.fail("update", ConnectionError("down"), times=10)
    with pytest.raises(CloudUnavailableError):
        dell.mgr.push()
    drive_service.failures.clear()
    assert dell.mgr.lease.holder().device_id == "dell-ubuntu"


def test_expired_lease_recovered(pair):
    dell, hp = pair
    dell.mgr.pull(apply=False)
    lease = dell.mgr.lease.read()
    lease.expires_at = "2000-01-01T00:00:00Z"
    dell.mgr.lease._write(lease)
    _, recovered = hp.mgr.lease.acquire()
    assert recovered == "dell-ubuntu"


def test_break_lease_requires_explicit_flag(pair):
    dell, hp = pair
    dell.mgr.pull(apply=False)
    _, recovered = hp.mgr.lease.acquire(break_foreign=True)
    assert recovered == "dell-ubuntu"


def test_drive_unreachable_fails_closed_before_session(pair, drive_service):
    dell, _ = pair
    drive_service.offline = True
    with pytest.raises(CloudUnavailableError):
        dell.mgr.pull(apply=True)
    assert not any(c[:1] == ["import"] for c in dell.cswap.calls)


# ---------------------------------------------------------------- duplicates & history
def test_duplicate_upload_prevented(pair, drive_service):
    dell, _ = pair
    first_push(dell)
    before = len(drive_service.store)
    dell.mgr.pull(apply=False)
    assert dell.mgr.push() is None
    assert len(drive_service.store) == before


def test_history_keeps_at_least_ten(pair, drive_service):
    dell, _ = pair
    for i in range(13):
        dell.cswap.set_tag(f"t{i}")
        dell.mgr.pull(apply=False)
        dell.mgr.push()
    hist = [n for n in drive_service.names_in("history") if n.endswith(".age")]
    assert len(hist) == 10
    assert cloud_manifest(drive_service).revision == 13
    assert len(list(dell.paths.backups_dir.glob("r*.age"))) == 10


# ---------------------------------------------------------------- integrity
def test_interrupted_push_keeps_previous_backup_valid(pair, drive_service, monkeypatch):
    dell, hp = pair
    first_push(dell)
    dell.cswap.set_tag("v2")
    dell.mgr.pull(apply=False)
    real_files = type(drive_service).files

    def flaky(self):
        files = real_files(self)
        orig_update = files.update

        def update(fileId, **kw):
            if self.store[fileId]["name"] == "manifest.json":
                raise ConnectionError("killed before commit point")
            return orig_update(fileId, **kw)
        files.update = update
        return files
    monkeypatch.setattr(type(drive_service), "files", flaky)
    with pytest.raises(CloudUnavailableError):
        dell.mgr.push()
    monkeypatch.setattr(type(drive_service), "files", real_files)

    # current/backup.age already holds v2 but the manifest still says r1 (torn state)
    assert cloud_manifest(drive_service).revision == 1
    # Dell's failed push kept its lease (correct): HP must explicitly break it.
    with pytest.raises(LeaseError):
        hp.mgr.pull(apply=True, accept_cloud=True)
    hp.cswap.set_tag("hp-old")
    msg = hp.mgr.pull(apply=True, accept_cloud=True, break_lease=True)
    assert "r1" in msg and hp.cswap.tag() == "v1"   # history fallback, verified


def test_corrupted_cloud_payload_rejected(pair, drive_service):
    dell, hp = pair
    first_push(dell)
    for f in drive_service.store.values():
        if f["name"].endswith(".age"):
            f["data"] = f["data"][:-1] + b"X"
    with pytest.raises(IntegrityError):
        hp.mgr.pull(apply=True, accept_cloud=True)
    assert not any(c[:1] == ["import"] for c in hp.cswap.calls)


def test_tampered_manifest_signature_rejected(pair, drive_service):
    dell, hp = pair
    first_push(dell)
    f = drive_service.by_name("manifest.json")[0]
    obj = json.loads(f["data"])
    obj["account_count"] = 2
    obj["device_id"] = "hp-win10"  # claims to be from HP, signed by Dell
    f["data"] = json.dumps(obj).encode()
    with pytest.raises(IntegrityError, match="signature"):
        hp.mgr.pull(apply=True, accept_cloud=True)


# ---------------------------------------------------------------- conflicts
def test_revision_conflict_preserves_both_and_requires_reconcile(pair, drive_service):
    dell, hp = pair
    first_push(dell)
    hp.mgr.pull(apply=True, accept_cloud=True)
    hp.cswap.set_tag("hp-newer")
    hp.mgr.push()                                   # cloud r2 from HP
    dell.cswap.set_tag("dell-diverged")             # Dell changed without pulling
    dell.mgr.lease.acquire()
    with pytest.raises(ConflictError, match="r1, cloud is r2"):
        dell.mgr.push()
    m = cloud_manifest(drive_service)
    assert m.revision == 2 and m.device_id == "hp-win10"   # cloud NOT overwritten
    assert any(n.startswith("conflict-") for n in drive_service.names_in("history"))
    assert list(dell.paths.backups_dir.glob("conflict-*.age"))
    assert json.loads(drive_service.by_name("conflict.json")[0]["data"])["resolved"] is False

    m3 = dell.mgr.push(accept_conflict=True)        # explicit reconciliation
    assert m3.revision == 3 and m3.parent_revision == 2
    assert json.loads(drive_service.by_name("conflict.json")[0]["data"])["resolved"] is True


def test_pull_refuses_when_local_also_changed(pair):
    dell, hp = pair
    first_push(dell)
    hp.mgr.pull(apply=True, accept_cloud=True)
    hp.cswap.set_tag("hp2")
    hp.mgr.push()
    dell.cswap.set_tag("dell-unpushed")
    with pytest.raises(ConflictError, match="local accounts changed"):
        dell.mgr.pull(apply=True)
    assert dell.cswap.tag() == "dell-unpushed"


# ---------------------------------------------------------------- restore / rollback
def test_rollback_restores_pre_restore_state(pair):
    dell, hp = pair
    first_push(dell)
    hp.cswap.set_tag("hp-original")
    hp.mgr.pull(apply=True, accept_cloud=True)
    assert hp.cswap.tag() == "v1"
    verified = hp.mgr.load_local(hp.mgr.latest_rollback())
    hp.mgr.restore(verified)
    assert hp.cswap.tag() == "hp-original"


def test_failed_import_auto_rolls_back(pair):
    dell, hp = pair
    first_push(dell)
    hp.cswap.set_tag("hp-original")
    hp.cswap.fail_import = True
    with pytest.raises(SyncError, match="import failed"):
        hp.mgr.pull(apply=True, accept_cloud=True)
    assert hp.cswap.tag() == "hp-original"


def test_restore_refused_while_cswap_busy(pair, monkeypatch):
    from cswapsync import cswap_adapter
    dell, hp = pair
    first_push(dell)
    monkeypatch.setattr(cswap_adapter, "_process_names",
                        lambda: [("python3", ["/usr/bin/python3", "/home/x/.local/bin/cswap", "tui"])])
    with pytest.raises(CswapBusyError):
        hp.mgr.pull(apply=True, accept_cloud=True)


def test_restore_refused_while_claude_running(pair, monkeypatch):
    from cswapsync import cswap_adapter
    dell, hp = pair
    first_push(dell)
    hp.cswap.set_tag("hp-local")
    monkeypatch.setattr(cswap_adapter, "_process_names", lambda: [("claude", ["claude"])])
    with pytest.raises(CswapBusyError, match="Claude Code"):
        hp.mgr.pull(apply=True, accept_cloud=True)
    assert not any(c[:1] == ["import"] for c in hp.cswap.calls)


def test_own_cswapsync_wrapper_not_detected_as_busy(pair, monkeypatch):
    from cswapsync import cswap_adapter
    dell, _ = pair
    monkeypatch.setattr(cswap_adapter, "_process_names",
                        lambda: [("bash", ["bash", "/home/x/.local/bin/cswapsync", "push"])])
    assert dell.adapter.busy_reasons() == []


def test_restore_specific_cloud_revision(pair):
    dell, hp = pair
    first_push(dell)
    dell.cswap.set_tag("v2")
    dell.mgr.pull(apply=False)
    dell.mgr.push()
    hp.mgr.lease.acquire()
    verified = hp.mgr.fetch_history(1)
    hp.mgr.restore(verified)
    assert hp.cswap.tag() == "v1"


def test_newly_trusted_device_forces_reencrypted_push(make_device, drive_service):
    dell, hp = make_device("dell-ubuntu"), make_device("hp-win10")
    hp.trust(dell)
    m1 = first_push(dell)                       # encrypted to Dell only
    assert m1.recipients == ["dell-ubuntu"]
    with pytest.raises(IntegrityError, match="not encrypted to this device"):
        hp.mgr.fetch_cloud(hp.mgr.cloud_manifest())
    dell.trust(hp)                              # same tokens, new recipient
    dell.mgr.lease.acquire()
    m2 = dell.mgr.push()
    assert m2 is not None and m2.recipients == ["dell-ubuntu", "hp-win10"]
    assert hp.mgr.fetch_cloud(hp.mgr.cloud_manifest()).summary.account_count == 2
