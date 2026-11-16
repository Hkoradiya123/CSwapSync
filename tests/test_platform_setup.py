"""systemd unit templating/installation, against a temp unit dir (never the real one)."""
import pytest

from cswapsync import platform_setup as plat


@pytest.fixture
def units(tmp_path, monkeypatch):
    unit_dir = tmp_path / "systemd-user"
    unit_dir.mkdir()
    (unit_dir / "cswap-refresh.service").write_text("[Service]\nExecStart=/bin/true\n")
    monkeypatch.setattr(plat, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "_systemctl", lambda *a: None)
    monkeypatch.setattr(plat, "cswapsync_bin", lambda: "/opt/tools/bin/cswapsync")
    return unit_dir


def test_templates_ship_with_package_and_render_binary():
    for name in ("20-cswapsync-sync.conf", "cswapsync-heartbeat.service",
                 "cswapsync-heartbeat.timer", "10-cswapsync-lease-gate.conf"):
        text = plat.render_unit(name, "/x/cswapsync")
        assert "@CSWAPSYNC_BIN@" not in text
    assert "ExecStartPost=/x/cswapsync post-refresh --inline --notify" in \
        plat.render_unit("20-cswapsync-sync.conf", "/x/cswapsync")
    assert 'ExecStart="/a b/cswapsync" heartbeat' in \
        plat.render_unit("cswapsync-heartbeat.service", "/a b/cswapsync")


def test_install_is_idempotent_and_rerenders_when_binary_moves(units, monkeypatch):
    original = (units / "cswap-refresh.service").read_text()
    assert not plat.refresh_hook_installed()
    plat.install_refresh_hook()
    assert plat.refresh_hook_installed()
    assert (units / "cswap-refresh.service").read_text() == original   # never edited
    dropin = units / "cswap-refresh.service.d" / "20-cswapsync-sync.conf"
    assert "/opt/tools/bin/cswapsync post-refresh" in dropin.read_text()

    monkeypatch.setattr(plat, "cswapsync_bin", lambda: "/new/place/cswapsync")
    assert not plat.refresh_hook_installed()        # stale path detected
    plat.install_refresh_hook()
    assert "/new/place/cswapsync" in dropin.read_text()


def test_foreign_unit_with_same_name_not_overwritten(units):
    foreign = units / "cswapsync-heartbeat.timer"
    foreign.write_text("[Timer]\nOnCalendar=daily\n")
    notes = plat.install_refresh_hook()
    assert foreign.read_text() == "[Timer]\nOnCalendar=daily\n"
    assert any("not ours" in n for n in notes)


def test_old_separate_postrefresh_unit_removed(units):
    (units / "cswapsync-postrefresh.service").write_text("old")
    plat.install_refresh_hook()
    assert not (units / "cswapsync-postrefresh.service").exists()


def test_lease_gate_optional(units):
    plat.install_refresh_hook(gate=False)
    assert not plat.lease_gate_installed()
    plat.install_refresh_hook(gate=True)
    assert plat.lease_gate_installed()
    assert plat.refresh_hook_installed(gate=True)
