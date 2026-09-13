# Omen PAPER runtime — September 10, 21:16 UTC

The price-explained equity reconciliation fix is deployed at clean runtime commit
`5025b21586223d05dfeb4c37c2a514338a25a711` in `C:\Users\leepa\elrond-omen-runtime`.
This supersedes the current pin in the [September 9 historical record](omen-runtime-2026-09-09.md).
The [reconciliation explanation](equity-valuation-reconciliation.md) records the triggering
one-cent difference, classification requirements and conservative limits.

At 21:14 UTC deployment stopped all six application tasks and verified their prior processes
exited before fast-forwarding the checkout and updating the exact commit in their actions.
All seven tasks retain local standard account `erlond`, Password logon, Limited privileges,
AtStartup and timed triggers. Elevated owner inspection verified `erlond` and session 0.
The console, daemon, protection and model tasks are Running; ingestion, research and report
completed with result 0. The model service was not restarted. No OpenConsole process was present.
The operator-authorized credential was supplied through a temporary Windows-encrypted file;
it was consumed and removed, and batch logon was verified before stopping services.

Full Windows local CI on the exact implementation commit passed **2,846 tests, 45 skipped,
one warning in 568.57 seconds**, plus Ruff, Linux/win32 type checks, lockfile and clean-snapshot
checks. Linux execution was **NOT RUN / DOCKER_NOT_FOUND**, so aggregate local CI exited 1.
Independent agent review found no actionable findings and independently passed the 24 new tests.
Subsequent documentation commits do not change the production pin.

Fresh deployed reconciliation at 21:15:12 UTC returned RECONCILED with zero differences and no
unresolved intents; its only readiness reason was the still-engaged switch. At 21:15:23 the
existing CLI clearance path probed authoritative readiness and returned `engaged=false`.
Subsequent doctor and reconciliation checks returned 0: PAPER account verified, risk healthy,
readiness true, no safety reasons, zero differences and no unresolved intents. Config, status,
preflight, ingestion-status and research-status also returned 0. Recovery inventory/startup
reports retain the documented WAL/global-authority refusals described in the prior runtime report.

The specific 20:05 equity-only RECONCILIATION_FAILED incident was resolved at 21:15:52 using the
repository API after checking the original equity diff, deployed commit, fresh clean reconciliation
and successful measured clearance. A resolved INFO audit record links the evidence. The original
HALTED run and original difference remain intact. No cycle, rejected intent or order was replayed;
no forward observation or qualification day was fabricated. Yesterday's successful trading cycle
and the older HTTP 403 ERROR run remain separate history.

The active store is unchanged: `D:\Elrond\state\quantbot-paper-fresh-20260908.db`, schema 27 /
revision 0027. An online backup at
`D:\Elrond\backups\quantbot-paper-equity-valuation-20260910.db` passed `integrity_check`.
At 21:16 UTC the dashboard reports ten open positions, equity 494.55, clean reconciliation and a
clear switch. The latest actual protection evaluation remains HEALTHY at 19:45 UTC, with ten
protected, zero unprotected and zero exits required. The last actual market observation is the
19:46 DEGRADED record from before the fix. The market is closed; these historical observations
are not relabeled by a deployment or readiness probe. The daemons await their normal session.

The September 8 UNHANDLED_OPERATIONAL_EXCEPTION remains unresolved because the original HTTP 403
body is unavailable. IEX single-venue volume capability restrictions remain enforced. Cash,
position, order, fill, drawdown and other risk gates were not loosened; incoherent valuation
evidence still refuses reconciliation.

Local evidence under `D:\Elrond\evidence`: `equity-valuation-deployment.json`,
`equity-valuation-local-ci-summary.json`, `equity-valuation-runtime-audit-before-clear\`,
`equity-valuation-runtime-audit-after-clear\`, `equity-valuation-kill-clear.json`,
`equity-valuation-incident-resolution.json`, and `equity-valuation-probe.json`.
