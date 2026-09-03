# Ingestion control: cursors, run history, incidents and heartbeats

**Issue:** #198 · **Backend:** SQLite (schema 25) · **Status:** implemented, wired to
`MarketDataSync`

---

## The problem this exists for

Missing data is the quietest way this project loses future research capacity.

`MarketDataSync.sync` refetched a fixed session window every cycle and stored it idempotently.
That is restart-safe by accident — it survives a restart because it never remembers anything —
and it answers none of the questions an operator actually has:

- When did this source last succeed?
- Is it stopped, paused, or broken?
- Did last night's run cover its whole range, or die halfway?
- Is that gap in the data a holiday, or three weeks of nothing?

None of those had an answer. A collector silently stopped for three weeks looked exactly like
one that ran ten minutes ago, and by the time a hypothesis needed that window, the provider no
longer served it. The loss is not detected when it happens; it is detected when the research it
would have supported becomes impossible.

## The relationship to the trading kill switch

**They are independent, deliberately, and the independence is enforced rather than intended.**

An engaged kill switch halts order submission. It says nothing about whether market data may be
collected — and stopping collection because trading is halted would destroy exactly the
observations needed to understand why it halted. The worst version of that bug is silent: a
halt on Monday, no data for the rest of the week, and a post-mortem with no post-mortem data.

Nothing in `quantbot.ingestion` imports or references kill-switch machinery.
`test_ingestion_never_consults_the_trading_kill_switch` parses each module's AST and asserts no
reference to `kill_switch`, `kill_switch_state`, `KillSwitch` or `KillSwitchState`, then runs the
same detector over `operations/kill_switch.py` to prove the detector is capable of finding one.

The converse also holds: nothing in ingestion can *clear* a kill switch, engage one, or affect
readiness. Collection state is not a trading permission.

## What is durable

Five tables, all additive in migration `0025`, all operational state and none of it research
evidence. Nothing here may appear in an experiment manifest.

| Table | Holds |
|---|---|
| `ingestion_sources` | one row per configured collector: provider, kind, configuration hash, enabled |
| `ingestion_cursors` | one mutable checkpoint per exact stream, with an optimistic `version` |
| `ingestion_runs` | append-only attempt history: mode, status, requested range, record counts |
| `ingestion_events` | append-only operational facts; incidents stay open until recovery is observed |
| `component_heartbeats` | append-only health samples, carrying the instance that wrote them |

Every column type is deliberately boring — `String`, `Integer`, `Boolean`, `Text` — and every
constraint is a plain `CHECK` or foreign key. #197 has to prove SQLite/PostgreSQL parity for this
schema, and a partial index or dialect-specific type added now would be a portability problem
discovered later rather than avoided now.

## The four invariants, and how each is enforced

These are enforced by shape rather than by discipline, because a rule a caller has to remember is
a rule that gets forgotten at 2am.

### 1. A cursor cannot advance past data that was not committed

`IngestionControlPlane` does not own a transaction. It reads and writes through the session the
caller is already using to store observations, so the checkpoint and the rows it points past
commit together or roll back together. There is no window in which one exists without the other.

A cursor that survived a crash its observations did not would be worse than no cursor: the next
run starts *after* data nobody stored, and the gap is permanent and invisible.

### 2. A failed run cannot checkpoint

`fail_run` and `abandon_stale_runs` take no `CursorAdvance`. There is no public `advance_cursor`
either — a checkpoint is a consequence of a run that succeeded, not an independent operation. A
partial batch that could checkpoint would skip whatever it never reached.

### 3. A backfill cannot make a stopped feed look current

`CursorAdvance` has no `last_success_at` field. The control plane derives that field from the
run's mode inside `complete_run`, and only a `LIVE` run writes it. So a source whose only
successful runs are backfills reports `NEVER_COLLECTED` — which is true — however many rows those
backfills stored, and `last_observation_at` still moves, because the backfill really did observe
those bars.

This is the mechanical form of "historical catch-up cannot masquerade as forward evidence".

### 4. A stale checkpoint is refused, not merged

`CursorAdvance.expected_version` must match the stored version, or `CursorConflict` is raised.
Two collectors racing on one stream is exactly where a cursor silently goes backwards, and the
window between the two positions is skipped forever without anything noticing.

## The five states, and why a boolean will not do

```
DISABLED          somebody turned it off, on purpose, and said why
NEVER_COLLECTED   registered, never succeeded; no position to resume from
HEALTHY           enabled, and succeeded after the last expected window
STALE             succeeded once, and not since something was due
FAILED            the most recent run failed or was abandoned, with nothing since
```

A single `healthy: bool` has to pick one of these and is wrong about the other four. The recovery
for each is different: a disabled source needs a decision, a never-collected one needs a first
run, a stale one needs a gap calculated, and a failed one needs the provider looked at.

**Market-closed is not a state here.** `expected_success_after=None` means nothing was due, which
is what a holiday is, and it resolves to `HEALTHY`. The calendar question stays with the caller
on purpose: deciding when a collection was owed is a market-session question, and an ingestion
module that owned one would need to be told about half-days and early closes to avoid crying wolf
every long weekend.

`quantbot ingestion-status` supplies a default when the caller has no calendar to hand:
`DEFAULT_STALE_AFTER_HOURS = 100`, taken from `scripts/supervisor.py::STALE_RUN_HOURS`. That is
an existing deployed value — CLAUDE.md's first tiebreak for an unspecified threshold — and it was
chosen there for a reason that applies unchanged: consecutive daily cycles are 24h apart midweek,
72h across a weekend and 96h when a Monday holiday intervenes, so a tighter bound cries wolf every
long weekend and trains an operator to ignore the warning.

## Incidents resolve on observation, never on silence

A stale or failed source opens **one** incident and keeps it open. Repeated observation does not
stack duplicates: an operator scrolling a wall of identical alerts learns less than one who sees a
single alert that has been open for three weeks.

An incident closes only when something observes recovery — a successful run followed by a status
check. A collector that stopped emitting cannot close its own incident by going quiet.

`status(..., record_incident=True)` is opt-in and needs the observation time, because merely
asking a question should not write history. `quantbot ingestion-status` never passes it.

## Operator surface

```bash
quantbot ingestion-status
```

Read-only by construction: it opens no run, moves no cursor and records no incident. A status
command that wrote history would be the worst thing to run while trying to work out what happened
to collection. The test asserts this by comparing the whole control-plane state before and after.

For each source it reports every stream's health, last success, last observation, hours since
success, and reason; the latest run with its full record counts; and every open incident. A
registered source with no cursor is reported as `NEVER_COLLECTED` rather than omitted — a
per-stream loop that found nothing to iterate would report "nothing has ever worked" as silence,
which is the failure this whole issue is about.

Zero sources is reported as zero sources, with a note saying so. That was the state the
coordinator was found in during #190, and it is not health.

## What is not here

- **The retry loop.** This plane records attempts; it does not schedule them. Bounding retries
  belongs to whatever drives collection (#143). What is guaranteed is that a provider failing all
  afternoon is *visible*: every attempt is a countable row, the source does not drift back to
  healthy because the last attempt was quiet, and one outage is one incident.
- **Backfill orchestration.** `RunMode.BACKFILL` exists and is enforced; calculating a missing
  interval from durable data and driving the catch-up is separate work, and it depends on an
  authoritative store existing (#190).
- **A retention policy for heartbeats.** If the volume becomes material, a bounded policy may be
  added **for heartbeat rows only**. It must never delete market observations or research
  evidence.
