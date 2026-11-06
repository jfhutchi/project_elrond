# Market coverage and overnight operating proposal

November 6, 2026, America/New_York. Research and planning only. No account permissions were queried or changed, and no new assets, leverage, strategies or numerical risk limits were activated.

**Priority update, November 6:** the user has selected crypto spot implementation as the next market extension. Direct international equities, FX and options are **DEFERRED**; their collected research is retained. The offline candidate batch is a research queue, not a prerequisite that postpones crypto integration. This dated document does not report subsequent implementation or commissioning status.

## Separate economic exposure from trading hours

US-listed ETFs can provide index, overseas-equity, commodity or currency-related exposure during their own trading sessions. They are not interchangeable with a cash index, overseas ordinary share, spot currency or futures contract. An index itself is a calculation; trading its ETF or derivative changes costs, session hours and tracking behavior. Overseas exposure through a US ETF does not create overnight exchange access.

| Market | Elrond today | Proposed next research work |
|---|---|---|
| US equities and ETFs | Current paper execution scope; daily backtest and intraday exploration are separate | Test the catalog's first batch, with point-in-time liquidity/universe filters and proper market calendars. |
| Crypto spot | At the research snapshot, data adapter/offline capability exists; stock eligibility and learning calendar still need integration | Current implementation priority: verify account-specific eligibility, 24/7 boundaries, costs and fault behavior. Any transferred strategy requires independent crypto evaluation. |
| International equities | No verified direct execution integration | **DEFERRED.** Retain venue-data, currency-accounting, local-lot, settlement and holiday research for later review. |
| Spot FX | No verified execution/accounting integration | **DEFERRED.** Retain bid/ask and financing requirements. An FX ETF is a separate instrument, not executable spot returns. |
| Options | Outside the current implementation scope | **DEFERRED.** No options strategy, permissions, exercise/assignment or accounting work is activated by this catalog. |
| Futures, crypto derivatives and multi-leg carry | Not supported by the active paper path | Defer execution work until contract specifications, roll/margin/collateral, shorting, liquidation and leg-risk simulation are justified and separately authorized. |

Alpaca documents 24/7 crypto spot trading and crypto paper simulation, while prohibiting crypto margin and short sales. Account eligibility remains unverified. Its published lowest-volume tier lists 15 bps maker and 25 bps taker fees per fill: two taker fills are approximately 50 bps of round-trip cost before spread or slippage. Fees can be charged in the received asset and reported later; neither immediate absence nor a zero USD `net_amount` establishes zero cost. Rates and applicable account terms must be rechecked before tests are frozen. [Crypto specification](https://docs.alpaca.markets/us/docs/crypto-trading), [paper support](https://docs.alpaca.markets/us/docs/paper-trading).

Interactive Brokers is a researched option for broader market access, not an installed Elrond integration or a recommendation to open/fund an account. Its paper permissions/data depend on account configuration, and the simulator has top-of-book and order-type limitations. A separate adapter, market-data entitlement check and accounting/safety tests would be required. [IBKR paper-account guide](https://www.ibkrguides.com/orgportal/aboutpapertradingaccount.htm).

Current ordering: crypto spot implementation first; retain US-equity/ETF candidate research as a separate offline queue. International equities, FX and options remain deferred. More hours of observation do not require continuous exposure or a trade every hour.

## Sleep interval: 22:00–06:00 America/New_York

The user requested more conservative behavior during this local interval. Use an IANA timezone conversion from an aware UTC clock, never a fixed UTC offset. The quiet interval spans midnight and changes UTC length around daylight-saving transitions. Test spring-forward, repeated fall-back hours, restart during the interval and host timezone changes. Exchange sessions remain in each exchange's own timezone; the user's sleep schedule is a separate policy.

The following are **dated research scenarios**, not a statement of current settings or later user authorization. They do not override subsequent accepted implementation defaults:

| Policy | New entry exposure | Concurrent positions | New-entry loss budget | Purpose |
|---|---:|---|---:|---|
| Observe-only overnight | 0 | No new positions | 0 | Initial commissioning option while validating overnight operations; preserve protection of inherited/open positions. |
| Conservative overnight | At most 50% of the separately approved daytime entry cap | Lower cap, explicitly chosen before activation | At most 50% of the approved daytime entry-loss budget | Candidate for comparison once data, costs and recovery behavior are proven. |

The 50% ratios originate as research proposals in this document; use the separately recorded accepted settings for implementation. Fractional/lot minimums may make smaller orders infeasible; skip rather than round up past a cap. No leverage or new asset permission is implied. This catalog does not establish the current acceptance or implementation status of concurrent-position counts, portfolio exposure, loss thresholds or entry pauses.

Existing positions keep their planned protection, exits, reconciliation and emergency-stop behavior. Reducing the allowance for **new** exposure must not disable exits or silently force liquidation at 22:00. A proposal may require an orderly exposure reduction, but its timing/rules need explicit approval rather than a hidden market sell. Resetting counters at midnight must not forgive prior losses or outstanding commitments.

Actions that require human approval wait until the user is awake. Record and deduplicate pending requests; revalidate their market/state context after 06:00. Elapsed time is not approval, and previously valid trade signals do not remain executable overnight. Autonomous protective management already within approved rules continues. Notify immediately only for actionable failures that existing recovery cannot handle, without turning approval requests into notification storms.

## Required evidence before any broader rollout

1. Offline strategy and cost evidence from the evaluation plan, including an overnight subset and stressed spread/liquidity conditions.
2. Symbol, precision, minimum order, session, asset-denominated fee and currency reconciliation tests for the proposed market.
3. Paper-only credentials/endpoints, idempotent orders, bounded requests, restart recovery and protection under outages; a separate calendar for crypto rather than bypassing equity checks.
4. One authoritative order owner and ledger per approved account topology; dashboard access from the management workstation does not start another engine.
5. A proposed configuration with exact daytime and sleep limits, a rollback/stop procedure and explicit activation authorization. Shadow observations precede simulated orders.

This document changes none of the running system. The research snapshot records overnight conservatism as a product requirement; current enforcement must be established from the separate implementation and commissioning evidence.
