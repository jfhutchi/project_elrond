# Readiness repair — October 15, 2026

Guardian, outcomes and trader passed two advancing useful-progress polls, with independent processes. Inherited protection was measured healthy before trader was started.

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

## Final activation capture — 11:21 Eastern

The deadline heartbeat recovered after the operator's time-service attempt. Following the
operator's request to get the trader running, fresh checks passed with no override or removed
time gate. Activation completed at 11:21:55 Eastern. Guardian, outcomes and trader each
showed advancing useful progress in distinct processes. The one-time deployment task disabled
itself; role login/recovery triggers remain installed. Earlier clock/refusal files are retained
as dated history and are superseded by activation-complete.json and measured-status.json.

The clock service's long-term reliability was not changed or certified by this activation.
Provider interruptions can still produce scoped entry waits; running roles do not promise a
fill or profit. Daily learning continues under the existing ten-session/all-history policy.
