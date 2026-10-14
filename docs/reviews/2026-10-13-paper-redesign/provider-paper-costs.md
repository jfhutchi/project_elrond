# Supplemental provider cost evidence

Checked October 14, 2026 during isolated implementation preparation. This is public documentation, not an authenticated account/API observation or commissioning receipt.

[Alpaca's current Paper Trading documentation](https://docs.alpaca.markets/us/docs/paper-trading) explicitly lists regulatory fees among effects its paper simulation omits. It also documents omitted latency slippage, queue position, market impact, price improvement and dividends; its comparison table marks paper borrow fees unavailable. The page displayed an update age of three months when checked.

Inference for M6: this supports investigating a versioned, component-specific documented-zero contract for applicable PAPER regulatory fees. It does not prove every charge is absent, establish account/endpoint applicability, make unknown historical fee zeros verified, or establish complete current cash activity. Keep modeled economic costs separate from provider-PAPER results and retain late conflicting provider evidence as a revision.

Before commissioning, bind any supported zero contract to the exact provider/account/endpoint, asset and transaction scope, effective period, documentation identity and cost component. Verify the existing supported adapter's read-only evidence and account applicability. If that evidence conflicts with the documented simulation behavior, retain the conflict and reconcile it; do not force zero or replace missing sources with a balancing cash residual.

This supplements the earlier generic account-activity/regulatory-fee documentation. It narrows a potential PAPER cost blocker without changing the approved target, fee provenance rules, model authority or runtime.
