# Prepared Alpaca support request — not sent

Official channel: https://alpaca.markets/support/request

Subject: Recover original PAPER BTC/USD submission disposition, November 8 23:05 UTC

Please investigate the existing paper account associated with this support request. This concerns PAPER trading only at https://paper-api.alpaca.markets, not a live account. No new order or account reset is requested.

Client order ID: `qb-fa5f1cabf5f085faa2b6360cba367aa0e07daabb`

Intended order: BUY BTC/USD, quantity `0.000032817`, MARKET, GTC; no limit/stop price.

The intent was created at 2026-11-08T23:05:04.720194Z. The application's final send authorization was recorded at 2026-11-08T23:05:52.496300Z. Please search approximately 23:04–23:07 UTC. Authorization is not proof that a POST reached Alpaca.

The application retained a REJECTED state but discarded the original request-level HTTP response and X-Request-ID. A contemporaneous cycle log at 23:05:56.311994Z records "Alpaca Paper request failed with HTTP 403"; that cycle included two local dispatch requests, so we cannot independently attribute that HTTP status to this exact client or endpoint. We do not know the original error code or reason.

Please retrieve any retained original POST /v2/orders request and its request ID, HTTP status, provider error code/message and timestamp. Please explicitly confirm whether an order was created, accepted, routed, partially/fully filled, or rejected before creation. If an order exists, please provide its order ID and authoritative lifecycle/fill records. If the original request record is unavailable, please distinguish that from definitive confirmation of rejection/no order creation.

At 2026-11-09T16:38:03Z, read-only authenticated checks of the expected paper account returned 334 all-status orders (below the 500 limit), zero crypto orders, zero crypto positions and 14 recent non-crypto fill activities. A prior exact client lookup returned 404. We have not treated absence/404 as settlement proof or replayed the old submission. Entries remain fenced pending reliable reconciliation.

Please use the account identification attached to this authenticated support request. We can supply the paper account UUID through your private support channel if needed. API keys/secrets will not be supplied.

## Why this route

[Alpaca's Trading API documentation](https://docs.alpaca.markets/us/docs/getting-started-with-trading-api) says X-Request-ID identifies internal calls and cannot be queried from another endpoint. [An Alpaca staff explanation](https://forum.alpaca.markets/t/how-can-i-find-why-an-order-was-rejected/18537) confirms staff can inspect internal rejection logs; it does not guarantee retention or recovery of this specific request. No documented retail request-history API was found. This message requires the operator's explicit authorization before sending.
