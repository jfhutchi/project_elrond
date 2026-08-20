# Running the daemon on a Raspberry Pi

The trading daemon has to be alive at every session close. A desktop that sleeps, or reboots for
updates, is the wrong host for that. A dedicated Pi is a better one.

## Which boards work

| board | arch | path | verdict |
|---|---|---|---|
| Pi Zero 2 W, Pi 3/4/5 (64-bit OS) | aarch64 | uv + PyPI | works |
| **Pi Zero / Zero W (original)** | **armv6l** | **venv + piwheels** | **works — verified on hardware** |
| Pi 2/3/4 on 32-bit OS | armv7l | venv + piwheels | works; 64-bit is better |

Check with:

```bash
uname -m && grep -E '^(Model|Revision)' /proc/cpuinfo
```

### The ARMv6 case (Pi Zero / Zero W)

The original Zero is ARMv6, single-core 1GHz, 512MB RAM, and **cannot run a 64-bit OS at all** —
reflashing to arm64 is not an option for this board.

PyPI publishes no `armv6l` wheel for `pydantic-core`, which is compiled Rust. Installing from PyPI
would fall back to building it from source on one slow core, which takes hours and usually ends in
an OOM kill.

[piwheels.org](https://www.piwheels.org/project/pydantic-core/) exists for exactly this and **does
publish `armv6l` wheels**. So the board works; it just has to install from that index rather than
compile.

**Confirmed on hardware** — Pi Zero W Rev 1.1, Raspbian 13 (trixie), Python 3.13.5, 427MB RAM:

```
Downloading https://www.piwheels.org/simple/pydantic-core/
  pydantic_core-2.46.4-cp313-cp313-linux_armv6l.whl (2.1 MB)
Successfully downloaded pydantic-core
```

2.1MB, seconds, no compiler. That was the only genuine blocker for this board.

Two consequences for ARMv6:

- **Python 3.11+, so Bookworm (Debian 12) or newer.** Bookworm ships 3.11 and trixie ships 3.13;
  both still support ARMv6. Bullseye (3.9) and Buster (3.7) are too old and need a reflash — worth
  checking if the Pi has been running something else for years.
- **pip and venv, not uv.** uv is also Rust and its ARMv6 support is not something to bet an
  unattended trading process on. `setup.sh` picks the path automatically from `uname -m`.

Note that **piwheels is not always preconfigured.** Raspberry Pi OS often ships `/etc/pip.conf`
pointing at it, but the trixie image tested here did not have that file at all. `setup.sh` checks
and adds it rather than assuming — without it, pip silently falls back to building pydantic-core
from source, which on this board means hours and probably an OOM kill.

## Why the resource profile fits

The whole dependency set is light:

```
alembic  httpx  pydantic  pydantic-settings  pyyaml
sqlalchemy  structlog  tzdata  websockets
```

The strategy evaluates 23 symbols on daily bars and makes one decision per session. Arithmetic is
`Decimal` throughout, not vectorised. There is no model training and no backtest in the daemon
path.

## Install

```bash
git clone --branch elrond-v0.2 https://github.com/jfhutchi/project_elrond.git ~/quantbot
bash ~/quantbot/deploy/pi/setup.sh
```

It detects the architecture and picks the install path itself. It only touches `apt` if something
is actually missing — on a current image, nothing is — and it refuses to run at all if an
`apt`/`dpkg` lock is held by an upgrade in another shell, rather than interrupting it.

Idempotent — safe to re-run. It installs the code and the systemd unit, and creates
`/etc/quantbot/quantbot.env` as an empty template. It does not start anything, and it never
writes credentials.

## Migrating from an existing host

The qualification window is the thing to protect. It is 30 sessions of forward evidence and it
cannot be rebuilt by re-running anything.

Strategy identity is derived from the **config**, not the host or the git commit — the same config
produces the same `strategy_id` on any machine. So the window survives a host move as long as the
ledger comes with it.

1. **Stop the old host first.** Copying a live SQLite database in WAL mode can capture a partial
   write.

   ```powershell
   Stop-ScheduledTask -TaskName QuantBotDaemon
   Disable-ScheduledTask -TaskName QuantBotDaemon
   ```

2. **Copy the ledger.**

   ```bash
   scp quantbot.db pi@<host>:~/quantbot-data/quantbot.db
   ```

3. **Fill in credentials** in `/etc/quantbot/quantbot.env` (mode 600). Paper keys only.

4. **Verify before starting.** On ARMv6 the venv is at `.venv`; on aarch64 use `uv run`.

   ```bash
   cd ~/quantbot && ./.venv/bin/quantbot status
   cd ~/quantbot && ./.venv/bin/quantbot reconcile
   ```

   `status` should report `trading_mode: PAPER`, `live_credentials_configured: false`, and a
   cleared kill switch. `reconcile` should report `RECONCILED` with `diff_count: 0`.

   If the first `reconcile` reports a stale-snapshot difference, run it again — the first run
   writes a fresh snapshot and the second compares against it. A persistent position mismatch is
   a real problem; a one-cent equity difference during market hours is mark drift.

5. **Start.**

   ```bash
   sudo systemctl start quantbot
   journalctl -u quantbot -f
   ```

**Never run both hosts against the same paper account.** Two daemons reconciling and trading the
same account will fight, and the ledgers will diverge from the broker and from each other.

## Rollback

The Pi changes nothing on the old host except that its task is disabled. To go back:

```powershell
Enable-ScheduledTask -TaskName QuantBotDaemon
Start-ScheduledTask  -TaskName QuantBotDaemon
```

Stop the Pi first (`sudo systemctl stop quantbot`), and copy the ledger back if the Pi ran any
sessions — otherwise those days are lost from the record.

## Operational notes

**The Pi has no real-time clock.** Session boundaries and point-in-time correctness both depend on
the clock being right. The unit orders itself after `time-sync.target`, and `setup.sh` warns if NTP
is not synchronised, but neither guarantees it. Confirm with `timedatectl`.

**Put the ledger somewhere durable.** SD cards corrupt under power loss, and the durable ledger is
the point of this system. A USB SSD is better. A high-endurance card plus clean shutdown is the
minimum.

**Restart policy is deliberately rate-limited.** `Restart=always` with `RestartSec=5` and
`StartLimitBurst=5` in 120s. A daemon that crashes on startup and restarts instantly forever will
hammer the broker API and get the account rate-limited, turning a small fault into an outage.
After five failures inside two minutes systemd stops trying and waits for a human — a process that
cannot start is something to investigate, not something to retry into the ground.

**Logs go to the journal**, not to a file:

```bash
journalctl -u quantbot -f
journalctl -u quantbot --since "1 hour ago"
```
