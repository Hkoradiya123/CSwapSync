"""post-refresh: backup only after the existing cswap refresh succeeded and accounts are healthy."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from settings import LeaseError, RefreshNotHealthyError


def write_log(dev, body: str, minutes_ago: int = 1, exit_code: int = 0):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).astimezone() \
        .replace(microsecond=0).isoformat()
    old = "=== 2020-01-01T00:00:00+05:30 ===\nold run invalid_grant\nexit=1\n"
    Path(dev.config.refresh_log).write_text(
        f"{old}=== {ts} ===\n{body}\nexit={exit_code}\n")


GOOD = "  1: user1@example.test\n    • refresh token: ok\n  2: user2@example.test"


def test_healthy_refresh_pushes_and_releases_lease(pair, drive_service):
    dell, _ = pair
    write_log(dell, GOOD)
    m = dell.mgr.post_refresh()
    assert m.revision == 1
    assert dell.mgr.lease.holder() is None          # took it only for this run


@pytest.mark.parametrize("body,exit_code,age,match", [
    (GOOD + "\nre-login needed", 0, 1, "attention"),
    (GOOD + "\ninvalid_grant", 0, 1, "attention"),
    (GOOD + "\nrefresh deferred", 0, 1, "attention"),
    (GOOD, 1, 1, "exit 0"),
    (GOOD, 0, 600, "too old"),
])
def test_refresh_failure_blocks_backup(pair, drive_service, body, exit_code, age, match):
    dell, _ = pair
    write_log(dell, body, minutes_ago=age, exit_code=exit_code)
    with pytest.raises(RefreshNotHealthyError, match=match):
        dell.mgr.post_refresh()
    assert not any(c[:1] == ["export"] for c in dell.cswap.calls)
    assert not drive_service.by_name("manifest.json")


def test_unhealthy_account_blocks_backup(pair, drive_service):
    dell, _ = pair
    write_log(dell, GOOD)
    dell.cswap.health = {1: "ok", 2: "relogin_required"}
    with pytest.raises(RefreshNotHealthyError, match="health"):
        dell.mgr.post_refresh()
    assert not drive_service.by_name("manifest.json")


def test_missing_account_blocks_backup(pair):
    dell, _ = pair
    write_log(dell, GOOD)
    dell.cswap.health = {1: "ok"}
    with pytest.raises(RefreshNotHealthyError):
        dell.mgr.post_refresh()


def test_waits_while_refresh_running(pair):
    dell, _ = pair
    write_log(dell, GOOD)
    states = iter(["active", "active", "inactive", "inactive", "inactive", "inactive"])

    def runner(cmd, **kw):
        if cmd[0] == "systemctl":
            dell.cswap.refresh_state = next(states, "inactive")
        return type(dell.cswap).__call__(dell.cswap, cmd, **kw)
    dell.adapter._run_proc = runner
    assert dell.mgr.post_refresh().revision == 1


def test_skips_push_when_other_device_holds_lease(pair, drive_service):
    dell, hp = pair
    hp.mgr.lease.acquire()
    write_log(dell, GOOD)
    with pytest.raises(LeaseError, match="hp-win10"):
        dell.mgr.post_refresh()
    assert not drive_service.by_name("manifest.json")


def test_keeps_own_session_lease(pair):
    dell, _ = pair
    dell.mgr.lease.acquire()
    write_log(dell, GOOD)
    dell.mgr.post_refresh()
    assert dell.mgr.lease.holder().device_id == "dell-ubuntu"


def test_conflict_during_post_refresh_releases_lease(pair):
    dell, hp = pair
    hp.mgr.lease.acquire()
    hp.mgr.push()                     # cloud r1 from HP, Dell never pulled
    dell.cswap.set_tag("dell-local")
    write_log(dell, GOOD)
    from settings import ConflictError
    with pytest.raises(ConflictError):
        dell.mgr.post_refresh()
    assert dell.mgr.lease.holder() is None


def test_inline_mode_runs_while_own_unit_is_activating(pair):
    dell, _ = pair
    write_log(dell, GOOD)
    dell.cswap.refresh_state = "activating"      # ExecStartPost: the unit is us
    assert dell.mgr.post_refresh(inline=True).revision == 1


def test_non_inline_refuses_while_refresh_activating(pair):
    dell, _ = pair
    write_log(dell, GOOD)
    dell.cswap.refresh_state = "activating"
    dell.config.refresh_wait_timeout_seconds = -1   # deadline already passed
    with pytest.raises(RefreshNotHealthyError, match="still running"):
        dell.mgr.check_refresh()
