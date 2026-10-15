# Prospective PAPER close continuity

> **Current PAPER runtime, October 15:** The approved affordable-stock/whole-share entry policy is active on the Omen. Fractions remain preferred; an exact one-share fallback keeps the existing loss budget. The first AAPL round trip is verified; fees/net remain unknown and no learner fit is claimed. See the [current handover](evidence/paper-core/whole-share-2026-10-15/README.md).

> **Account reporting scope.** This optional close-to-close account evidence remains distinct from [PAPER core](paper-core.md) owned-lot outcomes. V2 requires fee-complete net labels. The [approved V3 successor](superpowers/specs/2026-10-14-daily-paper-exploration-design.md), merged in PR #290 and commissioned locally with the submission readiness repair, admits established gross outcomes from closed, owned, reconciled lots when unavailable fees are their sole incompleteness; fees remain unknown and net remains null. Account-equity movements never substitute for owned labels. Current broker funds used for bounded sizing do not certify historical cash or actual net profit. Account reporting includes inherited/unrealized exposure; it cannot establish lot cost completeness or model activation. Rollout is recorded in [handover](evidence/paper-core/handover.md).

The optional `--broker-close-reference` capture mode uses Alpaca's authenticated
PAPER `GET /v2/account/portfolio/history`. Each previous/current exchange close
gets a separate two-minute query window, from one minute before to one minute
after close, with `timeframe=1Min`, `intraday_reporting=market_hours` and
`cashflow_types=ALL`. The source is documented at
[Alpaca portfolio history](https://docs.alpaca.markets/us/reference/getaccountportfoliohistory-1).
Its reported P/L and percentage arrays are not used as our performance metric.
Daily history labels are not guaranteed to identify the exact exchange-close
instant. We therefore retain both intraday query parameters and untouched raw
responses with individual hashes inside an immutable composite source document.
Only the exact close timestamp is selected. An absent current close rejects the
capture; an absent previous close can only seed a baseline, never a comparison.
Previously retained exact-close daily receipts remain readable without relabeling.

Daily account performance remains exact current-close equity minus the immediately
preceding close equity minus classified external cash flow. Equity includes marked
positions and broker costs. Deposits/withdrawals are not profit; missing and invalid
days are excluded, and flat valid days remain in the profitable-day denominator.

## Required proof

- The existing pinned PAPER account and creation/store epoch must be verified.
- A completed, diff-free reconciliation and matching durable live account snapshot
  must fall inside the close window and remain fresh. Before/after account reads
  must agree. Any activity between close and snapshot invalidates the comparison;
  this avoids claiming that later economic changes were reconciled at close.
- History must contain a unique finite equity point at the exact authoritative
  exchange close. No nearest timestamp, day-label reinterpretation or interpolation
  is allowed. Early close times come from the same authoritative session calendar.
- The immediately previous session needs a retained canonical reference matching
  its immutable baseline. Its equity must agree exactly with both the new history
  response's prior-close point and the broker account's `last_equity`.
- Activity queries use previous exchange close through current exchange close,
  not the later receipt timestamps. Complete pagination and the existing strict
  activity classifier remain required. Unknown journals, duplicate IDs and ambiguous
  funding do not pass. Retained activity JSON/hash permits classification and net
  flow to be recomputed during reference validation.
- All provider requests must finish within the existing five-minute close window;
  reconciliation freshness is checked again before retaining/finalizing evidence.

`close-references/<close-evidence-hash>.json` binds the raw history response/hash,
request bounds, account epoch, exact evidence hash, reconciliation ID/timing/snapshot
hash, pinned-account verification and activity inputs/hash. Files are created
exclusively; an incompatible retry raises instead of overwriting. The existing
validation sidecars and daily ledger are reused; no operational migration or new
accounting table is needed.

## Bootstrap and compatibility

A verified current close can seed a canonical baseline without a profit comparison,
even when prior history is absent. A later session still requires the exact prior
point and all continuity/flow gates. A legacy baseline without this reference cannot
bridge into a broker-close profit comparison. Existing invalid/unfinalized historical
captures are neither fetched again nor rewritten. Missing history is never invented.

The flag is off by default. An immutable `close-reference-mode.json` marker prevents
mode downgrade even when reference files are lost. After canonical mode is recorded,
new captures refuse an implicit return to live after-close marks. Finalization
recovery in opt-in mode requires the retained matching reference; no new provider
read or historical recapture substitutes for a lost proof.

Source tests use synthetic account/history/activity fixtures. They establish code
behavior, not actual provider completeness, runtime health or profitability.
Prospective acceptance still needs a separately approved capture rollout, a real
timely canonical seed and a later real reconciled comparison, plus actual provider
confirmation of exact-close intraday points and close-boundary activity filtering.
If those supported responses are unavailable or incompatible, the mode fails closed.

No deployment, task/security change, credential expansion, stop clearance, increased
assessment cadence, orders, promotion or historical accounting repair is authorized
by this source work. Capture rollout approval remains separate; frozen worker hash
review is not part of this feature.

An approved rollout would update only the existing capture launcher/source and
explicit flag, retaining its schedule/security/credential reference. Back up its
launcher and performance stores without copying credentials. If rolled back after
canonical receipts exist, pause that accounting writer rather than resume an older
writer unaware of the new mark basis. Preserve all new/old ledger and reference
files; restoring an older database over later evidence is not a rollback option.
No position-protection or execution task is involved.
