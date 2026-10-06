# Separate five-minute PAPER experiment specification

> **Current guidance — 2026-10-06:** Historical October 1 experiment record. The [current direction](intraday-paper-direction.md) supersedes its current-goal interpretation while preserving all recorded limits, evidence and failures below.

Status: source-only preparation. The active minute-alpha-v1 DIA model and its
configuration are unchanged. This specification is not an activation approval,
trial charge, trained artifact, or claim of prospective performance.

## Contract and source

`five-minute-price-alpha-v1` consumes genuine published IEX `5Min` DIA/SPY bars,
with interval-start timestamps and seven complete same-session bars per asset.
The seven closes span thirty minutes; the earliest completed cutoff is thirty-
five minutes after the exchange open. All required timestamps must exist. No
forward fill, substitution of minute bars, venue-volume inference, or backward
sliding to conceal a missing newest panel member is allowed. The newest required
panel cutoff must be completed, no more than two minutes old, and leave thirty
minutes before the actual session close, including early closes.

Ten price-only features: 5/15/30-minute returns; population deviation of the three
five-minute returns in the last fifteen minutes; asset-minus-SPY fifteen-minute
return; distance from the last three/six closing-price mean; five-minute return
minus one-third of the fifteen-minute return; SPY fifteen-minute return; and SPY
fifteen-minute return deviation. These definitions are separate from the twelve-
feature minute contract. No existing minute coefficients may be reused.

Labels use the exact published asset and SPY bar whose completion is thirty
minutes after the feature cutoff. They retain actual receipt/availability time
and source hash. A missing target member yields no label. Prospective decisions
must follow receipt; historical evaluation must remain separately labeled and
must never be represented as prospective observation or account profit.

## Fixed research protocol before fitting

Use the existing authorized IEX provider only. Recent SIP access was denied;
do not retry through alternate routes or enable a paid subscription.

Proposed fixed training interval: February 2–March 13, 2026. Fixed validation:
March 16–March 31, 2026. Check the original learner's immutable protected-date
declarations and canonical registry before any collection. April–September
remains excluded even after its calendar end. Require at least twenty valid
training sessions and five valid validation sessions; disclose uneven coverage
and session concentration. Reject overlapping training/validation target
windows and purge boundaries. Do not broaden dates to rescue an unsuccessful
experiment.

Register and charge two immutable trials before fitting: constant baseline and
ridge alpha=1, seed=0. No parameter search or subsequent uncharged retries.
Reports must show raw coverage, missing windows, session counts, independent
nonoverlapping matched periods, errors, net simulated returns and drawdown.
Use the same benchmark windows and five basis points each side, with commissions
explicit. Preserve baseline comparison and historical/prospective separation.

## Activation remains a separate exact-artifact decision

No model fitting, provider collection, runtime wiring, or deployment is performed
by the pure contract module. A later frozen artifact must bind this feature
contract, provenance, charged trials and tested execution configuration. It must
not be loaded by the minute-model parser. Switching entry authority must be
auditable and exclusive; it must not introduce simultaneous independent entry
owners or take exit ownership of legacy holdings.

Preserve one new position, maximum 5% position value, 0.25% entry risk, 1% stop,
thirty-minute horizon and all tighter existing account/kill-switch controls.
Existing holdings retain their protection and exit owner. No live routing,
automatic challenger promotion, new credentials or paid access is authorized.

Acceptance remains actual prospective forecast → eligible PAPER intent/fill (or
explicit valid risk/cost veto) → independent protection/deadline → fill/outcome
accounting → logged next research decision. Forecast-only simulated gains are
not account profit. Missing/invalid sessions are not zero-return days. The
profitable-day target remains aspirational rather than a circular entry gate.
