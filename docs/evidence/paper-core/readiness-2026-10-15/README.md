# Readiness repair â€” October 15, 2026

Core commissioning succeeded. Guardian/outcomes are running and inherited protection was measured healthy. Trader remains disabled while the legacy deadline clock check is unhealthy. Windows Time reported unsynchronized; three independent NTP samples measured the host approximately 1.03 seconds behind. The normal resync returned access denied; the elevated Windows prompt was cancelled. Administrator resynchronization remains required.

The 09:30 Eastern handover failed when its retained-service check sampled a service outside
Running state. Protection logged ValueError at the opening boundary. Regression tests reproduce
the exact failure when a closed clock response supplies next_open at or before local receipt.
The loop now waits five seconds and re-reads that clock without evaluating positions or
granting order authority. The deployment waits at most 60 seconds for each retained service;
changed source pins and disabled services still refuse immediately.

The first renewed activation also exposed the helper's untested `main(argv)` invocation.
The deployed CLI accepts no parameters. The helper now supplies normal process arguments
through sys.argv and restores the original object on every exit. Core source, configuration,
manifest and all acquisition/commissioning rules are unchanged.

## Verification and deployment

- Opening-boundary regression: two cases observed RED with the exact ValueError, then GREEN.
- Protection supervisor/runtime/risk suites: **79 passed**, 14.08 seconds; scoped Ruff passed.
- Four PowerShell gate cases passed: recovering service, timeout, disabled service, changed source.
- Four activation-window boundary cases passed; actual CLI signature and argument restoration
  verified independently. Review confirmed both helper race fixes: expiry is checked immediately
  before commission and role starts, and old-entry task states are refreshed after service waits.
- Independent review found no remaining substantive issue after these corrections.
- Fixed native support checkout is clean and pinned to `ab6f610`; all 46 locked distributions
  were retained. Native protection validation passed. No paid CI was dispatched.
- A selected limited Windows token returned READY_FOR_EXPLICIT_ACTIVATION / exit10 without
  commissioning. Fresh broker acquisition subsequently commissioned unchanged core release0734974b.
- The user's readiness-repair request authorized a new bounded attempt from 10:50 to noon
  Eastern. The earlier expired attempt and refusal remain preserved in their original directory.

The original IWM exit was genuinely filled. The existing PaperDeadlineSupervisor durable sync
validated the original broker/client identity, chronology and quantity and recorded one real
fill. The probe transport allowed only GET requests. An online SQLite backup precedes this
write. No order was cancelled/replaced, no stop cleared, no fees or outcomes fabricated.

Local task definitions, backups, private acquisition documents and full role-health records
remain under D:/Elrond/review/readiness-20261015. Private commission/account documents are
excluded from this package. Text helper copies are retained for review, not executable inputs.
`measured-status.json` is a dated capture, not a promise of future health or daily profitability.

## Remaining release blocker

The bounded readiness phase refused at 11:11 Eastern with
PROTECTION_READINESS_REQUIRED_TRADER_REMAINS_DISABLED. Its latest position assessment
was HEALTHY with eight protected positions; the specific remaining objection is
DEADLINE_SUPERVISOR_UNHEALTHY / DEADLINE_BROKER_CLOCK_STALE. The deployment task is now
disabled, while guardian/outcomes and retained services continue independently.

Run `w32tm /resync /rediscover` from an Administrator terminal, verify synchronization
and a healthy deadline heartbeat, then rerun fresh readiness before enabling trader.
Do not change the computer clock manually, relax the deadline gate or reuse old acquisition
approval. The recorded retry window expires at noon Eastern; any later attempt needs a
new explicitly bounded window and fresh evidence.

Subsequent clock investigation found a fixed 32,768-second polling interval (about 9h6m),
one configured server, and an October12 DNS resolution failure for that server. Prior-boot
operational records show successful synchronization earlier on October15. The 10:48 Eastern
reboot is followed by unsynchronized status. Direct NTP probes currently reach the server.
The existing Windows ForceSynchronizeTime task also denies this token access. These findings
support missed resynchronization/infrequent correction, rather than proving a hardware defect.
