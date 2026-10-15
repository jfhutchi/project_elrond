# Bounded PAPER position research journal

> **Optional legacy research.** The [PAPER core](paper-core.md) does not require news, SEC research, a thesis journal or model-generated commentary to evaluate entries, protect owned lots or learn from outcomes. This separate journal keeps its original observational authority and point-in-time limits. Missing coverage cannot fabricate evidence or widen core authority. Its retained code and history may be retired only after dependency/ownership review.

This source-only follow-on starts from frozen runtime head 90490a3. It is not
deployed and does not modify the pilot universe, forecast, risk policy, exit
ownership, account, provider configuration, or promotion state.

Existing inputs are AlpacaNewsSource and the SEC filing scouts/providers. News
already has publication/retrieval timestamps, content hashes and explicit health;
SEC sources retain actual filing publication time, never inferred fiscal periods.
Availability in source code is not evidence of present provider entitlement,
configured SEC contact, instrument mapping, freshness or deployed connectivity.

The minimum component adapts news into existing Source provenance, accepts SEC
Source records, rejects future retrieval/publication, stale/unhealthy/derived or
undated evidence, and logs a frozen thesis/forecast ID with edge after modeled
costs. Missing coverage remains a gap. Hard-loss advice takes priority over model
edge. A positive mark alone is not a sell signal. Every record explicitly has no
order authority and is appended idempotently to the existing signal ledger, with
no schema migration. Forecast IDs provide the join to existing outcome evidence;
the journal creates no fills or performance claims.

Next integration must query the actual held instrument, preserve query-symbol
association, configure per-source freshness policy and origin validation, and
record source coverage at every decision. The current IWM/QQQ universe consists
of ETFs: issuer or constituent-company filings must not be silently treated as
fund-level evidence, and this work does not add individual stocks. Missing mapping
is an explicit gap, not a guess.

Before execution use, test the shadow caller against prospective position ticks,
link entry/hold/exit thesis changes and realized outcomes, compare to the frozen
forecast-only baseline after costs, and review any exact code/config binding
change. News text must be treated as untrusted input; recommendations cannot
override protection, kill switch, quote/clock gates or risk limits. No paid feed,
new credentials, automatic model promotion or live route is introduced.

## Connector-to-journal slice

PositionSources now calls the existing AlpacaNewsSource for exactly one held
instrument and, for an explicitly verified equity mapping, SecFilingProvider for
one configured filing form. Company CIKs can be parsed from an already retrieved
SEC ticker artifact with exact payload-hash verification and unique ticker match.
No mapping is guessed for ETFs and no constituent universe is added. The mapping
artifact must be healthy, original and known by query time; an absent SEC provider
or contact remains unavailable rather than being automatically configured.

Each snapshot records provider, exact query, scope, actual completion/retrieval
time, returned/usable counts and gaps. Complete public coverage is always false.
The news adapter's pre-I/O retrieval timestamp is replaced with measured response
completion, without changing publication time. Provider exceptions are reduced to
safe codes, never raw errors or contact/credential text. Bounded source windows
are explicit. Same-day EDGAR Atom filings are excluded because its filing date
does not prove exact intraday availability. Shadow decisions recheck staleness at
decision time and cannot precede actual retrieval. The existing durable signal
journal can retain the snapshot and advice atomically and idempotently without
creating orders or adding a schema.

Run `PYTHONPATH=src python scripts/position_research_shadow_demo.py` for an entirely
offline, explicitly SYNTHETIC ETF/company example. This script has no real provider
requests, broker calls or runtime writes. A separate integration test exercises
the full connector-to-durable-journal flow in an isolated test database and checks
that no order intents are created.

Prospective acceptance still requires current provider availability/entitlement,
an authoritative current equity mapping artifact where applicable, query-symbol
attribution, configured SEC contact if used, freshness/coverage receipts from real
position ticks, outcome linkage and a shadow comparison against the frozen
forecast-only baseline. This slice is not connected to execution or deployed.

## Observational deployment

The pinned `position_research_launch` module and Windows wrapper run independently
with a separate shadow database and lock, outside the learner evidence directory.
Initialization is explicit; ordinary startup cannot create/migrate the store.
The shadow catalog retains official issuer/provider instrument provenance and
dated aliases. It never registers guessed company/constituent CIKs for ETFs.

Only fixed GET requests obtain current PAPER-position membership and bounded news.
No account values, quantities or credentials are recorded. Advice may use only
today's integrity-checked cohort from the exact frozen model and a completed,
eligible producer receipt; stale/future data is rejected. Missing forecasts have
null IDs/edges and produce WAIT, never invented zero returns. No semantic news
signal modifies the frozen forecast. News remains observed context until a separate
prospective experiment validates an interpretation. Existing protection owns all
risk/exit authority, and the original trading services/pins remain unchanged.

Rollout uses existing Limited/Interactive task identity and credentials, a clean
native release and frozen dependencies. No operational database migration or
planned protection pause is needed. Rollback disables only the new research task
and preserves its audit store; it does not rewind trading/learner state. Real
provider receipts are acceptance evidence, not profitability or complete public
coverage. SEC company filings still require an explicitly approved contact sent
in HTTP User-Agent requests to SEC EDGAR; no contact is inferred or transmitted.
