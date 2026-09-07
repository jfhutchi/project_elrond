# Position protection

The independent `protection-daemon` evaluates broker positions without waiting for strategy,
research, or model execution. It records every evaluated position in the existing durable ledger
and submits required exits through the execution coordinator. Deployment and authoritative-store
recovery remain subject to #190; the presence of this command does not prove a supervisor is
running on an operator's host.

## Policy v2: position-relative losses and usable inputs

`position-protection-v2` corrects the v1 evaluator's use of long-position arithmetic for shorts.
`move_bps` now means position-relative return from average entry: negative means a loss for
either side. With entry 100 and the project-default 2,000 bps hard stop:

| Position | Price | Move | Decision |
| --- | --- | --- | --- |
| Long | 75 | -2,500 bps | Exit via SELL |
| Short | 125 | -2,500 bps | Exit via BUY |
| Short | 75 | +2,500 bps | No hard-stop exit |

The exact loss threshold triggers an exit. The 2,000 bps account drawdown threshold retains
precedence when both rules fire. Thresholds must be positive finite `Decimal` values. Maximum
price age must be a nonnegative integer number of days; the default remains four days.

The evaluator refuses missing, future, timezone-naive, or expired price timestamps with
`BLOCKED_STATE`. Age is compared as an exact elapsed duration: four days is accepted, and four
days plus one microsecond is expired. Observation time itself must be timezone-aware.
Zero/nonfinite position quantities, invalid entry/market prices, and supplied negative/nonfinite
drawdown produce `INCIDENT`, never an exit or a healthy result. A missing drawdown input retains
the existing semantics: the per-position stop can be evaluated, but account drawdown was not
measured. These rules do not introduce a guessed substitute for missing data.

The runtime currently supplies a broker mark observed during the cycle. Its recorded time is the
cycle's observation time, not an independently verified exchange quote timestamp; this change
does not establish upstream quote freshness or intraday data guarantees.

Historical evaluation/action rows retain their stored policy version, hash, and move. There is
no migration or relabeling. New evaluations use v2 and a distinct cycle identity. Existing durable
actions retain their original order identity and follow the existing reconciliation path.

## Operational scope

This correction adds no trading authority. Paper account identity, risk-reducing quantity checks,
durable order identity, and broker reconciliation remain enforced by the execution coordinator.
Live trading remains disabled. The separate liveness lock and console status already exist;
operator deployment, host retirement, and measured protection coverage remain open requirements
under #157/#190.
