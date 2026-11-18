"""`cswapsync setup`: interactive, idempotent, cross-platform (Linux + Windows).

First question: is this device the SENDER (accounts already live here) or the
RECEIVER (accounts come from Drive)? Each step checks what already exists,
asks only for what is missing, and the last step tests the result.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import platform_setup as plat
from .app_context import App, trust_bundle
from .doctor import FAIL, OK, WARN, cswap_version_check, run_doctor
from .cswap_adapter import CswapAdapter, validate_export_payload
from .encryption import parse_bundle
from .settings import (IS_WINDOWS, Config, ConfigurationError, IntegrityError, Paths, SyncError,
                      atomic_write_json, read_json)

SKIP = "SKIP"
# ASCII on purpose: legacy Windows consoles (cp1252) cannot print check marks.
ICON = {OK: "[green]OK  [/]", WARN: "[yellow]WARN[/]", FAIL: "[red]FAIL[/]", SKIP: "[dim]SKIP[/]"}
StepFn = Callable[[], tuple[str, str]]


class UI:
    """Thin rich wrapper so tests can script answers."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def print(self, text: str = "") -> None:
        self.console.print(text)

    def raw(self, text: str) -> None:
        """Print without markup or wrapping (keys must copy as one line)."""
        self.console.print(text, markup=False, soft_wrap=True, highlight=False)

    def panel(self, text: str, title: str) -> None:
        self.console.print(Panel(text, title=title, expand=False))

    def ask(self, prompt: str, default: str | None = None) -> str:
        return Prompt.ask(prompt, default=default, console=self.console) or ""

    def confirm(self, prompt: str, default: bool = False) -> bool:
        return Confirm.ask(prompt, default=default, console=self.console)

    def choose(self, prompt: str, choices: list[str], default: str) -> str:
        return Prompt.ask(prompt, choices=choices, default=default, console=self.console)


class SetupWizard:
    def __init__(self, paths: Paths, ui: UI, runner=None):
        self.paths = paths
        self.ui = ui
        self.runner = runner            # tests inject a fake cswap
        self.role = ""
        self.app: App | None = None
        self.results: list[tuple[str, str, str]] = []

    # ------------------------------------------------------------------ driver
    def run(self) -> int:
        self.paths.ensure()
        plat.restrict_to_owner(self.paths.credentials_dir)
        self.ui.panel("Encrypted CSwap sync via Google Drive.\nRe-running is safe: finished "
                      "steps are detected and skipped.", "CSwapSync setup")
        self.role = self._choose_role()
        steps = self._steps()
        for idx, (title, fn) in enumerate(steps, 1):
            self.ui.print(f"\n[bold cyan]Step {idx}/{len(steps)}[/] [bold]{title}[/]")
            status, detail = self._run_step(fn)
            self.results.append((title, status, detail))
            self.ui.print(f"  {ICON[status]} {detail}")
            if status == FAIL:
                break
        return self._summary()

    def _run_step(self, fn: StepFn) -> tuple[str, str]:
        from googleapiclient.errors import HttpError
        while True:
            try:
                return fn()
            except (SyncError, OSError, HttpError) as exc:
                self.ui.print(f"  [red]FAIL {_explain(exc)}[/]")
                if not self.ui.confirm("  Retry this step?", default=True):
                    return FAIL, _explain(exc)

    def _choose_role(self) -> str:
        existing = (read_json(self.paths.device_file, default={}) or {}).get("role", "")
        default = "2" if existing == "receiver" else "1"
        self.ui.print("\n  [bold]1[/] Sender   - Claude accounts already live on THIS device (e.g. Dell)"
                      "\n  [bold]2[/] Receiver - accounts should come FROM Drive to this device (e.g. HP)")
        return "sender" if self.ui.choose("Is device ka role?", ["1", "2"], default) == "1" \
            else "receiver"

    def _steps(self) -> list[tuple[str, StepFn]]:
        common = [("Device identity", self.step_device),
                  ("Config + CSwap", self.step_config),
                  ("Encryption keys", self.step_keys),
                  ("Google OAuth client JSON", self.step_client_json),
                  ("Google Drive login", self.step_drive_auth)]
        if self.role == "sender":
            return common + [("Trust receiver device (optional)", self.step_trust),
                             ("Auto-sync after token refresh", self.step_refresh_hook),
                             ("Upload to Drive", self.step_push),
                             ("Plaintext export cleanup", self.step_cleanup),
                             ("Test", self.step_test)]
        return common + [("Trust sender device", self.step_trust),
                         ("Read cloud backup", self.step_cloud_access),
                         ("Restore accounts on this device", self.step_restore),
                         ("Test", self.step_test)]

    # ------------------------------------------------------------------ steps
    def step_device(self) -> tuple[str, str]:
        raw = read_json(self.paths.device_file, default={}) or {}
        device_id = raw.get("device_id", "")
        if plat.valid_device_id(device_id) and self.ui.confirm(
                f"  Device id '{device_id}' rakhein?", default=True):
            status = OK
        else:
            if self.paths.credentials_dir.joinpath("age-identity.txt").exists():
                self.ui.print("  [yellow]Keys pehle se hain: id badli to dusre device pe "
                              "dobara 'trust' karna padega.[/]")
            while True:
                device_id = self.ui.ask("  Device id (letters/digits/-)",
                                        plat.default_device_id(self.role))
                if plat.valid_device_id(device_id):
                    break
                self.ui.print("  [red]sirf a-z, 0-9 aur '-' (max 40)[/]")
            status = OK
        atomic_write_json(self.paths.device_file, {
            "device_id": device_id, "role": self.role, "platform": sys.platform})
        return status, f"{device_id} ({self.role}, {sys.platform})"

    def step_config(self) -> tuple[str, str]:
        if not self.paths.config_file.exists():
            atomic_write_json(self.paths.config_file, Config().defaults_json())
        raw = read_json(self.paths.config_file, default={})
        cswap = raw.get("cswap_bin", "")
        if not cswap or not _exists(cswap):
            cswap = plat.find_cswap() or ""
        while not cswap or not _exists(cswap):
            self.ui.print("  CSwap nahi mila. Install: [bold]uv tool install claude-swap[/]")
            cswap = self.ui.ask("  cswap ka path (install ke baad Enter = dobara dhundo)", "")
            cswap = cswap or plat.find_cswap() or ""
        adapter = CswapAdapter(str(Path(cswap).expanduser()), raw.get("refresh_service", "cswap-refresh.service"),
                               **({"runner": self.runner} if self.runner else {}))
        version = adapter.version()
        count = int(raw.get("expected_account_count", 2))
        if self.role == "sender":
            count = _managed_count(adapter) or count
        while True:
            answer = self.ui.ask("  Kitne Claude accounts sync honge?", str(count)).strip()
            if answer.isdigit() and int(answer) >= 1:
                count = int(answer)
                break
            self.ui.print("  [red]positive number chahiye[/]")
        raw.update(cswap_bin=cswap, expected_account_count=count)
        atomic_write_json(self.paths.config_file, raw)
        self._reload()
        return OK, f"cswap {version} at {cswap}; {count} accounts"

    def step_keys(self) -> tuple[str, str]:
        keys = self.app.keys
        created = not keys.exists()
        if created:
            keys.generate()
        for path in (self.paths.credentials_dir, keys.identity_path, keys.signing_path):
            plat.restrict_to_owner(path)
        bundle = keys.public().bundle()
        other = "Receiver (HP)" if self.role == "sender" else "Sender (Dell)"
        self.ui.panel(f"Neeche wali PUBLIC key {other} pe chahiye: wahan setup me paste karo\n"
                      f"ya 'cswapsync trust \'<key>\''. Private key isi device pe rahegi.",
                      "Is device ki public key")
        self.ui.raw(bundle)   # one unwrapped line: safe to copy
        return OK, "generated new keypair" if created else "existing keypair kept"

    def step_client_json(self) -> tuple[str, str]:
        dest = self.paths.client_secret_file
        if dest.exists() and plat.is_desktop_client(dest):
            return OK, f"present (project {plat.client_project(dest)})"
        if self.role == "receiver":
            self.ui.print("  Sender wali machine ki [bold]same[/] google-client-secret.json chahiye.")
        for cand in plat.client_secret_candidates():
            if self.ui.confirm(f"  Use {cand} (project {plat.client_project(cand)})?", True):
                return self._install_client(cand)
        while True:
            src = Path(self.ui.ask("  OAuth Desktop client JSON ka path").strip().strip('"'))
            if src.is_file() and plat.is_desktop_client(src):
                return self._install_client(src)
            self.ui.print("  [red]file nahi mili ya 'Desktop app' type client nahi hai[/]")

    def _install_client(self, src) -> tuple[str, str]:
        shutil.copyfile(src, self.paths.client_secret_file)
        plat.restrict_to_owner(self.paths.client_secret_file)
        return OK, f"copied (project {plat.client_project(self.paths.client_secret_file)})"

    def step_drive_auth(self) -> tuple[str, str]:
        try:
            drive = self.app.drive(interactive=False)
        except ConfigurationError:
            self.ui.print("  Browser khulega. Wahi Google account chuno jo "
                          + ("receiver" if self.role == "sender" else "sender") + " pe hai.")
            drive = self.app.drive(interactive=True)
        drive.ensure_layout()
        self.app.state.save()
        plat.restrict_to_owner(self.paths.google_token_file)
        return OK, f"Drive reachable; '{self.app.config.drive_folder_name}' folders ready"

    def step_trust(self) -> tuple[str, str]:
        peers = [parse_bundle(b).device_id for b in self.app.config.trusted_devices]
        required = self.role == "receiver" and not peers
        if peers and not self.ui.confirm(f"  Trusted: {', '.join(peers)}. Aur add karna hai?",
                                         default=False):
            return OK, f"trusted: {', '.join(peers)}"
        where = "receiver ke setup (step 3)" if self.role == "sender" else "sender pe 'cswapsync keys show'"
        while True:
            bundle = self.ui.ask(f"  Dusre device ki public key paste karo ({where})"
                                 + ("" if required else ", Enter = skip"), "").strip()
            if not bundle and not required:
                return (OK if peers else WARN,
                        f"trusted: {', '.join(peers)}" if peers else
                        "skipped: backup sirf isi device pe decrypt hoga (baad me 'cswapsync trust')")
            try:
                peer = trust_bundle(self.paths, self.app.device.device_id, bundle)
            except ConfigurationError as exc:
                self.ui.print(f"  [red]{exc}[/]")
                continue
            self._reload()
            fp = bundle.strip()[-8:]
            return OK, f"trusted '{peer.device_id}' (end …{fp}: dusre device pe match karo)"

    def step_refresh_hook(self) -> tuple[str, str]:
        if IS_WINDOWS:
            return SKIP, "Windows: no cswap-refresh.service; use 'cswapsync push'"
        if not plat.has_refresh_unit():
            return WARN, "cswap-refresh.service nahi mila; manual 'cswapsync push'"
        gate = plat.lease_gate_installed()
        if plat.refresh_hook_installed(gate):
            return OK, "already wired into cswap-refresh.service"
        if not self.ui.confirm("  Refresh ke turant baad Drive sync (same service) lagayein?", True):
            return SKIP, "not installed"
        if not gate and self.app.config.trusted_devices:
            gate = self.ui.confirm("  Dusre device ke session ke dauraan yahan ka refresh skip "
                                   "karein? (token rotation se bachata hai)", default=True)
        notes = plat.install_refresh_hook(gate)
        return OK, "; ".join(["sync added to cswap-refresh.service"
                              + (" + lease gate" if gate else ""), *notes])

    def step_push(self) -> tuple[str, str]:
        mgr = self.app.manager()
        mgr.lease.acquire()
        try:
            manifest = mgr.push(release=True)
        except Exception:
            mgr.lease.release()
            raise
        if manifest is None:
            return OK, f"cloud r{mgr.state.get('last_synced_revision')} already current"
        return OK, (f"uploaded r{manifest.revision} ({manifest.account_count} accounts) for "
                    f"{', '.join(manifest.recipients)}; checksum verified")

    def step_cleanup(self) -> tuple[str, str]:
        strays = [p for p in plat.STRAY_EXPORTS if p.exists()]
        if not strays:
            return OK, "no plaintext exports found"
        for path in strays:
            self.ui.print(f"  [yellow]{path} me raw refresh tokens plaintext me hain.[/]")
            if self.ui.confirm("  Delete karein? (cloud backup verified hai)", default=False):
                path.unlink()
                return OK, f"deleted {path}"
        return WARN, "kept plaintext export (delete later)"

    def step_cloud_access(self) -> tuple[str, str]:
        mgr = self.app.manager()
        cloud = mgr.cloud_manifest()
        if cloud is None:
            raise SyncError("cloud me abhi koi backup nahi; sender pe 'cswapsync push' chalao")
        try:
            verified = mgr.fetch_cloud(cloud)
        except IntegrityError as exc:
            bundle = self.app.keys.public().bundle()
            raise SyncError(f"{exc}.\n  Sender pe chalao:\n    cswapsync trust '{bundle}'\n"
                            f"    cswapsync push\n  phir yahan Retry.") from exc
        return OK, (f"r{cloud.revision} from {cloud.device_id}: signature, SHA256, decrypt ok, "
                    f"{verified.summary.account_count} accounts")

    def step_restore(self) -> tuple[str, str]:
        mgr = self.app.manager()
        has_local = _has_local_accounts(self.app.adapter)
        self.ui.print("  Cloud ke accounts is device ki CSwap me import honge."
                      + (" Pehle local accounts ka rollback snapshot banega." if has_local else "")
                      + "\n  [bold]Claude Code band hona chahiye.[/]")
        if not self.ui.confirm("  Abhi import karein?", default=False):
            return SKIP, "later: cswapsync pull --yes" + ("" if has_local else " --bootstrap")
        msg = mgr.pull(apply=True, accept_cloud=True, bootstrap=not has_local)
        if not self.ui.confirm("  Abhi isi device pe Claude use karoge? (lease yahin rahegi)",
                               default=True):
            mgr.push(release=True)
            msg += "; lease released"
        return OK, msg

    def step_test(self) -> tuple[str, str]:
        mgr = self.app.manager()

        def cloud_roundtrip():
            cloud = mgr.cloud_manifest()
            if cloud is None:
                return (WARN, "cloud backup", "none yet")
            v = mgr.fetch_cloud(cloud)
            return (OK, "cloud backup", f"r{cloud.revision}: download+verify+decrypt ok, "
                    f"{v.summary.account_count} accounts")
        results = run_doctor(self.paths, self.app.config,
                             [cswap_version_check(self.app.adapter), cloud_roundtrip])
        table = Table(show_header=False, box=None, padding=(0, 1))
        for status, name, detail in results:
            table.add_row(ICON.get(status, status), name, detail)
        self.ui.console.print(table)
        fails = [n for s, n, _ in results if s == FAIL]
        warns = sum(1 for s, _, _ in results if s == WARN)
        if fails:
            return FAIL, f"{len(fails)} failed: {', '.join(fails)}"
        return (WARN if warns else OK), f"{len(results)} checks passed, {warns} warnings"

    # ------------------------------------------------------------------ helpers
    def _reload(self) -> None:
        self.app = App(paths=self.paths, runner=self.runner)

    def _summary(self) -> int:
        table = Table(title="Setup summary")
        table.add_column("")
        table.add_column("Step")
        table.add_column("Result")
        for title, status, detail in self.results:
            table.add_row(ICON[status], title, detail)
        self.ui.console.print(table)
        failed = any(s == FAIL for _, s, _ in self.results)
        if not failed:
            hint = ("Roz: kuch nahi (refresh ke baad auto-sync). Dusre device pe jaane se pehle: "
                    "'cswapsync push'." if self.role == "sender" else
                    "Kaam se pehle: 'cswapsync pull --yes'. Kaam ke baad: 'cswapsync push'.")
            self.ui.print(f"\n[green]Setup complete.[/] {hint}")
        return 1 if failed else 0


def _exists(path: str) -> bool:
    return Path(path).expanduser().exists() or shutil.which(path) is not None


def _managed_count(adapter: CswapAdapter) -> int | None:
    try:
        raw = adapter._run("status", "--json", timeout=60)
        return int(json.loads(raw).get("totalManagedAccounts") or 0) or None
    except (SyncError, ValueError, TypeError):
        return None


def _has_local_accounts(adapter: CswapAdapter) -> bool:
    """True only when the local accounts form a valid export worth a rollback snapshot.

    A receiver may carry broken leftovers (e.g. a slot with no OAuth tokens). Those make
    the rollback snapshot's validation raise IntegrityError and abort the restore, so they
    are treated as "nothing to preserve" and the cloud state is bootstrapped over them.
    """
    try:
        validate_export_payload(adapter.export_payload(), None)
        return True
    except (SyncError, IntegrityError):
        return False


def _explain(exc: BaseException) -> str:
    text = str(exc)
    if "accessNotConfigured" in text or "has not been used in project" in text:
        return ("Google Drive API is project me enable nahi: Cloud Console > APIs > "
                "Google Drive API > Enable, 2-3 min ruko, Retry")
    return text.split("\n  Details")[0][:600]
