# Routing value of the anomaly detector (#56)

**Status: routing value established on the measurable proxy. Not evidence of an edge.**

This is the falsification gate #56 required before any catalyst or model enrichment is built.
The premise under test is economic: *most symbols are informationally uninteresting most of the
time, so expensive research should follow information density rather than the calendar.* The
experiment was run first precisely because a subsystem that has already grown a news pipeline is
much harder to abandon than one that has not.

## What was measured

| | |
|---|---|
| corpus | `alpaca:sip:all:1day`, 23 symbols, 2016-01-04 .. 2026-08-14 |
| detector | `anomaly-detector-v1-a5fd3c25d75f8d40` |
| dependence snapshot | `dependence-v1-03d6be8c63b924a1` (120 sessions, complete linkage, 0.90) |
| report identity | `routing-value-v1-36a47d3aff4ff5b4` |

## Result

```
scanned symbol-sessions   60,007
selected                   1,058      selection rate 1.763%
searches avoided          58,949      98.24% of a naive scan

dependence units             981
intraclass correlation     0.4098      estimated from the outcomes
design effect              1.5519
effective observations       632      1.67x fewer than the raw event count

NEXT_SESSION_ABSOLUTE_MOVE   MEASURED
  selected mean             1.751 sigma
  baseline mean             0.802 sigma
  LIFT                      2.18x

CATALYST_PREVALENCE          CATALYST_CORPUS_ABSENT
```

**Reading.** Enriching 1.76% of symbol-sessions selects sessions whose *next* session moves
2.18x further, in absolute terms, than an average session — while avoiding 58,949 of 60,007
enrichment calls. On this corpus and this proxy, routing buys more informative questions per
unit of search cost, which is what #56 claimed it would.

## What this is not

**It is not an edge.** The proxy is a direction-free absolute move. It says something continued
to happen at the selected sessions, which is what an enrichment budget wants to buy; it says
nothing about whether trading them is profitable, and a proxy that rewarded direction would have
turned this gate into a backtest. No anomaly path holds broker authority, and any hypothesis
derived from these events still enters the normal registration and protected-evaluation path.

**Catalyst prevalence lift is unmeasured, not favourable.** The universe is broad-market ETFs,
which do not file material 8-Ks the way single issuers do, and no news provider is integrated.
`CATALYST_CORPUS_ABSENT` is reported rather than replaced by a stand-in nobody validated — a
lift computed against an unvalidated corpus is precisely the flattering artefact this gate exists
to catch. The claim above therefore rests on one proxy, and a second one could still overturn the
economics once a catalyst corpus exists.

## The correction worth recording

The first implementation of effective sample size keyed on `(session, dependence group)` and
**silently did not work**. Complete linkage at 0.90 yields 20 clusters over 23 symbols — almost
all singletons — so the 2025-04-07 session, where 22 of 23 symbols fired on one macro fact, still
counted as roughly 20 independent observations. The reported inflation was a reassuring **1.08x**.

The pairwise threshold that correctly refuses to call Treasuries and equities one bet is far too
strict to describe what happens when the whole market moves at once. Estimating the discount from
the outcomes instead (Kish design effect, session as cluster, ICC from the data) gives **1.67x**.
The lift is unaffected — it is a ratio of means — but every statement about sample size and
precision would have been overstated by two thirds.

## Reproduction

`quantbot.anomaly.routing` holds the estimator; `evaluate_routing_value` returns the frozen,
content-addressed report. The measurement script lives outside the package because it reads a
research store rather than the operator store. Re-running against a different corpus, detector or
dependence snapshot produces a different `routing-value-` identity, so two verdicts can never be
pooled.
