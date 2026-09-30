# Omen learner and PAPER verification — September 23, 2026

This is a read-only operational snapshot at 09:46 Eastern plus isolated source tests. It is not a
deployment or a qualification decision. Source stayed on `codex/elrond-learner` at `f3234ec`
until the source-only test and console wording changes in this pass. The Omen pins remained:
learner `2e559fa25eea`, daemon/protection `d10fe786321c`, console `ea5e0d5d38f3`.

## Operational evidence

- Alpaca PAPER `/clock` said open at 09:46:17 Eastern, closing at 16:00. `/account` was active;
  equity was $498.49 and cash $50.91. `/positions` returned DBC, EEM, EFA, IWM, QQQ, SPY, XLE,
  XLK and XLV. All 11 returned orders were filled; the last was the September 15 XLI sale.
- The independent protection cycle at 09:45:00 Eastern was `HEALTHY`: nine broker positions,
  nine evaluations, zero unprotected and zero exits required. The nine evaluation symbols exactly
  matched the fresh broker position set; each was reconciled, marked `protected`, and `NO_ACTION`.
  Latest reconciliation was `RECONCILED` with zero diffs; the kill switch was disengaged.
  Here `protected` means the local v2 loop checked broker marks against position loss and account
  drawdown rules and found no exit due. There are no broker-held stop orders. Future protection
  depends on the Omen, scheduler, credentials, network and broker being available.
- `Elrond Learner` was Ready, last ran at 09:39:35 Eastern with exit 0 and a matching 09:39:38
  receipt. The ten-minute trigger and logon trigger are installed. The receipt had zero prospective
  observations, predictions, outcomes or retraining; two historical seed models were published.
  It named `PROTECTED_FEATURE_WINDOW`, `PAPER_PROMOTION_REQUIRED` and
  `PAPER_EXECUTION_NOT_CONFIGURED`. Exit 0 only means the blocked pass completed.
- Daemon and protection tasks were running at their pinned commit. The learner task's actual
  principal was `LEANNES-LAPTOP\leepa`, `Interactive`, Limited; it has **no startup trigger**.
  The pinned learner runtime checkout, its host-visible D: Python and venv, environment file,
  learner store and output directories were accessible to that user; PAPER API HTTPS was reachable.
  Sign-out, sleep and
  reboot recovery were not tested. Unattended operation is blocked by the current interactive
  task registration, regardless of successful signed-in ticks.

## Learning path and October boundary

The scheduled task invokes `start-learner.ps1` and the worker; the forward cycle fetches actual
IEX minute snapshots. `process_snapshot` resolves eligible prior observations, scores model-bound
forecasts against exact targets, then checks the current session and protected range before
building 31-minute features. It records an immutable observation once per ten-minute slot and
issues predictions from already published seed models. Challenger retraining separately requires
20 training and five validation **forward session dates**, then 24 new matured cohorts and a
four-hour cooldown. Because seed models exist, this 25-session threshold does not gate initial
October forecasts or outcome scoring. It also does not grant qualification or PAPER orders.

No separate human observation approval artifact exists in the deployed forecast path. The
`PAPER_PROMOTION_REQUIRED` receipt code concerns broker entry authority; it does not gate local
forecast issuance. The console's old phrase “approved PAPER observation deployment” was
misleading and is corrected in source only. PAPER entry eligibility accepts the existing
`PAPER_OBSERVATION` promotion stage; it does not require `PAPER_QUALIFIED`, which is evaluated
later from at least 30 authentic PAPER days and 30 trades plus the economic criteria. PAPER entry
also requires exact model deployment, configured execution adapter and existing risk controls;
none was commissioned.

The learner dataset manifest protects April 1 through September 30 under canonical
`us-equities-daily`; the forward guard checks that identity against registry aliases and the
learner holdout. Synthetic separate-database tests cover September 30 refusal, October 1
observation and model forecast while retraining and PAPER entry remain blocked, missing lookback
bars, exact target availability, duplicate bars and duplicate ticks. The existing registry suite
also checks vendor aliases. If the protected registry is unchanged, the Omen is signed in and
awake, and complete timely minute bars arrive, the October 1 task ticks can automatically start
prospective local forecasts after 31 completed regular-session minutes (earliest around 10:01
Eastern). A later tick can resolve the first exact 30-minute target and score it. The first
milestone is a timestamped, model-bound October 1 forecast followed by an authentic resolved
target and score, not a PAPER trade or a profit claim. Missing bars or provider trouble defer it.

## Attribution and performance

Operational fills and intents identify `adaptive-momentum-v1-309894d8d8a5296e`, config 1.2.0,
as the producer of the existing positions and XLI round trip. The learner has submitted no
orders. The PAPER account received one $500 transfer September 8 and two $0.01 fees. XLI realized
P&L is -$0.8772; fresh nine-position unrealized P&L was -$0.6170. Broker equity of $498.49 was
-$1.51 (-0.302%) after cash flows at 09:46 Eastern. The cent-level difference from reconstructed
trade P&L is broker equity/price rounding. The 462 sampled ledger marks since funding show a
largest observed peak-to-trough drawdown of 2.18%, with an observation gap September 17–20;
that is not a complete intraday maximum.

A same-period rough passive reference buys SPY at its September 8 IEX close ($766.06) and marks
at the broker's 09:46 September 23 SPY position price ($771.445): +0.703% before costs and
dividends, about 1.005 percentage points above the account. Source and timestamp differences
limit precision. The separate January–March historical learner replay showed ridge -3.46% net,
mean -0.13% and matched SPY -6.02% under its intermittent matched-window simulation. Those
results do not describe the operational account or prospective performance.

## Verification, limits and human action

The packaged checkout venv reproduced two Windows service tests failing: the installer explicitly
rejected its AppData base Python as `VENV_BASE_NOT_HOST_VISIBLE`, while the wrapper test timed out
before launch with stderr suppressed. An isolated source copy linked to the existing host-visible
D: Python venv passed **20/20** service tests. This isolates the failing source checkout
environment, but the first wrapper test hid stderr, so its exact original failure is not proven
to be the same installer preflight rejection. Neither preflight was weakened. The learner,
registry, promotion and protection suite passed **394/394**; console learner and protection tests
passed **22/22**. Ruff, focused mypy (19 files) and `git diff --check` passed. The October boundary
test uses synthetic bars and separate temporary stores; no production clock, registry or forecast
was changed.

The old broker 403 remains an unresolved incident: these checks do not establish its cause.
Reboot/sign-out/sleep recovery also remains unverified. For the six operational tasks, the
approved unattended procedure is an elevated invocation from the clean pinned operational
checkout of `setup-windows.ps1 -EnvironmentFile
C:\Users\leepa\.config\quantbot\quantbot.env -Database
D:\Elrond\state\quantbot-paper-fresh-20260908.db -Mode Boot -Credential (Get-Credential)
-UpdateManaged`, after an audited backup, task export and a separately approved maintenance /
recovery test because the installer refuses running tasks. The existing `schedule-learner.ps1`
only registers an Interactive principal and does not implement that Boot procedure for the
learner. A separately reviewed learner task update would therefore still be needed. The current
user has a limited token; no principal, credential, ACL or task was changed. Unattended learner
and position-protection continuity remain blocked, while ordinary signed-in PAPER runtime runs.
