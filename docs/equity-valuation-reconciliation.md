# Equity changes across broker observations

At 20:05 UTC September 10, the Omen PAPER cycle halted and engaged the kill switch because
stored account equity was 494.18 and the following broker read reported 494.19. Its only
reconciliation difference was equity. Cash, positions, open orders and fills had no recorded
differences. The same cent-level valuation movement had produced intermittent session warnings.

Equity includes marked holdings. Comparing two separate observations as though equity were a
fixed ledger balance confuses price movement with a ledger discrepancy. Alpaca documents equity
as cash plus signed long and short market values in its
[account reference](https://docs.alpaca.markets/us/docs/account-plans).

The comparator now classifies an equity-only difference as informational valuation evidence
when all of the following hold:

- Every existing material identity, currency, cash, buying-power, quantity, order and fill check
  agrees. Cash must also match exactly for this classification.
- Both portfolios contain the same nonzero quantities and entry prices. Signed market values
  agree with quantity times price within the existing currency reporting quantum.
- Each snapshot's equity agrees with cash plus signed position market values within that same
  quantum, and the change in position value explains the equity change within it.
- The marked position value actually changed. A standalone equity change with unchanged marks
  remains an unexplained discrepancy.

Every bound is strictly less than the existing quantum, 0.01 at the default two decimal places.
No dollar or percentage allowance is added. Signed short values are handled explicitly. The
original equity values, cash, market-value totals, residuals and reporting bound are retained in
the existing reconciliation summary under `valuation_changes`; no database migration is needed.
Material `diffs` retain their meaning and still prevent new orders. Existing drawdown, entry risk,
position protection, broker restrictions and kill-switch clearance requirements are unchanged.

Account and position endpoints are not an atomic snapshot. If the observations are internally
inconsistent, this conservative rule can still refuse reconciliation. It does not label every
small difference harmless or increase the precision allowance to hide timing uncertainty.

Failed equity-only comparisons also retain both observed cash amounts, position market-value
totals, equity residuals, position counts and inconsistent-mark counts inside the existing
material `ACCOUNT/equity` difference. These are diagnostic evidence, not an exemption or a
substitute portfolio. The original scalar equity values remain recorded. The status stays
`RECONCILIATION_REQUIRED` and new orders stay blocked. No schema change is required.
This permits later diagnosis without guessing from a scalar equity difference after the second
portfolio has gone out of scope. Existing older receipts are not rewritten or backfilled.

Regression tests exercise a real recovery and operational cycle: an explained one-cent mark
completes without engaging the switch, while a one-cent cash discrepancy or unexplained equity
change still halts and engages it. Other cases cover positive/negative marks, shorts, missing
orders/fills, invalid valuations, entry-price/quantity changes and rounding boundaries.

A read-only PAPER probe at 21:01:59 UTC compared the retained 494.18 snapshot to a fresh 494.54
broker observation. Cash remained 2.05; signed position values moved from 492.133166 to
492.489683. The unexplained delta was 0.003483, below 0.01. All ten positions, eleven fills and
other ledger fields reconciled. This was only a pure comparison of broker GET responses; it
submitted no order and did not clear the switch or alter the original HALTED run.
Local probe evidence: `D:\Elrond\evidence\equity-valuation-probe.json`.
