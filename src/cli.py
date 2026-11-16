"""cswapsync command-line entry point.

Exit codes: 0 ok, 1 generic failure, 2 configuration, 4 revision conflict,
5 lease, 6 integrity, 7 CSwap busy, 8 Drive unreachable, 9 refresh unhealthy.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.umask(0o077)

from app_context import App, trust_bundle  # noqa: E402
from doctor import OK, cswap_version_check, print_results, run_doctor  # noqa: E402
from logging_config import setup_logging  # noqa: E402
from settings import Paths, ProcessLock, SyncError  # noqa: E402
from sync_manager import SyncManager  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cswapsync", description="Encrypted CSwap sync via "
                                "Google Drive with an exclusive device session lease.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="interactive first-time setup (sender or receiver)")
    sub.add_parser("status", help="local + cloud sync state and lease holder")
    pull = sub.add_parser("pull", help="PRE-session: take lease, verify cloud, optionally restore")
    pull.add_argument("--yes", action="store_true", help="actually restore the newer cloud state")
    pull.add_argument("--accept-cloud", action="store_true",
                      help="take cloud even though local changed since last sync")
    pull.add_argument("--break-lease", action="store_true",
                      help="forcibly take an ACTIVE lease from the other device")
    pull.add_argument("--bootstrap", action="store_true",
                      help="fresh device with no cswap accounts yet")
    push = sub.add_parser("push", help="POST-session: export, encrypt, upload, release lease")
    push.add_argument("--keep-lease", action="store_true")
    push.add_argument("--accept-conflict", action="store_true",
                      help="after reconciling, make this device's state the new cloud revision")
    sub.add_parser("backup", help="local encrypted backup only (no cloud)")
    rs = sub.add_parser("restore", help="restore from a verified backup")
    src = rs.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="local backups/*.age file")
    src.add_argument("--cloud-revision", type=int)
    src.add_argument("--latest-rollback", action="store_true")
    rs.add_argument("--yes", action="store_true", help="apply (default: verify only)")
    doc = sub.add_parser("doctor", help="diagnostics")
    doc.add_argument("--health", action="store_true", help="also run cswap account health")
    doc.add_argument("--offline", action="store_true", help="skip Google Drive checks")
    sub.add_parser("auth", help="interactive Google OAuth consent (opens browser)")
    keys = sub.add_parser("keys", help="device keypair")
    keys.add_argument("action", choices=["init", "show"])
    trust = sub.add_parser("trust", help="trust another device's public key bundle")
    trust.add_argument("bundle")
    pr = sub.add_parser("post-refresh", help="run after cswap-refresh: verify, then push")
    pr.add_argument("--inline", action="store_true",
                    help="running as ExecStartPost inside cswap-refresh.service")
    pr.add_argument("--notify", action="store_true", help="desktop notification of result")
    sub.add_parser("heartbeat", help="extend this device's lease if it holds one")
    sub.add_parser("release", help="release this device's lease without pushing")
    sub.add_parser("lease-check", help="exit 0 if lease is free or ours, 1 if another "
                   "device holds it (systemd ExecCondition gate)")
    return p


def cmd_status(app: App) -> int:
    print(f"device            : {app.device.device_id} ({app.device.role})")
    print(f"keys              : {'present' if app.keys.exists() else 'MISSING'}")
    print(f"trusted peers     : {len(app.config.trusted_devices)}")
    print(f"last synced rev   : {app.state.get('last_synced_revision')}")
    print(f"last push (UTC)   : {app.state.get('last_push_utc')}")
    local = sorted(app.paths.backups_dir.glob("*.age"))
    print(f"local backups     : {len(local)}")
    busy = app.adapter.busy_reasons()
    print(f"cswap busy        : {'; '.join(busy) if busy else 'no'}")
    try:
        mgr = app.manager()
        cloud = mgr.cloud_manifest()
        holder = mgr.lease.holder()
        print(f"cloud revision    : {cloud.revision if cloud else None}"
              + (f" from {cloud.device_id} at {cloud.created_at_utc}" if cloud else ""))
        print(f"lease holder      : {holder.device_id + ' until ' + holder.expires_at if holder else 'free'}")
        if cloud and cloud.revision != app.state.get("last_synced_revision"):
            print("NOTE              : cloud and local revisions differ (pull or reconcile)")
    except SyncError as exc:
        print(f"cloud             : unavailable ({exc})")
    return 0


def cmd_doctor(app: App, health: bool, offline: bool) -> int:
    extra = [cswap_version_check(app.adapter)]
    if health:
        def account_health():
            res = app.adapter.health()
            bad = {k: v for k, v in res.items() if v != "ok"}
            return (OK if not bad else "FAIL", "account health",
                    f"{len(res)} accounts, " + ("all ok" if not bad else f"not ok: {bad}"))
        extra.append(account_health)
    if not offline:
        def drive_layout():
            drive = app.drive()
            drive.ensure_layout()
            app.state.save()
            return (OK, "google drive", f"reachable, '{app.config.drive_folder_name}' layout ok")
        extra.append(drive_layout)
    return print_results(run_doctor(app.paths, app.config, extra))


def cmd_trust(app: App, bundle: str) -> int:
    peer = trust_bundle(app.paths, app.device.device_id, bundle)
    print(f"trusted device '{peer.device_id}'. New backups will be encrypted to it; "
          f"run 'cswapsync push' so the current cloud backup is re-encrypted for it.")
    return 0


def dispatch(app: App, args: argparse.Namespace) -> int:
    if args.cmd == "keys":
        if args.action == "init":
            app.keys.generate()
            print("device keys generated (private keys stay in credentials/)")
        print(app.keys.public().bundle())
        return 0
    if args.cmd == "trust":
        return cmd_trust(app, args.bundle)
    if args.cmd == "auth":
        app.drive(interactive=True).ensure_layout()
        app.state.save()
        print("Google Drive authorised; folder layout ready")
        return 0
    if args.cmd == "doctor":
        return cmd_doctor(app, args.health, args.offline)
    if args.cmd == "status":
        return cmd_status(app)

    if args.cmd == "heartbeat" and not app.paths.google_token_file.exists():
        print("Google Drive not authorised yet; heartbeat skipped")
        return 0
    mgr = app.manager()
    if args.cmd == "backup":
        print(f"local backup: {mgr.backup()}")
    elif args.cmd == "push":
        m = mgr.push(release=not args.keep_lease, accept_conflict=args.accept_conflict)
        print(f"pushed revision {m.revision}" if m else "cloud already up to date")
    elif args.cmd == "pull":
        print(mgr.pull(apply=args.yes, accept_cloud=args.accept_cloud,
                       break_lease=args.break_lease, bootstrap=args.bootstrap))
    elif args.cmd == "restore":
        return cmd_restore(mgr, args)
    elif args.cmd == "post-refresh":
        try:
            m = mgr.post_refresh(inline=args.inline)
        except SyncError as exc:
            if args.notify:
                notify("CSwapSync: Drive sync FAILED", str(exc))
            raise
        msg = f"uploaded revision {m.revision}" if m else "Drive already up to date"
        if args.notify:
            notify("CSwapSync: Drive synced", msg)
        print(f"post-refresh: {msg}")
    elif args.cmd == "heartbeat":
        holder = mgr.lease.holder()
        if holder and holder.device_id == app.device.device_id:
            print(f"lease extended until {mgr.lease.heartbeat().expires_at}")
        else:
            print("no lease held by this device; nothing to do")
    elif args.cmd == "lease-check":
        holder = mgr.lease.holder()
        if holder and holder.device_id != app.device.device_id:
            print(f"lease held by {holder.device_id}; skipping")
            return 1
        return 0
    elif args.cmd == "release":
        mgr.lease.release()
        print("lease released")
    return 0


def notify(title: str, body: str) -> None:
    """Best-effort desktop notification (same channel cswap-refresh.sh uses)."""
    import shutil
    import subprocess
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", title, body[:300]], check=False, timeout=10)


def cmd_restore(mgr: SyncManager, args: argparse.Namespace) -> int:
    if args.cloud_revision is not None:
        mgr.lease.acquire()  # restoring cloud state starts a managed session
        verified = mgr.fetch_history(args.cloud_revision)
    else:
        path = mgr.latest_rollback() if args.latest_rollback else args.file
        verified = mgr.load_local(path)
    m = verified.manifest
    print(f"verified backup r{m.revision} from {m.device_id} at {m.created_at_utc}: "
          f"{verified.summary.account_count} accounts, slots {list(verified.summary.slots)}")
    if not args.yes:
        print("verification only. Re-run with --yes to apply (a rollback snapshot is taken first).")
        return 0
    rollback = mgr.restore(verified)
    print(f"restored. rollback snapshot: {rollback}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "setup":
        from setup_wizard import UI, SetupWizard
        paths = Paths()
        paths.ensure()
        setup_logging(paths.logs_dir, args.verbose)
        try:
            with ProcessLock(paths.lock_file):
                return SetupWizard(paths, UI()).run()
        except KeyboardInterrupt:
            print("\nsetup cancelled; re-run 'cswapsync setup' any time")
            return 130
    try:
        app = App(args.verbose)
        if args.cmd in ("status", "doctor", "keys", "trust"):
            return dispatch(app, args)
        with ProcessLock(app.paths.lock_file):
            return dispatch(app, args)
    except SyncError as exc:
        print(f"cswapsync: {exc}", file=sys.stderr)
        try:
            app.log.error("%s failed: %s", args.cmd, exc)  # type: ignore[possibly-undefined]
        except NameError:
            pass
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        from googleapiclient.errors import HttpError
        if not isinstance(exc, HttpError):
            raise
        status = getattr(exc.resp, "status", "?")
        reason = exc.reason if hasattr(exc, "reason") else ""
        print(f"cswapsync: Google Drive error {status}: {reason}", file=sys.stderr)
        return 8


if __name__ == "__main__":
    sys.exit(main())
