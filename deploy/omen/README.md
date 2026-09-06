# Coordinator deployment (HP Omen, WSL2 Ubuntu)

> Before using this installer, complete [runtime recovery](../../docs/runtime-recovery.md).
> The WSL observations below are historical desktop observations, not facts about the current
> `Leannes-Laptop` Omen, where WSL was not installed at the 2026-09-05 inventory. This installer
> can activate a coordinator; a local empty store is not evidence that the Pi is retired.

Bringing Elrond up on the always-on coordinator described in
[`docs/runtime-topology.md`](../../docs/runtime-topology.md) (#38).

**Nothing here has been installed.** The units and the setup script are written and reviewed;
running them changes persistent configuration on the operator's machine and moves the durable
ledger, and both are operator decisions. This file exists so that decision is a short command
rather than a research project.

## Why user units rather than system units

The Pi runs system units under `/etc/systemd/system`, installed with sudo. These are **user
units** under `~/.config/systemd/user`, and the difference is not cosmetic:

- They install and start without root. Elrond needs no privilege it does not already have as
  the owning user, and a paper-trading daemon that runs as root is a blast radius nobody asked
  for.
- They stop when the user's last session ends — *unless lingering is enabled*, which is the one
  step below that does require an administrator prompt. Without it, "always on" means "on while
  a terminal is open", which is not what the topology promises.

## Prerequisites, in order

**1. WSL2 systemd.** Already enabled on this machine — `/etc/wsl.conf` carries `[boot]
systemd=true` and PID 1 is systemd. Nothing to do.

**2. Lingering.** This is the step that makes the coordinator survive a reboot with no terminal
open:

```bash
sudo loginctl enable-linger "$USER"
```

Verify with `loginctl show-user "$USER" --property=Linger` — it must print `Linger=yes`. This
is the one command here that needs a password, which is why it is not scripted.

**3. WSL memory ceiling.** WSL2 currently reports 15,847 MB against the Windows side's
32,486 MB — a default, not a hardware limit. Placement reads what the kernel reports, so raising
it changes which jobs are permitted. To lift it, in Windows `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
memory=24GB
```

Then `wsl --shutdown` from Windows and reopen. Leaving it alone is a legitimate choice; it means
heavy jobs are sized against 15 GB, which is still far above anything currently declared.

**4. Credentials.** `~/.config/quantbot/quantbot.env`, mode 600, outside the repository, exactly
as on the Pi. Set `TRADING_MODE=PAPER` and leave `LIVE_TRADING_ACKNOWLEDGED=false`; those are the
two settings the code actually reads (`config.py`), and both already default to the safe value.
The units read the file via `EnvironmentFile`; never put a credential in a unit file, which is
world-readable.

`setup.sh` refuses to install against a credential file that says otherwise, via
`deploy/refuse-live-trading.sh`. Check one yourself at any time:

```bash
bash deploy/refuse-live-trading.sh ~/.config/quantbot/quantbot.env
```

> **This line named `LIVE_TRADING` until 2026-09-03, and so did `setup.sh:25`.** No such setting
> exists — `Settings` ignores unknown variables — so the pre-flight check grepped for a name
> nothing reads, and would have passed an env file that really did set `TRADING_MODE=LIVE` while
> printing that it refused. The refusal in `config.py` was the boundary that held; the one here
> was decoration. Now shared with the Pi installer, which had no check at all, and covered by
> `tests/unit/test_deployment_live_trading_guard.py`. See `docs/node-deployment.md` under
> *Safety*.

## Install

```bash
./deploy/omen/setup.sh
```

It copies the units, reloads the user manager, enables and starts them, and prints the status of
each. It refuses to run if lingering is off, because installing an always-on service that stops
when you close a terminal is worse than not installing it — it looks deployed.

## What runs, and what deliberately does not

| Unit | Purpose |
|---|---|
| `quantbot-daemon.service` | The paper-trading daemon: cycles, risk, reconciliation, kill switch |
| `quantbot-dashboard.service` | The read-only operations page |

Kronos and semantic workers are **not** units. They are dispatched per job through the worker
interface and exit when done. A long-lived inference service would be a process competing with
the trading daemon for the same memory, on the host whose whole job is to stay up.

The Pi keeps the watchdog role and its own dashboard. Nothing here replaces it, and nothing here
depends on it.

## Migrating the durable ledger

Not scripted, on purpose. The ledger holds the paper account's fills and qualification days, it
is the evidence store the promotion ladder reads, and #16 forbids reconstructing forward
observations. Copying it is a one-way operator action that deserves its own backup and its own
verification:

```bash
# On the current host, with the daemon stopped:
sqlite3 quantbot.db "PRAGMA integrity_check;"
sqlite3 quantbot.db ".backup '/tmp/quantbot-backup.db'"
# Then copy the backup across, and on the coordinator, before starting anything:
sqlite3 quantbot.db "PRAGMA integrity_check;"
quantbot doctor
```

`doctor` is the check that matters: it reports schema version, kill-switch state and
reconciliation freshness. Start the daemon only after it is clean.

## Rollback

```bash
systemctl --user disable --now quantbot-daemon.service quantbot-dashboard.service
```

The Pi deployment is untouched by any of this and continues to run.
