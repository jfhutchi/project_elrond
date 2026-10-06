# Historical replay ranges

The learner dashboard can display verified saved-model replay over the complete
existing training and validation rows. These are separate sections, each starting
at $100. Training is in sample; validation was reused during development. Neither
is untouched holdout or prospective broker evidence. The original reports and
frozen models are preserved; display generation performs no fits, predictions in
the learner store, orders, or promotions.

The offline generator uses `learner.history.build_history_report`, only after
the original source/declaration and canonical protected-range guards. It must
verify the saved vintage with `learner.store.verify_source`; it does not infer
provider authenticity from local hashes. Output is a publish-once envelope at
`evidence/history-v1.json`, with schema 1 and a content-addressed document. Every
view binds its report, model document, dataset and stored input vintage.

Views select all available rows or one decision month. Entire five-session
holding windows are preserved, including exits in the following month. Each
model and SPY uses matching windows and the unchanged next-open/fifth-close
liquidated simulator, five basis points slippage per side, and zero assumed
commissions. Reported costs, notional, net returns and drawdown come from that
simulator rather than clipped/rebased return summaries.

The reader fails closed for corrupt hashes, unknown roles/models/ranges,
unmatched benchmarks, changed costs, nonfinite marks or dates outside a model's
declared training/validation interval. Dashboard GETs only read the saved bundle
and learner metadata. They never create/migrate a database or invoke fitting.

Protected April–September 2026 is excluded. More older data would not
automatically provide out-of-sample evidence for a model trained later. Missing
values remain unknown. Genuine prospective outcomes remain a separate panel;
daily cash-flow-adjusted account P&L remains the calendar's metric. There is no
automatic live switch or promotion. Nonoverlapping windows can still be dependent,
and daily closing marks cannot measure intraday drawdown.
