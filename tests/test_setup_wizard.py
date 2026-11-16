"""Setup wizard end-to-end with scripted answers, fake Drive and fake cswap."""
import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console

import app_context
import platform_setup
import setup_wizard
from fakes import FakeCswap
from google_drive import DriveStore
from settings import CloudUnavailableError, Paths
from setup_wizard import UI, SetupWizard

ROOT = Path(__file__).resolve().parent.parent


class ScriptedUI(UI):
    """Answers pulled in order; a callable answer is invoked first (simulates the user
    doing something on the other device before replying)."""

    def __init__(self, answers):
        self.out = io.StringIO()
        super().__init__(Console(file=self.out, width=140))
        self.answers = list(answers)
        self.asked = []

    def _next(self, prompt):
        self.asked.append(prompt)
        assert self.answers, f"unexpected prompt: {prompt}"
        ans = self.answers.pop(0)
        return ans() if callable(ans) else ans

    def ask(self, prompt, default=None):
        ans = self._next(prompt)
        if ans is None or ans == "":       # Enter -> default, like rich Prompt
            return default or ""
        return ans

    def confirm(self, prompt, default=False):
        ans = self._next(prompt)
        return default if ans is None else ans

    def choose(self, prompt, choices, default):
        return self._next(prompt)


@pytest.fixture
def env(tmp_path, drive_service, monkeypatch):
    client = tmp_path / "client_secret.json"
    client.write_text(json.dumps({"installed": {"client_id": "dummy.apps", "project_id": "p1"}}))
    stray = tmp_path / "cswap-accounts-export.json"
    stray.write_text("{}")
    monkeypatch.setattr(platform_setup, "client_secret_candidates", lambda: [client])
    monkeypatch.setattr(platform_setup, "STRAY_EXPORTS", [stray])
    monkeypatch.setattr(platform_setup, "has_refresh_unit", lambda: False)
    monkeypatch.setattr(platform_setup, "install_launcher", lambda root: "launcher ok")
    monkeypatch.setattr(setup_wizard, "_exists", lambda p: True)
    monkeypatch.setattr(app_context.App, "drive", lambda self, interactive=False: DriveStore(
        drive_service, "CSwap-Sync", self.state.data["drive_ids"], sleep=lambda s: None))
    import lease
    monkeypatch.setattr(lease.LeaseManager, "__init__", _fast_lease(lease.LeaseManager.__init__))

    import doctor
    monkeypatch.setattr(doctor, "_systemd", lambda *a: "enabled")   # never read real units
    monkeypatch.setattr(doctor, "STRAY_EXPORTS", [stray])

    def make(name):
        root = tmp_path / name
        (root / "config").mkdir(parents=True)
        shutil.copy(ROOT / "config" / "config.example.json", root / "config")
        (root / "credentials").mkdir(mode=0o700)
        token = root / "credentials" / "google-token.json"   # Drive itself is faked
        token.write_text("{}")
        token.chmod(0o600)
        return Paths(root)
    return make, stray


def _fast_lease(orig):
    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.settle, self.sleep = 0, (lambda s: None)
    return init


def sender_answers(trust="", delete=True):
    return ["1",            # role: sender
            "dell-test",    # device id (no device.json yet -> asked directly)
            "",             # account count -> default from cswap (2)
            True,           # use found client json
            trust,          # paste receiver bundle / skip
            delete]         # delete plaintext export


def test_sender_wizard_end_to_end(env):
    make, stray = env
    paths = make("dell")
    cswap = FakeCswap()
    ui = ScriptedUI(sender_answers())
    assert SetupWizard(paths, ui, runner=cswap).run() == 0, ui.out.getvalue()
    out = ui.out.getvalue()
    assert "uploaded r1 (2 accounts)" in out
    assert "download+verify+decrypt ok" in out
    assert not stray.exists()
    assert json.loads(paths.device_file.read_text())["role"] == "sender"
    assert "dummy-" not in out                              # no token ever printed


def test_sender_rerun_is_idempotent(env):
    make, stray = env
    paths = make("dell")
    cswap = FakeCswap()
    SetupWizard(paths, ScriptedUI(sender_answers()), runner=cswap).run()
    key_before = (paths.credentials_dir / "age-identity.txt").read_bytes()
    # role, keep id, count; client json present + no stray -> not asked; trust: skip
    ui = ScriptedUI(["1", True, "", ""])
    assert SetupWizard(paths, ui, runner=cswap).run() == 0, ui.out.getvalue()
    assert (paths.credentials_dir / "age-identity.txt").read_bytes() == key_before
    assert "existing keypair kept" in ui.out.getvalue()
    assert "already current" in ui.out.getvalue()


def test_receiver_wizard_bootstraps_from_sender(env):
    make, _ = env
    s_paths, r_paths = make("dell"), make("hp")
    sender_cswap, receiver_cswap = FakeCswap(), FakeCswap(envelope=None)  # HP: no accounts
    sender_cswap.set_tag("from-dell")
    SetupWizard(s_paths, ScriptedUI(sender_answers(delete=False)), runner=sender_cswap).run()
    sender_bundle = app_context.App(paths=s_paths, runner=sender_cswap).keys.public().bundle()

    def sender_trusts_receiver_and_pushes():
        bundle = app_context.App(paths=r_paths, runner=receiver_cswap).keys.public().bundle()
        app = app_context.App(paths=s_paths, runner=sender_cswap)
        app_context.trust_bundle(s_paths, app.device.device_id, bundle)
        app = app_context.App(paths=s_paths, runner=sender_cswap)
        mgr = app.manager()
        mgr.lease.acquire()
        mgr.push()
        return True                                   # "Retry this step?" -> yes

    ui = ScriptedUI(["2",              # role: receiver
                     "hp-test",        # device id
                     "",               # account count (default 2)
                     True,             # use client json
                     sender_bundle,    # trust sender
                     sender_trusts_receiver_and_pushes,   # cloud read fails -> fix -> retry
                     True,             # import now
                     False])           # not using Claude here now -> push + release lease
    assert SetupWizard(r_paths, ui, runner=receiver_cswap).run() == 0, ui.out.getvalue()
    out = ui.out.getvalue()
    assert "not encrypted to this device" in out and "cswapsync trust" in out
    assert receiver_cswap.tag() == "from-dell"
    assert ["switch", "1", "--force"] in receiver_cswap.calls   # live login activated
    assert "lease released" in out


def test_receiver_must_trust_a_sender(env):
    make, _ = env
    paths = make("hp")
    def user_quits():
        raise KeyboardInterrupt
    # receiver: Enter (empty) is NOT accepted, garbage is rejected, then user quits
    ui = ScriptedUI(["2", "hp-test", "", True, "", "garbage", user_quits])
    with pytest.raises(KeyboardInterrupt):
        SetupWizard(paths, ui, runner=FakeCswap(envelope=None)).run()
    assert "invalid public key bundle" in ui.out.getvalue()


def test_drive_down_stops_wizard_after_declined_retry(env, monkeypatch):
    make, _ = env
    paths = make("dell")

    def offline(self, interactive=False):
        raise CloudUnavailableError("Google Drive unreachable")
    monkeypatch.setattr(app_context.App, "drive", offline)
    ui = ScriptedUI(["1", "dell-test", "", True, False])   # ... retry? -> no
    assert SetupWizard(paths, ui, runner=FakeCswap()).run() == 1
    out = ui.out.getvalue()
    assert "Drive unreachable" in out and "Upload to Drive" not in out


def test_windows_skips_systemd_hook(env, monkeypatch):
    monkeypatch.setattr(setup_wizard, "IS_WINDOWS", True)
    make, _ = env
    ui = ScriptedUI(sender_answers())
    assert SetupWizard(make("dell"), ui, runner=FakeCswap()).run() == 0
    assert "Windows: no cswap-refresh.service" in ui.out.getvalue()


def test_config_step_expands_tilde_in_cswap_path(env):
    """Real-run regression: '~/.local/bin/cswap' must be expanded before exec."""
    make, _ = env
    seen = []
    cswap = FakeCswap()

    def runner(cmd, **kw):
        seen.append(cmd[0])
        return cswap(cmd, **kw)
    SetupWizard(make("dell"), ScriptedUI(sender_answers()), runner=runner).run()
    assert seen and not any(c.startswith("~") for c in seen)
