# Current direction: intraday PAPER research and learning

This is the current operator-approved direction as of October 6, 2026. Dated
deployment reports, old strategy results and frozen objective files remain
historical evidence. This document supersedes their descriptions of the current
goal; it does not rewrite their results, grants or numeric controls.

## Goal and present capability

Build a recurring research → eligible PAPER decision → protected PAPER trade →
reconciled outcome → bounded adaptation → later fresh evaluation loop. Multiple
eligible opportunities within a market session are the goal, rather than an
annual trade-count target. Opportunities are not an order quota: WAIT decisions,
coverage failures and risk vetoes are meaningful evidence and must be retained.

The new adaptive experiment is currently in its **SHADOW observation stage**.
It records point-in-time five-minute features, immutable forecasts from the
existing constant baseline and ridge, research/thesis coverage, and later
30-minute market-return labels. It has no model activation or order authority.
The original PAPER workers, protection and exit owners retain their separately
commissioned identities; SHADOW does not take over existing positions.

The model pair is an experimental comparison, not a demonstrated profitable
strategy. Seen historical prediction error cannot establish fresh economic edge.
Do not activate a weak candidate to manufacture trades, lower the fixed cost
floor, consume further fit calls before the registered successor conditions, or
recycle protected data. Source execution/adaptation integration tests use
synthetic fixtures; they are not real fills or prospective profitability.

## What the old “12 trades a year” wording meant

The exact legacy wording is the comment **“12 round trips a year”** immediately
above **max_annual_turnover: 12** in
[economic-objective-v1.yaml](../config/economic-objective-v1.yaml). It was marked
PROPOSED and explicitly described as an agent's choice, not an operator rule.

The field is validated as a nonnegative integer and included in the frozen
economic-objective identity in
[research/mandate.py](../src/quantbot/research/mandate.py). Current source does
**not** read it as an execution limit, count twelve trades, or refuse a thirteenth
trade. There is no implemented annual twelve-trade ceiling. The comment also
does not provide a code-defined conversion between turnover and trade count:
traded notional, position size, one-way legs and round trips are different units.

The original v1 file remains unchanged, including its comment, because existing
evidence names that frozen objective. A future economic-objective change must
be a prospective successor version, not a revision of earlier results. This
clarification does not remove any actual cost, turnover, risk or promotion check
elsewhere in the system.

## Required real acceptance

1. A natural eligible session supplies real provider/calendar receipts and a
   complete preregistered cohort, with actual availability timestamps and hashes.
   Missing ETF/company/filing coverage remains unknown; fund news is not proof of
   constituent-company research. IEX SINGLE_VENUE volume cannot support an
   all-market volume anomaly claim.
2. Both frozen artifacts log forecasts before their target time; mature genuine
   labels are joined to the same instrument, cutoff and horizon. Report matched
   samples, missing labels, cost-floor decisions and correlated-session limits.
3. An exact, eligible artifact is explicitly commissioned for bounded PAPER
   authority. Existing clock, account, broker, kill-switch, holdout, ownership,
   exposure and risk gates remain in force. Earlier general consent is not a
   fabricated activation record.
4. A real eligible intent produces an attributable PAPER fill. Independent
   protection and deadline handling retain the position's exit ownership, and
   entry/hold/exit thesis, research provenance, vetoes and reasons are durable.
5. The actual protected exit, fills, broker costs and reconciliation establish
   the trade outcome. A 30-minute market label is not broker P&L. A hypothesis or
   news recommendation cannot override a hard loss limit or hold a loser merely
   hoping for a rebound.
6. A successor meets the preregistered mature-evidence/trigger conditions, pays
   its permanent fit reservation, preserves lineage and failed/unknown attempts,
   and faces later fresh evaluation before gaining any authority. No automatic
   model promotion or live-money transition follows.

The bounded new-entry policy remains at most one new position, at most 5% position
value, 0.25% entry risk, a 1% stop and a 30-minute holding horizon, retaining tighter
existing controls and the fixed 10bp round-trip floor. Grandfathered PAPER
holdings keep their auditable baseline and original exit owner; their existence
does not authorize liquidation, account resets or unlimited new entries.

## Performance and dashboard truth

The requested 70–80% profitable-day rate is aspirational, not a prerequisite for
every experimental PAPER observation and not a profitability promise. Evaluate
daily reconciled close-to-close equity change, including realized/unrealized
marks and broker costs, less verified external cash flow, within the same account
epoch. Missing closes and reset-distorted comparisons are excluded rather than
invented as zeros. Flat days count in the denominator of all valid completed
observed trading days; a win/loss-only ratio must be separately labeled.

Report net P&L/returns, sample size and period, flat/losing/profitable counts,
drawdown, expectancy and coverage alongside the percentage. The month grid uses
signed green +USD, red −USD, neutral zero and explicit pending/missing/invalid
states, with authoritative exchange days and early closes. Process liveness,
stored forecasts, market labels and real trading outcomes remain distinct.

See [the staged runbook](adaptive-paper-runbook.md),
[acceptance history and limits](adaptive-paper-acceptance.md),
[dashboard readers](learner-dashboard.md) and
[safe dashboard access](dashboard-access.md). Verify live pins and freshness
through local runtime receipts; a merged checkout is not deployment evidence.
