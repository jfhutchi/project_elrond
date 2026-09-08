# Entry buying-power budgeting

The September 8 Omen PAPER cycle proposed almost the full 500 account equity using completed
daily closes. Nine accepted market buys left broker buying power of 49.37; the final DBC intent
was rejected with HTTP 403 and the existing hard-failure path engaged the kill switch. The
response body was not retained, so this does not prove the broker's exact rejection reason.

The inspection did prove a budgeting defect: in-run reservations reduced remaining exposure but
left the same broker buying-power balance available to every new order. Exposure limits can mask
this when cash and equity are equal; they do not replace a funding limit when buying power is
lower than equity. Orders already pending at the broker require different treatment because the
broker has already deducted their commitments from its reported available balance.

The portfolio builder now deducts each newly planned commitment exactly once. Reservations for
existing broker orders explicitly mark their buying power as already reserved. Those orders
still count toward exposure, open risk and position limits. Duplicate reservations do not charge
twice, and exhausted funds produce zero remaining buying power.

The production composition root also supplies `ENTRY_BUYING_POWER_RESERVE_BPS`, a bounded
environment setting with a **project default of 500 basis points (5%)**. This conservatively
retains 5% of the broker-reported available balance before deducting new commitments. It is
applied once to the account snapshot for the whole batch, not repeatedly to each order. For
example, a 500 snapshot with 450 of new planned commitments permits at most 25 more at the sizing
reference price. Existing pending broker commitments are not deducted a second time.

This is an operational reserve chosen to reduce the fragility of spending all available funds
at daily reference prices. It is not an operator-ratified threshold, a backtest cost assumption,
a permanent 5%-of-equity allocation target, or a guarantee against price gaps. Historical strategy
configurations and backtest results are unchanged. New PAPER observations must retain the actual
runtime commit and settings; the changed execution budget is not evidence of improved returns.

The accepted configuration range is 0 through 9,999 basis points, and its effective value is
visible through the normal configuration inventory and safe settings summary. Overrides require
service restart because the running settings are immutable. The current deployment uses the
project default; no credential is needed to select it. No risk gate or kill-switch condition is
removed, no rejected intent is revived, and broker rejection continues to follow the existing
fail-closed path.

Tests cover fresh and broker-reserved commitments, duplicate accounting, overcommitment, the
cash reserve across a complete batch, settings validation, and production composition wiring.
The end-to-end test uses real storage, reconciliation, strategy, risk and execution code with
deterministic network fixtures; test results are not inserted into the operational ledger.

Alpaca documents that open buys consume buying power and that market orders are checked against
current market prices, which may differ from completed daily closes:
[Placing Orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca).
