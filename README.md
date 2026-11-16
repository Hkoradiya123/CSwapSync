# CSwapSync

Encrypted hand-off of the two CSwap-managed Claude accounts between the Dell
(Ubuntu, daily use) and the HP (Windows 10) through Google Drive.

## Why this exists

CSwap's token refresh **rotates OAuth refresh tokens**. If two machines hold
copies of the same account and both refresh, the second refresh kills the
first machine's token (`invalid_grant`). So syncing the files is not enough:
only **one device at a time** may use the accounts, and the device that used
them last must publish its rotated tokens before the other one starts.

CSwapSync handles that with:

| Concern | Mechanism |
|---|---|
| Who may use the accounts now | cloud **lease** (`status/lease.json`): expiry, heartbeat, fail-closed |
| Confidentiality | `age` (X25519) encryption to every trusted device, done in memory |
| Who produced a backup | Ed25519 signature on every manifest, checked against trusted device keys |
| Corruption / partial upload | SHA256 of ciphertext + plaintext, re-download verify, commit-point manifest |
| Two devices diverging | revision numbers; on conflict both states are kept and a human decides |
| Bad restore | automatic encrypted rollback snapshot before every restore |

Plaintext credentials only exist in memory and in the pipe to/from
`cswap export -` / `cswap import -`. They are never written to disk, never
uploaded and never logged.

## Layout

```
src/cswapsync/settings.py        paths, config, state, atomic writes, error classes
src/cswapsync/logging_config.py  rotating log with secret redaction
src/cswapsync/encryption.py      device keys, age encrypt/decrypt, Ed25519 sign/verify
src/cswapsync/manifest.py        signed backup manifest + digests
src/cswapsync/cswap_adapter.py   ONLY module that runs cswap (verified against 0.29.0)
src/cswapsync/google_drive.py    OAuth (drive.file), retry/backoff, stable-id storage
src/cswapsync/lease.py           session lease
src/cswapsync/sync_manager.py    backup / push / pull / restore / post-refresh orchestration
src/cswapsync/doctor.py          diagnostics
src/cswapsync/cli.py             `cswapsync` command
src/cswapsync/setup_wizard.py   `cswapsync setup` (sender/receiver)
src/cswapsync/platform_setup.py systemd/ACL helpers
src/cswapsync/systemd/          unit templates (binary path filled at install)
scripts/               install scripts + shell wrappers
```

Drive: `CSwap-Sync/{current,history,status}/`. `current/manifest.json` is the
commit point and is written last. `history/` keeps at least 10 verified backups.

## Install / setup (Linux and Windows)

Install like claude-swap (Linux and Windows), then run the wizard:

```bash
uv tool install cswapsync        # or: pipx install cswapsync
cswapsync setup
```
Before the PyPI release, install from GitHub instead:
`uv tool install git+https://github.com/Hkoradiya123/CSwapSync`.
Upgrade: `uv tool upgrade cswapsync`, then `cswapsync setup` again (it re-points
the systemd units at the new binary if needed).

Code lives in the tool's own venv; data (keys, config, backups, logs) lives in
`~/CSwapSync` (`%USERPROFILE%\CSwapSync` on Windows) or `$CSWAPSYNC_HOME`, so
upgrades never touch it. `scripts/setup.sh` / `scripts/setup.ps1` do the same
install from a checkout.

The wizard (`cswapsync setup`, re-runnable any time) asks first: **Sender** (Claude
accounts already live here, e.g. Dell) or **Receiver** (accounts come from Drive,
e.g. HP). The wizard then only asks for what is missing:

| Step | Sender | Receiver |
|---|---|---|
| device id, cswap path, account count | ✓ | ✓ |
| keypair (shows this device's public key) | ✓ | ✓ |
| Google OAuth Desktop client JSON (auto-finds `~/.config/workspace-mcp/client_secret.json`, `~/Downloads/client_secret*.json`) | ✓ | ✓ same JSON as sender |
| Google login (browser, `drive.file`) | ✓ | ✓ same Google account |
| trust the other device's public key | optional | required |
| hook Drive sync into `cswap-refresh.service` (Linux) | ✓ | – |
| first push / read + verify cloud backup | push | read; if not encrypted to it, prints the exact `trust` + `push` to run on the sender, then Retry |
| restore accounts (rollback snapshot first; bootstrap if no local accounts) | – | asks first |
| delete stray plaintext export | asks | – |
| test (doctor + cloud download/verify/decrypt) | ✓ | ✓ |

Order for a new pair: sender setup → receiver setup (it shows its key and asks for
the sender's) → on sender `cswapsync trust '<receiver key>'` + `cswapsync push` →
Retry on the receiver. A newly trusted device always forces a re-encrypted push.

### Google OAuth client (one time)

Cloud Console: enable **Google Drive API**, consent screen with scope `drive.file`,
Credentials → OAuth client ID → **Desktop app** → download JSON. Both devices must
use the same JSON and the same Google account (`drive.file` only shows files made
by the same OAuth app).

## Daily workflow

```bash
cswapsync pull            # BEFORE using Claude here: take lease, verify cloud
cswapsync pull --yes      # ...and restore if cloud is newer
                          #    (quit Claude Code first; restore refuses while it runs)
# ... work ...
cswapsync push            # AFTER: export, encrypt, upload, verify, release lease
```
Wrappers: `scripts/sync.sh pre|post`, `preflight.sh`, `health-check.sh`.

`cswapsync status` shows local/cloud revision and the lease holder.

## Refresh integration (one service)

The existing `cswap-refresh.timer` (09:07 daily + 2 min after boot, `Persistent=true`;
**not** 10 PM) and `cswap-refresh.sh` are untouched. A drop-in
(`~/.config/systemd/user/cswap-refresh.service.d/20-cswapsync-sync.conf`) adds
`ExecStartPost=cswapsync post-refresh --inline --notify`, so the same
`cswap-refresh.service` does: refresh → its notifications → Drive sync. The sync:

1. reads the newest block of the refresh log: must be < 45 min old, `exit=0`,
   no `re-login needed|invalid_grant|belongs to another account|refresh deferred`
   (the refresh script itself always exits 0, so that proves nothing);
2. `cswap list --json`: both accounts `usageStatus == ok`;
3. if HP holds the lease → refuses (exit 5); else push + verify (no upload if
   nothing changed);
4. desktop notification "Drive synced" / "Drive sync FAILED: <reason>".

A failed sync marks `cswap-refresh.service` failed (the refresh itself already
happened). No automatic retry: the next boot or 09:07 run tries again, or run
`cswapsync push` by hand. Delete the drop-in to undo.

Optional: `scripts/setup.sh --gate-refresh` adds `10-cswapsync-lease-gate.conf` so
the refresh is **skipped** while the HP holds the lease (prevents Dell rotating
tokens out from under the HP).

Logs: `journalctl --user -u cswap-refresh` and `logs/cswapsync.log`.

## Restore / rollback

```bash
cswapsync restore --cloud-revision 12          # verify only
cswapsync restore --cloud-revision 12 --yes    # apply (rollback snapshot first)
cswapsync restore --file backups/r000012-<id>.age --yes
scripts/rollback.sh --yes                      # undo the last restore
```
Restore refuses while cswap, its refresh, or Claude Code is running.

## Conflicts

If cloud moved on since this device's last sync and local also changed,
push/pull stop with exit 4, keeping both states (`backups/conflict-*`,
`history/conflict-*`, `status/conflict.json`). Decide, then either
`cswapsync pull --yes --accept-cloud` or `cswapsync push --accept-conflict`.

## Exit codes

0 ok · 1 generic · 2 config/auth · 4 conflict · 5 lease · 6 integrity ·
7 cswap busy · 8 Drive unreachable · 9 refresh/health not ok

## Tests

```bash
uv venv && uv pip install -r requirements.txt -e .
.venv/bin/python -m pytest -q
```
Fake Drive + fake cswap with dummy tokens. Real Drive is not exercised by the
test suite.

## Known limits

- Drive has no compare-and-swap. Lease acquisition is write → settle → re-read;
  a simultaneous acquire within ~3 s could still race. Expiry covers it.
- `cswap list --json` (health) performs a usage fetch and may refresh tokens.
- Windows (HP): the Python code is cross-platform (pyrage/cryptography wheels,
  msvcrt locking, tasklist), but `scripts/*.sh` and systemd units are Linux-only;
  the HP needs its own launcher/Task Scheduler setup.
