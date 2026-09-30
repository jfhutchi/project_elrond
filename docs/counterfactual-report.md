# Counterfactual operator report

Run against the configured existing durable store (`QUANTBOT_DB_PATH` or
`QUANTBOT_DATABASE_URL`, following the normal database target rules):

```shell
quantbot counterfactual-report
quantbot counterfactual-report --by-regime
quantbot counterfactual-report --policy-identity counterfactual-policy-v1-EXACT_HASH --by-regime
```

The JSON response prints the selected `policy_identity`. The default is the current
`HypotheticalExecutionPolicy` identity; `available_policy_identities` lists identities
actually present in stored outcomes. Copy the complete identity to select historical
results. Policies never pool. An unknown identity gives zero evaluated outcomes and
explicit `NO_RECORDED_OUTCOME` counts, not results from another policy.

Groups separate strategy, configuration, gate, gate version and parameter hash. Counts
are distinct candidates; `observation_ids` retains all contributing refusals. One
candidate can appear in several gate groups. Do not sum groups as independent events.
Conflicting answers for a repeated candidate fail explicitly.

`outcome_counts` distinguishes evaluated, terminal unevaluable, explicit unavailable
and no recorded outcome. Missing outcome rows do not prove that a horizon is still
pending: evaluation may not have run or this policy may never have been used.
`unevaluable_reasons` and `unavailable_reasons` retain the recorded explanations.

`--by-regime` groups only the frozen snapshots bound to refusals, by method version,
benchmark, source, trend, volatility and breadth. Each group retains its snapshot IDs.
Missing bindings, missing snapshots and partially measured regimes stay explicit.
Future-dated bindings fail rather than introducing hindsight. Historical labels are
never reconstructed from current market data.

The existing gate-value calculation controls effective sample size and suppresses the
mean for underpowered groups. Its `candidates` count covers evaluated outcomes only;
the enclosing `distinct_candidates` includes refusals without an evaluated answer.
The sign describes the hypothetical return after costs relative to the benchmark,
not actual cash P/L. Dependence-cluster membership is unavailable in these records;
the existing session estimator cannot cover all shared events or overlapping horizons.

Every report is **EXPLORATORY**. Regime selection is explicit, not automatically
preregistered. Means and verdicts are descriptive, not causal effects, confidence
intervals or permission to change gates. Threshold changes need a registered research
question and search accounting; this output supplies neither. Uncertainty intervals,
tail/catalyst/liquidity analyses and further coverage remain tracked by #57.

The command opens no runtime, sends no orders, evaluates no new outcomes and writes no
history. It does not create or migrate a store. A missing store is an error, not an
empty healthy report. Running it locally does not establish the Omen's deployed state.
