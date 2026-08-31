# Project Elrond Competitive Frontier

**Research snapshot:** 2026-08-29  
**Implementation branch:** `codex/frontier-research-20260829`  
**Scope:** open-source trading frameworks, commercial retail products, current statistical research, and Elrond's own architecture and roadmap.

## Executive decision

Elrond should not try to win by claiming the most indicators, the fastest parameter sweep, the largest agent swarm, the broadest broker list, or a mysterious "AI score." Those features already exist in mature products and are increasingly commoditized.

Elrond's defensible category is **evidence-governed autonomous trading research**:

> Every strategy carries its proof; every decision is causally traceable; every runtime state is reproducible; no agent can silently spend statistical credibility or reach capital.

The flagship artifact should be an **Elrond Evidence Passport**. It is a signed, portable, machine-verifiable bundle attached to every strategy version. It should answer, without relying on narrative claims:

- What was hypothesized, by whom or by which agent, and before which data was observed?
- Exactly which data, code, configuration, prompt, model, calendar, universe, and cost assumptions produced the result?
- How many alternatives, retries, prompts, features, models, thresholds, and portfolios were actually tried?
- What did simple baselines and empirical nulls achieve?
- How much independent information was available after dependence and overlapping horizons?
- Did the result survive chronological selection adjustment, bias probes, stress tests, realistic costs, and independent replay?
- What has happened on authentic forward data, with uncertainty valid under continuous monitoring?
- Can a clean worker reproduce the artifacts and decisions byte-for-byte or explain every divergence?
- Why is the strategy not promoted, if it is not promoted?
- Which runtime incidents, drift states, reconciliation failures, or operator actions have affected it?

No reviewed open-source or commercial product visibly combines Elrond's existing preregistration, protected data roles, search accounting, power gates, durable negative results, forward-only qualification, untrusted worker boundary, and explicit paper-only capital boundary with a proof-carrying strategy artifact and deterministic causal replay. That is a market opening, not yet a proven superiority claim.

## Honest claim boundary

It is not possible to prove from public material that Elrond is "better than every free or paid product." Closed systems do not expose enough implementation detail or audited live performance, and vendor performance claims are not independent evidence. Elrond should therefore make superiority measurable rather than promotional.

A defensible public claim is:

> Elrond is designed to make overfitting, leakage, irreproducibility, and unexplained runtime divergence visible and enforceable as promotion blockers.

That claim becomes strong only when a published mutation-test corpus demonstrates that the controls catch intentionally injected failures.

## Competitive landscape

| Product or project | Strongest reviewed capability | Pattern worth adopting | Elrond's opening |
|---|---|---|---|
| [Freqtrade / FreqAI](https://github.com/freqtrade/freqtrade) | Mature crypto lifecycle, dry run, adaptive model training, model expiry, exchange integration, and executable [lookahead analysis](https://github.com/freqtrade/freqtrade/blob/develop/docs/lookahead-analysis.md) | Trainer/consumer separation, model registry and expiry, executable leakage probes | Every retrain must have an immutable manifest, declared trial budget, protected-data firewall, and challenger shadow; adaptive training must not silently reset the evidence claim |
| [Microsoft Qlib](https://github.com/microsoft/qlib) and [RD-Agent](https://github.com/microsoft/RD-Agent) | Broad point-in-time ML pipeline, experiment recording, factors, rolling models, portfolio construction, and automated factor/model generation | Point-in-time factor expression DAG, reusable recorder, nested execution contracts | Count every generated factor, prompt, retry, model, and hyperparameter branch against the research ledger; automation may expand work, never statistical credibility |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Strong shared event-driven core for backtest, sandbox, and live; explicit risk, reconciliation, fill models, event sourcing, and deterministic simulation | Causal event kernel, ports/adapters, seeded failure tests, order-state reconciliation | Add tamper-evident hash chaining, capture-completeness gates, clean-state differential replay, and proof bundles around the runtime trace |
| [LEAN](https://github.com/QuantConnect/Lean) | Clean Universe/Alpha/Portfolio/Risk/Execution contracts, broad brokerage integrations, configurable reality models, and [live reconciliation](https://www.quantconnect.com/docs/v2/writing-algorithms/live-trading/reconciliation) | Versioned fee, fill, slippage, buying-power, and brokerage semantics | Make all reality assumptions evidence artifacts, calibrate them against paper observations, and explain divergence at every processing layer; default zero slippage must never be acceptable evidence |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | Strong crypto connector architecture and Controller/Executor lifecycle for long-running strategies and finite order behaviors | Connector contracts, controller/executor separation, multi-bot orchestration | Require conformance certification for timestamp, calendar, duplicate, reconnect, partial-fill, and secret-capability semantics; retain Elrond's independent evidence controls |
| [vectorbt](https://github.com/polakowo/vectorbt) | Very high-throughput matrix exploration and excellent notebook ergonomics | Fast discovery engine on explicitly exploratory data | Record the entire evaluated grid and confirm every survivor in an authoritative event engine; vectorized speed is not execution truth |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) / [FinRL-X](https://github.com/AI4Finance-Foundation/FinRL-Trading) | Standardized research environments and broad ML/DRL agent support | Pluggable sandbox interfaces and simple train-test-trade workflows | Treat RL as an untrusted research worker until reward perturbation, friction randomization, simple baselines, simulator calibration, and offline-policy evaluation pass |
| [Jesse](https://github.com/jesse-ai/jesse) | Accessible notebooks, fast backtests, optimization, Monte Carlo, and assistant/MCP ergonomics | Fast falsification workflow and read-only research-assistant surface | Deliver equally easy research with dependence-aware statistics and evidence-grade verdicts; never present an in-sample p-value as profitability evidence |
| [TrendSpider](https://trendspider.com/product/artificial-intelligence-ai-trading-strategy-lab/) | Polished no-code strategy construction, scanning, feature engineering, ML lab, alerts, and bots | Natural-language/visual strategy ergonomics | Compile natural language into a typed, versioned strategy IR and show the incremental trial budget and data-role consequences before a run |
| [Composer](https://www.composer.trade/) | Clear natural-language to editable allocation-tree workflow with backtest and automation | Round-trip AI, visual, and programmatic representation with semantic diffs | Couple the representation to an Evidence Passport, realistic execution assumptions, and promotion gates rather than treating a convenient backtest as proof |
| [Trade Ideas](https://www.trade-ideas.com/features/) | Polished streaming opportunity discovery and automation UX | Immediate opportunity radar and inspectable candidate queue | Expose the frozen market snapshot, mechanism, availability, uncertainty, trial burden, and rejection reason behind every opportunity |
| [QuantRocket](https://www.quantrocket.com/docs/) | Practical service isolation, scheduled workflows, data collection, notebooks, vector and event research, separate order boundary | Independently runnable pipeline stages with explicit artifacts | Add adversarial scientific controls and clean-worker reproduction around each stage |
| [vn.py](https://github.com/vnpy/vnpy) | Broad event-driven gateways, recorders, strategies, WebSocket/REST clients, and plug-in risk rules | Gateway architecture and capability-specific pre-trade rules | Certify adapters and retain proof-bearing decisions above the execution layer |
| [OctoBot](https://github.com/Drakkar-Software/OctoBot) and [Superalgos](https://github.com/Superalgos/Superalgos) | Friendly visual/self-hosted automation and broad extension surfaces | Onboarding, inspectability, extensibility | Use signed, capability-scoped research plug-ins and generated provenance graphs; canonical artifacts, not a visual canvas, remain the source of truth |

## What Elrond already does unusually well

The existing project has the foundation for this category:

- Paper-only execution and explicit denial of live capital access.
- Immutable, content-addressed research artifacts and explicit data roles.
- Preregistration and a hypothesis ledger that preserves negative results.
- Search-budget accounting, novelty checks, power gates, and empirical-null thinking.
- Protected historical windows that are not silently recycled after exhaustion.
- Fail-closed, untrusted research-worker boundaries.
- Distinct signal, risk, order-intent, reconciliation, milestone, and promotion concepts.
- Dependence-aware session snapshots and frozen anomaly-event models.
- A roadmap that already recognizes executable reproduction, capacity, shadowing, regimes, anomaly/catalyst evidence, provider-neutral identity, and a control console.

These are more valuable than adding another forecasting model. The audit also found that several important capabilities are presently models or readiness checks rather than complete operating loops.

## Current gaps that matter most

1. **Executable reproduction is not executable yet.** The reproduction path reports readiness but does not reconstruct inputs, rerun a clean worker, emit a new manifest, and compare outputs and decision traces.

2. **Research-cycle stages are not fully wired.** The CLI currently creates proposed capacity/novelty artifacts but does not orchestrate scouting, critique, registered experiments, reproduction, and final evidence in one durable state machine.

3. **Anomaly evidence needs durable exact persistence.** Frozen anomaly and dependence models exist, but the storage representation must preserve every baseline session and every numeric field so an event can be replayed exactly.

4. **Runtime health can overstate stream connectivity.** A runtime must never label a stream connected merely because the process is healthy or a REST path is available. Health needs a real connection state and message high-water mark.

5. **Capital capacity exists but is not fully included in milestone assessment.** A gate that is not passed to promotion logic is documentation, not enforcement.

6. **Reconciliation is not yet a deterministic runtime twin.** The system needs a single causal trace and layer-by-layer divergence classification across data, features, signals, risk, intents, fills, and portfolios.

7. **Forward qualification is vulnerable to repeated looks.** Authentic forward data is scarce. A fixed day/trade count is not enough; interim decisions need anytime-valid uncertainty or a strictly fixed analysis date.

8. **CI skips cluster around the most operationally important boundaries.** OS sandboxing, WSL, and live local-model execution need dedicated integration environments or explicit release-risk accounting.

## Product moat: the Elrond Evidence Passport

The Passport is a schema, validator, signature, and renderer. It should be portable JSON plus a human-readable report and contain references to immutable artifacts rather than copying mutable state.

### Required sections

1. **Identity**
   - Passport schema version and content hash.
   - Strategy IR hash, source revision, configuration hash, dependency lock hash.
   - Producer identities and signatures.
   - Parent strategy and semantic diff from the parent.

2. **Hypothesis and preregistration**
   - Mechanism, prediction, falsifiers, primary metric, benchmark, economic null.
   - Analysis unit, horizon, timing conventions, multiple-testing family, success/futility criteria.
   - Registration timestamp and evidence available at registration.

3. **Data lineage and roles**
   - Provider-neutral instrument, venue, calendar, timestamp, revision, and corporate-action identities.
   - Snapshot hashes and byte-level source references.
   - Discovery, validation, protected, and forward roles.
   - Availability lags, missingness, revisions, and quarantine state.

4. **Search ledger**
   - Total candidates, prompts, retries, features, models, thresholds, allocations, seeds, and post-hoc branches evaluated.
   - Complete candidate identities, including failures and discarded results.
   - Family definitions and adjusted significance/selection evidence.

5. **Historical evidence**
   - Anchored outer walk-forward path.
   - Purge/embargo rules and label-interval audit.
   - Inner CPCV/PBO stability report where applicable.
   - Deflated Sharpe Ratio, empirical-null percentile, stationary bootstrap, worst/median path, parameter stability.
   - Explicit warning that resampling does not create new information or restore an exhausted protected window.

6. **Bias and metamorphic tests**
   - Prefix invariance.
   - Future poisoning.
   - Chunk invariance.
   - Warm-up invariance.
   - Calendar invariance.
   - Reference-engine differential replay.
   - Mutation-test outcomes and open incidents.

7. **Execution reality**
   - Versioned fee, spread, fill, latency, borrow, settlement, halt, auction, and market-impact models.
   - Decision/arrival/ack/fill timestamps and implementation shortfall.
   - Partial/no-fill and opportunity-cost accounting.
   - Calibration sample, confidence bounds, conservative fallback, and capacity curve.

8. **Uncertainty and forward evidence**
   - Frozen forward benchmark and economic null.
   - Observation unit, delayed-label policy, overlapping-horizon policy.
   - Running effect and anytime-valid confidence sequence/e-process where assumptions support one.
   - Every interim look, success/futility/max-duration boundary, and an honest `NOT_YET_RESOLVED` state.
   - Calibration coverage, interval width, abstention, and regime-conditional diagnostics.

9. **Runtime causality**
   - Causal journal root/hash, high-water marks, capture-completeness verdict.
   - Strategy-to-order correlation and causation graph.
   - Reconciliation and shadow-twin divergence by layer.
   - Drift state, incidents, quarantines, operator actions, and recovery proof.

10. **Promotion verdict**
    - Machine-verifiable gate results.
    - Explicit reasons not promoted.
    - Authority and capital scope.
    - Earliest permissible next action and what new evidence, not reanalysis, is required.

## Technical architecture

### 1. Tamper-evident causal event journal

Every material event should have:

- Monotonic sequence within a journal partition.
- Event ID, schema version, event type, producer identity, and occurred/observed/recorded timestamps.
- Correlation ID and causation ID.
- Strategy, experiment, evidence, runtime, and account identities where applicable.
- Canonical payload hash and previous-event hash.
- Capture source and completeness status.
- Idempotency key and conflict behavior.
- Optional encrypted payload envelope with an inspectable, unencrypted routing header.

The first implementation should use the anomaly/catalyst workflow because its frozen event model, missing-versus-unknown semantics, enrichment boundary, rejected-candidate shadow relationship, and eventual order intent naturally exercise the journal.

Acceptance criteria:

- Exact append replay reconstructs the same materialized state and hashes.
- An exact duplicate is idempotent; a reused identity with different bytes raises a conflict.
- A missing sequence, broken hash, wrong producer, or schema downgrade quarantines the stream.
- Restart between intent, broker acknowledgement, and fill cannot duplicate an order.
- High-water marks are durable, monotonic, and independently inspectable.
- The runtime cannot report healthy capture without a recent verified high-water mark.

### 2. Deterministic reproduction and fault replay

Extend issue #48 from readiness assessment into an isolated replay job:

1. Resolve the registered source revision and dependency lock.
2. Materialize exact input bytes and verify every hash before execution.
3. Start a clean, network-denied worker with only declared capabilities.
4. Execute the registered plan with frozen seed, clock/calendar, environment, and resource limits.
5. Emit a new manifest, outputs, logs, causal trace, and environment attestation.
6. Compare declared artifacts and semantic traces.
7. Classify every mismatch instead of returning a single boolean.
8. Preserve the failed replay as evidence.

Fault campaigns should inject duplicate, delayed, reordered, and dropped events; clock skew; stale data; partial fills; rejected orders; rate limits; and restarts between each order-state transition.

### 3. Metamorphic backtest audit

This is the highest near-term engineering return because it detects plausible-looking but impossible results.

For every candidate runner:

- **Prefix invariance:** output through time T equals a run ending at T.
- **Future poisoning:** edits after T cannot alter decisions at or before T.
- **Chunk invariance:** batch and streamed chunks yield the same canonical events.
- **Warm-up invariance:** once registered warm-up is satisfied, additional prehistory does not change decisions.
- **Calendar invariance:** inserted non-trading dates do not change outputs.
- **Trace parity:** identical canonical inputs yield identical backtest, shadow, and paper decision/order-intent traces.
- **Differential confirmation:** a reference engine agrees on timestamps, positions, cash, and P&L for canonical fixtures, or opens an incident with both assumption sets.

Freqtrade's executable [lookahead analysis](https://github.com/freqtrade/freqtrade/blob/develop/docs/lookahead-analysis.md) and [recursive analysis](https://github.com/freqtrade/freqtrade/blob/develop/docs/recursive-analysis.md) are useful starting patterns. A second engine is an oracle for disagreement, not automatic truth.

### 4. Anytime-valid forward evidence

Forward evidence should support continuous monitoring without turning each status check into an undeclared significance test.

Create a versioned `ForwardEvidenceProcess` with:

- Frozen benchmark/economic null and registered outcome.
- Non-overlapping observation units and strict delayed labels.
- Running effect plus a confidence sequence or e-process appropriate to the bounded score/null assumptions.
- Preregistered success, futility, and maximum-duration boundaries.
- An immutable record of every interim look.
- `NOT_YET_RESOLVED` when no boundary is crossed.
- Separate dependence-aware P&L analysis when martingale assumptions do not hold.

Time-uniform confidence sequences support valid continuous monitoring under explicit assumptions; see [Howard et al.](https://arxiv.org/abs/1810.08240) and [Henzi and Ziegel](https://academic.oup.com/biomet/article/109/3/647/6375942). They do not automatically fix serial dependence, unbounded returns, overlapping labels, or a poorly specified null.

### 5. Unified selection-adjustment report

Historical validation should emit one immutable `SelectionAdjustmentReport`:

1. Anchored outer walk-forward defines the chronological deployment path.
2. Training samples with label intervals overlapping the test interval are purged.
3. An embargo handles remaining boundary dependence.
4. Inner combinatorial purged cross-validation estimates path/parameter instability and Probability of Backtest Overfitting.
5. Hyperparameters and ensemble weights freeze before each outer test window.
6. Every evaluated alternative is recorded.
7. The report includes DSR, PBO, empirical-null rank, path distribution, worst/median fold, and parameter stability.

Primary references include the [Deflated Sharpe Ratio](https://doi.org/10.2139/ssrn.2460551) and [Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). CPCV is a diagnostic, not a source of new data and not permission to relabel exhausted history as confirmatory.

### 6. Versioned execution-reality laboratory

Make every execution assumption a frozen artifact:

- Spread, fee, order type, time-in-force, auction, partial/no-fill, latency, rejection, halt, settlement, borrow, margin, and corporate-action semantics.
- Decision, arrival, acknowledgement, and fill clocks.
- Implementation shortfall against decision, arrival, auction, and VWAP benchmarks.
- Opportunity cost for unfilled/expired orders.
- Buckets for symbol/liquidity, volatility, spread, order type, session phase, and participation.
- Seeded stochastic simulation from empirical quantiles.
- A conservative fallback for under-sampled buckets.
- Capacity curves across account size and participation rather than one global basis-point constant.

At Elrond's present approximately $100, liquid-ETF scale, auction behavior, fractional/minimum order behavior, rejected orders, spread, and real paper fills matter more than L2/HFT simulation. For eventual scaling, [cvxportfolio's transaction-cost formulation](https://www.cvxportfolio.com/en/1.3.1/costs.html) and [Almgren et al.'s empirical impact work](https://www.cis.upenn.edu/~mkearns/finread/costestim.pdf) are useful references.

### 7. Typed Strategy IR

Natural language, visual editing, and Python APIs should compile to one typed DAG:

- Universe and provider-neutral instrument identities.
- Data sources, availability lags, revisions, and calendars.
- Feature graph.
- Forecast and uncertainty contract.
- Allocation.
- Independent risk constraints.
- Execution policy and reality model.
- Evaluation and promotion contract.

Before execution, a semantic diff must show changed nodes, affected hypotheses, added trials, invalidated evidence, protected-data consequences, permissions, and expected resource cost. The assistant can propose a diff but cannot silently apply it or expand permissions.

### 8. Uncertainty-aware abstention and drift

Forecast workers should return calibrated sets/intervals, not ungrounded confidence. An adaptive conformal wrapper should track point prediction, bounds, target coverage, calibration-window hash, delayed labels, rolling and regime-conditional coverage, interval width, and calibration incidents. It should abstain when an interval crosses the cost-adjusted hurdle and may only reduce size as uncertainty grows.

Ordinary split conformal relies on exchangeability, which markets violate. Relevant starting points are [Adaptive Conformal Inference](https://proceedings.neurips.cc/paper/2021/hash/0d441de75945e5acbc865406fc9a2559-Abstract.html) and [Conformal PID](https://proceedings.neurips.cc/paper_files/paper/2023/hash/47f2fad8c1111d07f83c91be7870f8db-Abstract.html). A conformal interval is not a probability of profit.

Treat drift as risk state:

- Data drift: schema, missingness, revisions, feature distributions, universe.
- Model drift: delayed residuals, directional loss, calibration coverage, abstention.
- Execution drift: spreads, shortfall, fills, rejects, latency.

Responses are versioned state transitions such as `ACTIVE -> REDUCING -> SHADOW -> HALTED`, never silent retraining or ex-post model switching.

## Prioritized delivery plan

### P0: make claims reproducible and traces exact

1. Durable exact anomaly-event and dependence-context persistence.
2. Executable clean-worker reproduction with classified mismatches.
3. Causal journal envelope, hash chain, high-water marks, and anomaly-workflow integration.
4. Metamorphic bias test harness with prefix/future/chunk/calendar invariants.
5. Real stream health and capture-completeness reporting.
6. Wire capacity into milestone assessment.

Exit criteria:

- A frozen anomaly event survives database round-trip byte-for-byte.
- A clean worker reruns a registered canonical experiment and reproduces its declared artifacts.
- Mutation tests prove that future leakage, a missing journal event, reused event identity, and an altered input byte are blocked.
- Runtime health turns unhealthy on a stale/broken stream and never reports an invented connection.
- Promotion cannot bypass reproduction, capture completeness, or capacity.

### P1: turn evidence into a product

1. Evidence Passport schema, validator, signature, JSON export, and human renderer.
2. Layer-by-layer paper/shadow runtime-twin divergence report.
3. Versioned execution-reality bundle with real-fill calibration.
4. Anytime-valid forward process for bounded operational and forecast scores.
5. Unified selection-adjustment report.
6. Web-console Evidence Passport, causal "why?" trace, semantic diff, and blocker view.

Exit criteria:

- One command or API call validates a Passport from immutable references without repository-local hidden state.
- Every green gate links to its exact artifact; every missing field fails closed.
- An unexplained paper/shadow divergence opens an incident and blocks promotion.
- Forward status can be checked arbitrarily often without changing type-I error under the registered assumptions.

### P2: scale discovery without weakening controls

1. Typed Strategy IR with AI/visual/code round-trip.
2. Vectorized discovery worker behind the search ledger.
3. Authoritative event-engine differential confirmation.
4. Point-in-time factor DAG and safe cache.
5. Evidence-budgeted champion/challenger/shadow ModelOps.
6. Connector conformance certification.
7. Robust no-alpha portfolio allocator before expected-return optimization.

Exit criteria:

- Every generated variation increments search cardinality before results are viewed.
- No discovery-engine result can promote without event-engine replay.
- Retraining creates a new immutable identity and resets the applicable promotion evidence.
- A connector cannot load until its declared semantics and mandatory fault tests pass.

## Public benchmark and mutation corpus

A published benchmark is the strongest way to turn architectural claims into evidence. Include tiny deterministic fixtures plus deliberately corrupted variants:

- Lookahead in a rolling feature.
- Global normalization fit on future data.
- Incorrect corporate-action timing.
- Survivorship-biased universe.
- Overlapping labels without purge.
- Uncounted hyperparameter retry.
- Best-of-many selection presented as a single test.
- Zero or stale slippage model.
- Optimistic full fill in an illiquid bar.
- Duplicate broker acknowledgement.
- Restart between submit and acknowledgement.
- Missing journal sequence.
- Reused event ID with altered payload.
- Stale WebSocket with healthy REST.
- Strategy/config drift between research and paper.
- Future model knowledge or ticker/date memorization in an LLM research worker.

For every mutation, publish the expected blocking control and machine-readable incident. This makes the moat independently testable.

## What not to build now

- Direct LLM trading, agent voting, or model "confidence" treated as statistical probability.
- Deep RL before a calibrated simulator, strong simple baselines, and credible offline counterfactual evaluation exist.
- Automatic retraining that does not create a new version, preregistration, and evidence reset.
- Ex-post regime discovery followed by selecting the best strategy for each regime.
- Synthetic/GAN data counted as confirmatory evidence.
- More transformer tuning on the exhausted historical equity window.
- HFT, options, or order-book infrastructure at current capital and data scale.
- Broker-count expansion before connector conformance and reconciliation are reliable.
- A vectorized backtest treated as execution truth.
- CPCV, conformal inference, causal libraries, or agent debate presented as magic certificates.
- Lower costs before the registered paper-fill sample justifies it.
- A claim that Elrond is already better than every free or paid system.

## Immediate branch scope

The current branch should remain narrow enough to verify:

1. Persist frozen anomaly events and dependence relations without losing baseline-session identity or event fields.
2. Add additive schema migration, idempotent repositories, conflict detection, and exact round-trip tests.
3. Publish this competitive frontier as the governing architecture decision.
4. Follow with executable reproduction and the causal journal in reviewable increments.
5. Keep the branch unmerged until CI, adversarial review, and explicit human review are complete.

## Source index

### Systems

- [Freqtrade](https://github.com/freqtrade/freqtrade), [FreqAI](https://github.com/freqtrade/freqtrade/blob/develop/docs/freqai.md), [lookahead analysis](https://github.com/freqtrade/freqtrade/blob/develop/docs/lookahead-analysis.md), [recursive analysis](https://github.com/freqtrade/freqtrade/blob/develop/docs/recursive-analysis.md)
- [NautilusTrader](https://github.com/nautechsystems/nautilus_trader), [architecture](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/concepts/overview.md), [fill models](https://nautilustrader.io/docs/latest/concepts/backtesting/fill-models/)
- [LEAN](https://github.com/QuantConnect/Lean), [reality models](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/key-concepts), [slippage models](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/slippage/supported-models)
- [Qlib](https://github.com/microsoft/qlib), [RD-Agent](https://github.com/microsoft/RD-Agent)
- [Hummingbot](https://github.com/hummingbot/hummingbot), [V2 architecture](https://hummingbot.org/strategies/v2-strategies/)
- [vectorbt](https://github.com/polakowo/vectorbt)
- [FinRL](https://github.com/AI4Finance-Foundation/FinRL), [FinRL-X](https://github.com/AI4Finance-Foundation/FinRL-Trading)
- [Jesse](https://github.com/jesse-ai/jesse)
- [QuantRocket](https://www.quantrocket.com/docs/)
- [vn.py](https://github.com/vnpy/vnpy)
- [TrendSpider AI Strategy Lab](https://trendspider.com/product/artificial-intelligence-ai-trading-strategy-lab/)
- [Composer](https://www.composer.trade/)
- [Trade Ideas](https://www.trade-ideas.com/features/)
- [OctoBot](https://github.com/Drakkar-Software/OctoBot)
- [Superalgos](https://github.com/Superalgos/Superalgos)

### Statistical and execution foundations

- [Howard et al., Time-uniform confidence sequences](https://arxiv.org/abs/1810.08240)
- [Henzi and Ziegel, Valid sequential forecast comparison](https://academic.oup.com/biomet/article/109/3/647/6375942)
- [Bailey and Lopez de Prado, Deflated Sharpe Ratio](https://doi.org/10.2139/ssrn.2460551)
- [Bailey et al., Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)
- [Adaptive Conformal Inference](https://proceedings.neurips.cc/paper/2021/hash/0d441de75945e5acbc865406fc9a2559-Abstract.html)
- [Conformal PID](https://proceedings.neurips.cc/paper_files/paper/2023/hash/47f2fad8c1111d07f83c91be7870f8db-Abstract.html)
- [cvxportfolio cost models](https://www.cvxportfolio.com/en/1.3.1/costs.html)
- [Almgren et al., Direct estimation of equity market impact](https://www.cis.upenn.edu/~mkearns/finread/costestim.pdf)
- [Markowitz Portfolio Construction at Seventy](https://web.stanford.edu/~boyd/papers/markowitz.html)
- [DoWhy](https://github.com/py-why/dowhy)
- [Double/debiased machine learning](https://academic.oup.com/ectj/article/21/1/C1/5056401)

### LLM-agent caution

- [TradingAgents](https://arxiv.org/abs/2412.20138)
- [FinCon](https://arxiv.org/abs/2407.06567)
- [Profit Mirage / FinLake-Bench](https://arxiv.org/abs/2510.07920)
