# Fresh BTC reconciliation assessment — November9, 18:57–18:59 EST

**Result: current broker inventory is flat; the historical BTC request remains unresolved. No settlement or resume was performed.** This is a fresh evidence and supported-interface assessment, not a conclusion drawn from repeating the same404.

The case-specific mailbox search at18:56EST covered all matching conversations, including potential separately threaded replies. It still contained one conversation with the two already-retained automatic acknowledgements for case392277. No substantive Alpaca disposition was available. Active monitoring in this task continues; no recurring Automation, polling daemon or service was created. Actual message IDs and last-checked times are retained in the private monitor-state.json. Unchanged acknowledgements are not counted as new evidence.

## Independent authenticated broker capture

Six finite GETs at18:57:28–18:57:29EST verified the expected PAPER account. Raw response bytes were hashed, and complete parsed responses were captured privately with method/path/query, actual receipt times and available X-Request-ID; credentials were not retained. The capture did not call a submission/cancellation endpoint or write to any ledger.

| Observation | Actual result |
| --- | --- |
| All-status orders since2000 |334, below500 limit; complete returned history |
| Exact original client matches in that history |0 |
| Orders submitted in/after original request window |0 |
| Explicit crypto orders / unfiltered crypto orders |0 /0 |
| Exact original client lookup |HTTP404, provider code40410000 |
| All open positions / crypto positions |0 /0 |
| All account activities sinceNov6 UTC |16, complete below100 page limit |
| Activity types |14FILL,2FEE; no crypto activities |
| Broker account / crypto status |ACTIVE /ACTIVE |
| Broker trading_blocked / account_blocked |false /false |

The two fee rows are aggregate Nov6 account charges without an order ID or symbol. They were preserved as actual broker evidence; they were not allocated to individual trades, reclassified as BTC costs, or used to fabricate a terminal event. This refresh does not establish per-trade fee completeness.

The documented [order query](https://docs.alpaca.markets/us/reference/getallorders-1), [client lookup](https://docs.alpaca.markets/us/reference/getorderbyclientorderid) and [activity pagination](https://docs.alpaca.markets/us/reference/getaccountactivities-2) were checked. Those endpoints return order/activity records; current absence is not the discarded original submission response.

## Canonical state and actual supported interfaces

Read-only canonical inspection at18:59EST still finds the original BTC intent REJECTED, no BrokerOrder and only CORE_LOCKED_SEND_AUTHORIZED. The existing read-only resume preflight still returns ACTIVATION_UNRESOLVED_CLAIM. All403 executable pins verify, Guardian/Outcomes heartbeats are fresh, manual stop is not engaged, Trader task is disabled and Trader process count is0. No claim, account lease, authority, stop, economic record or configuration was altered.

A single bounded read-only agent reviewed the existing reconciliation paths and completed. Root independently read the critical implementations:

- activation.py:352 `_boundary` requires genuine definitive pre-send evidence or an actual BrokerOrder with canonical terminal evidence. A status label alone cannot close the claim.
- settlement.py:21 only admits the exact local-before-I/O refusal contract for rejected no-send intents. The original BTC event does not satisfy it.
- outcomes.py:968 `recover_intents` and AlpacaCoreBroker.owned_events can import a genuinely recovered original order and its complete terminal/fill evidence. Missing lookup preserves the claim rather than inventing an order.
- contracts.py:768 BrokerEvent accepts one real order/fill/cost/cash payload. It has no no-order support-attestation payload; a cash or fee row cannot impersonate one.
- authority.py:613–654 independently requires current inventory/funds/Guardian/time readiness and then calls the historical boundary. Flatness does not replace it. No CLI exposes an alternate no-order settlement override.

**If Alpaca retrieves an actual order:** the existing recover_intents → owned_events → import_broker_events path can recover it without the original HTTP bytes, provided exact account/client/order identity, matching intent, stable order observations, complete fill coverage and watermark all validate.

**If Alpaca definitively attests rejection before order creation:** that can provide the missing independent historical evidence, but no existing consumer admits such a no-order disposition. A minimal audited receipt/import contract must then be implemented and tested against that genuine evidence. It must preserve the original REJECTED claim and consumed attempt history, distinguish settled ownership from pre-send refusal, and reject unrelated, unavailable-log, ambiguous,404-only and contradictory evidence. No fake BrokerOrder or direct database edit is acceptable.

The precise remaining blocker is therefore the absence of either an authoritative original order/lifecycle record or an exact account/client/request-window rejection-before-creation attestation. There is no honest existing guarded command that can close this legacy claim using the current flat readings alone. Adding an override now would change the evidence requirement rather than reconcile it. No speculative source change or deployment was made in this assessment; previous repair tests remain as recorded, and no new whole-suite test claim is made.

Private captures: D:/Elrond/deploy/runtime-recovery-20261109/alpaca-case-392277/fresh-broker-summary.json, broker-private-20261109T235728Z.json, fresh-local-assessment.json and fresh-resume-preflight.json. Existing source fixes remain deployed. Conditional supported reconciliation/resume is already authorized when sufficient evidence arrives.
