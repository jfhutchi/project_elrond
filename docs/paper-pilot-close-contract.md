# Experimental PAPER positions and daily performance

> **Current PAPER runtime, October 15:** The approved affordable-stock/whole-share entry policy is active on the Omen. Fractions remain preferred; an exact one-share fallback keeps the existing loss budget. The first AAPL round trip is verified; fees/net remain unknown and no learner fit is claimed. See the [current handover](evidence/paper-core/whole-share-2026-10-15/README.md).

> **Legacy pilot contract.** The [PAPER core](paper-core.md) retains one concurrent core lot, 50 bps (0.5%) position value, 25 bps entry risk, a 100 bps stop and 30-minute horizon. Its [approved successor](superpowers/specs/2026-10-14-daily-paper-exploration-design.md), merged in PR #290 and commissioned locally with the submission readiness repair, adds six durable entry reservations per exchange session; that cap is separate from concurrency and includes completed/unknown-send cases. V3 established gross outcomes may train with a fee-only gap, leaving fees unknown and net null. Current-funds entry authority does not certify historical cash. Owned gross/net outcomes, account daily equity and modeled economics remain separate. The older 5% pilot below and inherited-owner protections retain their original contracts; see [handover](evidence/paper-core/handover.md).

The frozen experimental model has no live route and no automatic challenger promotion.
New learner entries retain the tighter of existing controls and 0.25% entry risk,
5% position value, a 1% stop and a 30-minute horizon. At most one new learner
position is held; an independent healthy deadline worker must exist before entry.

An explicitly commissioned inherited-position baseline is account-bound and
included in the frozen execution configuration. Unchanged inherited holdings may
be monitored without counting as a new learner position. They remain in every
aggregate exposure/risk calculation, cannot receive duplicate learner entries,
and retain their existing strategy/protection exit owner. They are not retroactively
called learner trades or given a fresh 30-minute deadline. A changed/closed holding,
post-baseline fill, or learner handoff permanently retires its exemption. Each
forecast-based monitoring record carries its prediction, cost-adjusted remaining
edge, inherited entry provenance and actual exit ownership.

The independent deadline worker and entry runner measure local time after the
broker clock request. Existing freshness and clock limits remain unchanged.
An unsynchronized machine must be repaired rather than having those limits widened.

Daily performance uses two timely, reconciled exchange-close equity snapshots,
with verified external cash flow subtracted. Equity includes realized and unrealized
results and broker costs. The capture worker runs real broker/ledger reconciliation
under the existing operational writer lock only within the close window, including
early closes. It has no broker order-mutating capability. It creates dedicated
immutable performance stores explicitly and never migrates the operational store.

The first valid close seeds a baseline and does not count as a profitable day.
Missing closes, reset epochs, unknown journals or mismatched prior-close marks
remain unavailable/invalid. A retry can finalize previously persisted timely
evidence after a crash; it cannot recapture a late close or rewrite existing evidence.
Historical account marks are not invented or inferred from ordinary operational runs.

The dashboard reads the dedicated stores without creating them. The monthly grid
and cumulative sidebar use the same signed USD measure. Flat days are neutral and
included as non-profitable in the denominator; missing/invalid sessions are excluded.
The forward period and sample count remain visible. No percentage threshold grants
live-trading authority.
