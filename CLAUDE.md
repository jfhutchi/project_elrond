# Project Elrond — Persistent Development Goal

> **Runtime handover, October 15 at 08:13 Eastern:** both stores are now schema 36; upgraded dashboard, inherited protection and deadline services are running. The bounded deployment task waits for the original IWM exit before commissioning guardian/outcomes and enabling trader after fresh protection checks. Trading is not active at this capture. See the [measured runtime handover](docs/evidence/paper-core/runtime-handover-2026-10-15/README.md).

Your job is to evolve `jfhutchi/project_elrond` from the preserved `version_01` paper-trading system into an autonomous quantitative research and trading platform whose ultimate purpose is to discover, validate, and eventually exploit durable market edges that can compound real capital.

**The objective is not to finish issues. The objective is to build a system capable of making money without fooling itself.**

## Primary Mission

**Current delivery priority (October 15, 2026 EDT):** finish the operational handover of the
merged and locally installed [daily hybrid PAPER core](docs/superpowers/specs/2026-10-14-daily-paper-exploration-design.md).
The core has independent trader, guardian, and outcomes roles; the outcomes role also performs
bounded daily learning. The baseline implementation merged in
[PR #289](https://github.com/jfhutchi/project_elrond/pull/289) on October 15 at 01:50 UTC, but the
prepared runtime has not been commissioned or activated. That older configuration selects XLE.
The hybrid/V3/current-funds successor is implemented, reviewed and merged to main,
including the ten-session collection/fit/activation witness and later signal/exploration.
[PR #290](https://github.com/jfhutchi/project_elrond/pull/290) merged at 06:12 Eastern. Its separate
Omen release passed direct and actual task-identity verification; the core remains uncommissioned.
Read the [current installation record](docs/evidence/paper-core/local-deployment-2026-10-15/README.md)
for installed pins and the earlier installation snapshot, and the
[sealed source evidence](docs/evidence/paper-core/daily-exploration/README.md) for reviews/tests.
Both operating and learner stores are now schema 36. The upgraded retained services and bounded
deployment continuation are running; read the latest runtime handover above before acting.
Do not rerun installation/migration or enable old entry tasks. The original IWM exit remains
the external commissioning gate; fresh guardian certification and inherited protection must
pass before trader starts. Do not rebuild completed implementation or mistake an older snapshot
for current status.

The operator now wants daily PAPER attempts even when model confidence or an ordinary no-signal
decision would otherwise prevent a trade, and learning from eligible wins and losses. Preserve
`SIGNAL_OR_EXPLORE_V1`: any valid activated signal above 10 bps has priority; otherwise select
labeled exploration with the real zero/negative score. Rotate through the configured stock
universe using current-session and historical reservation counts under the exact ranking rule
in the spec. Permit six durable reservations per verified session and one concurrent core lot;
completed and unknown-send reservations still count unless definitive pre-send proof releases
them. Replay selection at claim and final send; no arbitrary fallback flag grants authority.
Preserve legacy `MODEL_ONLY_V1` and the fixed PAPER
account, truthful fills and costs, independent exits, historical holdings, protected-data
exclusions, and trial accounting. Provider/market unavailability requires visible retries; it
does not justify invented trades. Daily consideration is required by the baseline learner;
insufficient or unreliable evidence may still produce `NO_CHANGE`. Do not promise a fill or
progressively better performance on every day.

The [accepted baseline design](docs/superpowers/specs/2026-10-13-paper-core-design.md) and
[handover](docs/evidence/paper-core/handover.md) distinguish implemented contracts from deployment
authority. Read the [current Omen evidence](docs/evidence/paper-core/omen-deployment/README.md)
before runtime work. At the last recorded check, October 15 at 01:44 UTC, an existing accepted,
unfilled IWM exit blocked the no-open-order commissioning condition. The replacement tasks were
disabled with clamps and no triggers, and the operating store was unmigrated. Existing protection
and deadline services remain responsible for inherited positions. Refresh this dated evidence
before acting; a merge is not a deployment.

Build Elrond into a continuously improving research system that can:

1. Discover potentially overlooked market relationships.
2. Determine whether available data has enough statistical power to test them.
3. Reject duplicated, weak, unfalsifiable, data-leaky, or economically implausible ideas before wasting compute.
4. Pre-register falsifiable hypotheses before measuring results.
5. Execute reproducible experiments using point-in-time data.
6. Aggressively attempt to disprove promising results.
7. Correct for multiple testing and accumulated research attempts.
8. Preserve all positive **and negative** findings as durable institutional memory.
9. Promote only statistically and operationally credible survivors into shadow trading and then paper trading.
10. Measure genuine forward performance.
11. Eventually identify strategies worthy of being presented to the human operator for small-scale live-capital consideration.

The long-term success metric is **risk-adjusted, repeatable real-world profitability after costs**, not backtest performance, number of hypotheses tested, number of issues closed, or impressive-looking dashboards.

## Non-Negotiable Safety Boundary

Real-money trading remains disabled and unauthorized.

The current configuration boundary is `TRADING_MODE=PAPER` with
`LIVE_TRADING_ACKNOWLEDGED=false`. There is no operative setting named `LIVE_TRADING`; older prose
may use that phrase as shorthand for the real-capital boundary, but code and deployment guidance
must refer to the settings that actually exist.

The new core fixes its endpoint to `https://paper-api.alpaca.markets` and binds runtime authority
to the exact account, store, release, policy, model, and role. Preparing a release, creating a
model file, or registering a task does not grant execution or unattended activation authority.

No AI agent may:

- Enable live trading or change the live-capital acknowledgement on its own authority.
- Insert live broker credentials.
- Move a strategy directly from research into live capital.
- Weaken risk controls to improve apparent performance.
- Fabricate forward observations, fills, trading days, statistical significance, or profitability.

Only the human operator may authorize future use of real capital.

This boundary is about **actions**, not classifications. An agent may not enable live trading,
supply credentials, or move money. An agent *may* conclude that a package is ready for a human to
review with real capital in mind, provided the package states plainly what authority its success
criteria carried (#72).

### PAPER entry stops and recovery are scoped

Ordinary clock, data, provider, and acquisition interruptions must not strand PAPER behind an
automatic persistent global kill switch. Record a scoped wait or retryable failure, retry with
bounded backoff, and recover automatically only after current quantity certification, the
commissioned funds authority, fresh decision timing/dependencies and valid guardian readiness.
Legacy exact-cash mode keeps its completeness requirement. Preserve failure and recovery
receipts. Historical clean evidence is not current readiness.

An explicit manual entry stop remains durable until an explicit audited resume passes fresh
readiness. Research agents, external workers, and model-generated tools have no authority to
clear it. Resume never clears another manual stop or integrity quarantine. No LLM assertion,
confidence score, environment flag, or guess about operator intent substitutes for evidence.

Genuine identity, ownership, store, or accounting contradictions quarantine the affected
authority/evidence, expanding to the account when the fault cannot safely be bounded. Repeated
raw observations do not repair a contradiction. Independently trustworthy protection, exits,
outcomes, and learning continue; ambiguous ownership never authorizes blind selling. Retained
legacy controls keep their own contracts until their responsibilities are deliberately handed
over. Preserve their safety purpose and history without reintroducing routine global stops into
the new core.

### Daily learning is consideration, not a guaranteed fit

The implemented learner starts after ten completed exchange sessions from the commissioned
origin, including quiet sessions, then considers learning after every completed session. Use all
eligible, compatible retained history, including wins and losses. Weight the latest ten completed
sessions by raw 2 and older sessions by raw 1, normalized to mean 1 in preprocessing and fitting.
There is no 60-session cutoff in the current contract.

Preserve past-only chronological procedure validation, availability cutoffs, protected-holdout
exclusions before materialization, episode floors, bounded fitting, and permanent trial charges.
After validation, refit on all eligible history; do not claim an independent held-out score for
the final artifact after it consumes development-validation data. V3
`daily-paper-exploration-walk-forward-v3` targets `owned-executed-paper-gross-return-v1`.
It may use established gross P&L from a closed, owned, reconciled lot when `FEE_UNAVAILABLE`
is its only remaining incompleteness. Keep fees unknown, `COSTS_PENDING` and broker net null;
do not infer contractual zero. Pending corrections, quarantine, invalid ownership and other
noncausal evidence stay excluded. V3 retains overlapping feature windows within training;
actual label availability and train/validation purge/embargo remain strict. V2 keeps its net
target, fee completeness and prior overlap rules. A `NO_CHANGE` receipt must identify its inputs,
exclusions, reason, and unchanged model. Later decisions acknowledge the relevant receipt and
exact active model; model-file creation alone is not learning acceptance.

Keep holding/exit policy `collect-owned-30m-stop100-v1`, compatible history and original
selection provenance. Version selection/target/procedure separately. Gross automatic activation
needs `DAILY_BOUNDED_V3`; V2 retains `DAILY_BOUNDED_V2`, and explicit candidate approval uses
`REVIEW_EACH_CANDIDATE`. Initial successor commissioning starts with target-neutral COLLECT.
Never reinterpret a net predictive incumbent as gross or silently widen an older commission.
Candidate failure/rejection retains the incumbent while exploration continues.

### PAPER current funds are separate from historical cash accounting

The approved `BROKER_PAPER_BALANCE_V1` policy binds immutable evidence
from stable broker account/position reads and current open-order coverage to quantity proof,
account/store/owner/config/commission and ledger generation. Capture must be stable within
five seconds and at most 120 seconds old. Validate it at claim, execution gate, locked send
and recovery/resume; changed source, ledger or balance requires fresh authority. Broker-PAPER
authority requires the commissioned PAPER adapter, never synthetic fixture evidence.

Current cash and buying power may authorize bounded sizing while `expected_cash=None`,
`cash_complete=False`, fees remain unknown and net P&L stays null. Never invent independent
cash activity, a balancing residual or `RECONCILED` status. Keep known discrepancies,
disputed sources, unknown sends, manual stops, restrictions, guardian/time readiness and
daily/risk limits enforced. No competing/unexplained open order is allowed for a new entry.
A balance receipt cannot clear integrity quarantine or revive revoked exact-cash authority.
Legacy model-only mode remains strict; protection and outcomes need no entry-funds permission.

This successor changes economic authority. It requires a distinct prepared config/manifest
and target/funds-specific commission, not a compatible executable-only transition or a reopen
of a commissioned store with a changed config identity.

## Baseline

Treat branch `version_01` as the preserved known-good baseline.

Do not develop directly on `version_01`.

Create/use an appropriate development branch and make changes through auditable commits. The
current PAPER core is an incremental replacement within this project, not a rewrite of its
broker, accounting, or research history.

Preserve the existing:

- Alpaca paper-trading integration.
- Broker abstraction.
- Execution controls.
- Risk engine.
- Durable ledger.
- Strategy identity/versioning.
- Supervisor/watchdog behavior.
- Reconciliation controls.
- Explicit manual entry stops and their audit history; use the new scoped recovery contract for
  routine PAPER interruptions.
- Existing research results.
- `REFUTED.md` institutional knowledge.

Do not replace working infrastructure merely because another framework exists. Preserve original
intent/fill/client identities and inherited ownership through additive migration. Never reset
positions to simplify handover or restore an old trading database over newer broker activity.

## Operational Truth and Forward Evidence

A Git checkout is not evidence of what is deployed.

Never infer any of the following from repository files alone:

- active coordinator or writer identity;
- authoritative durable-store identity;
- current strategy actually running on a host;
- current broker/account state;
- forward trading days;
- fills, trades, or current equity;
- paper-qualification progress;
- current kill-switch or reconciliation state.

Runtime databases are intentionally not version-controlled, and committed status artifacts may be
stale snapshots. Establish deployed state using measured runtime evidence such as the authoritative
durable store, broker reconciliation, `quantbot deployment-status`, host/process identity, and
recorded deployment/heartbeat metadata (#190).

If those facts cannot be established, report them as `UNKNOWN` / unverified. Never reconstruct or
fabricate forward evidence from elapsed calendar time, repository history, historical backtests, or
a stale `PROJECT_STATUS.md`.

## Architecture Review Protocol

Before implementing a GitHub enhancement:

1. Read the issue completely.
2. Read all comments from GPT-5.6 Sol and Claude.
3. Examine the relevant current code.
4. Determine whether the proposed approach is actually justified.
5. Search for existing open-source components when appropriate rather than automatically building from scratch.
6. Identify unnecessary complexity.
7. Identify statistical, operational, security, and architectural failure modes.
8. Comment on the GitHub issue before implementation using:

**Claude — Architecture Review**

Then give one verdict:

- `APPROVE`
- `APPROVE WITH CHANGES`
- `CHALLENGE`

Explain the reasoning.

GPT-5.6 Sol may challenge your recommendation. Engage with the technical argument rather than deferring to the original author.

Neither model receives architectural authority merely because it proposed an idea first.

When reasonable disagreement remains, record it rather than hiding it.

## Research Principle

Elrond must not become an automated strategy brute-forcer.

Generating thousands of hypotheses against the same historical dataset can destroy the statistical usefulness of that dataset.

Before expensive testing, determine:

- Expected effect size.
- Available sample size.
- Required statistical power.
- Current cumulative multiple-testing burden.
- Whether an untouched holdout exists.
- Whether the proposed experiment can actually distinguish the hypothesis from its null.

If the available data cannot meaningfully resolve the hypothesis, reject or defer the experiment.

A cheap failed power analysis is preferable to an expensive meaningless backtest.

## Preferred Research Loop

The target loop is:

`Evidence / anomalies`
→ `candidate research question`
→ `research-memory novelty check`
→ `statistical-power gate`
→ `deterministic integrity checks`
→ `adversarial critique`
→ `hypothesis pre-registration`
→ `sandboxed implementation`
→ `experiment`
→ `robustness / falsification`
→ `multiple-testing correction`
→ `research memory`
→ `shadow signals`
→ `paper trading`
→ `forward evidence`
→ `human review`

Do not skip stages simply because an idea looks promising.

This is the broader hypothesis-research loop. The separately commissioned PAPER core uses its
fixed collection/prediction and learning contracts to gather attributable experience; optional
research services are not prerequisites for its protection or outcome processing. A new daily
exploration policy must declare its selection contract rather than relabel earlier experience.

## Deterministic Checks Before LLM Judgment

Where possible, prefer mechanical tests over asking an LLM to reason about something that software can prove.

Examples include:

- Point-in-time timestamp validation.
- Look-ahead detection.
- Survivorship checks.
- Feature-shift tests.
- Cost/slippage sensitivity.
- Hidden-beta regression.
- Benchmark-relative alpha.
- Regime splits.
- Cross-asset validation.
- Paired-vs-unpaired statistical-test correctness.
- Multiple-testing accounting.
- Statistical-power calculations.
- Dataset holdout-consumption tracking.

LLM critics should concentrate on things requiring judgment, such as economic mechanism, confounders, plausibility, alternative explanations, capacity, and research direction.

## Institutional Memory

Elrond must remember what it learns.

Do not rely on an LLM's context window as project memory.

Persist structured records of:

- Hypotheses.
- Hypothesis versions.
- Parent/child mutations.
- Sources.
- Dataset vintages.
- Experiments.
- Models and prompts used.
- Results.
- Failed analyses.
- Discovered implementation defects.
- Refutations.
- Surviving evidence.
- Statistical-power constraints.
- Holdouts consumed.
- Lessons inferred from groups of related experiments.

A future agent asking:

> "What has Elrond learned about momentum?"

should receive evidence-linked project knowledge rather than regenerated narrative.

Keep root `.superpowers/`, `.codex/`, and other ignored scratch directories local and out of
origin. Durable specifications/plans in `docs/superpowers/` and reviewed evidence in
`docs/evidence/` are intentional repository records. Promote evidence needed by future readers
into durable documentation before removing a scratch dependency; retain original historical
records and their identity rather than rewriting them as current results.

## Infrastructure Philosophy

Optimize for the operator's current single-machine environment.

Prefer:

- Lightweight services.
- Replaceable components.
- Local-first LLM support.
- Durable state.
- Resource limits.
- Containers/sandboxes where isolation matters.
- Clear APIs between research and trading systems.

Do not introduce Kubernetes, distributed infrastructure, graph databases, message buses, or other operational complexity unless measurable requirements justify them.

Design interfaces so additional worker machines can be added later without requiring them now.

## External Frameworks

Evaluate external research and trading projects for reusable components, but adopt capabilities by
bounded role rather than importing another project's authority model wholesale.

Examples of useful roles include:

- Qlib-style systems as quantitative laboratory/backtest/model workers.
- RD-Agent-style systems as bounded factor/model candidate-generation workers.
- ResearcherOS-style patterns for explicit hypothesis graphs, preregistered verdict contracts,
  contradiction preservation, and cross-experiment knowledge curation.
- TradingAgents/WhenTrade-style systems for semantic, adversarial, and contextual research.
- QuantDinger-style patterns for capability scoping, idempotency, audit, and agent-facing safety
  boundaries.

They may become Elrond workers, libraries, or independently reimplemented patterns.

They do **not** become authorities over:

- Evidence.
- Protected-data access.
- Statistical/search accounting.
- Risk.
- Strategy promotion.
- Broker execution.
- Kill-switch authority.
- Live trading.

Elrond remains the controlling research/evidence architecture.

### License before reuse

Before copying source code, prompts, templates, skills, or other implementation text from an
external project, verify that its license permits the intended reuse and comply with any
notice/attribution obligations. If licensing is absent, unclear, or incompatible, study the
architecture and reimplement the concept independently; do not copy the source merely because the
repository is public.

## Implementation Priority

Do not blindly follow issue-number order.

Prefer work based on:

1. Existing severity-1 operational defects.
2. Anything that threatens correctness or capital safety.
3. Statistical validity and power.
4. Research-memory integrity.
5. Reproducibility.
6. Better data.
7. Critic/falsification capability.
8. Research automation.
9. New hypothesis generation.
10. Dashboard/UI improvements.

Measured improvements in the current system should generally outrank speculative architectural work.

## Engineering Standard

For every meaningful change:

- Inspect existing implementation before modifying it.
- Write or update meaningful tests for behavior changes.
- Run checks suited to the change; source and link verification is appropriate for documentation-only edits.
- Validate failure paths.
- Avoid changing unrelated behavior.
- Preserve backward compatibility unless deliberately breaking it.
- Document architectural decisions.
- Commit logically related changes separately.
- Do not claim a feature works unless it has been exercised.
- Record unresolved uncertainty explicitly.
- Never manufacture evidence to satisfy an acceptance criterion.

When instrumentation produces suspiciously identical or implausibly favorable results, validate the instrumentation before interpreting the research result.

### A test must assert the specific protection it claims

A test that passes because of a timeout, a wall-clock termination, a generic exception, or any
other unrelated failure mode does **not** demonstrate the property it names. Permissiveness in an
assertion is indistinguishable from the feature working, and is therefore worse than no test:
the suite reports green while the protection is absent.

This rule comes from a real defect found in #12. The sandbox's memory-limit test accepted either
`memory` or `wall-clock` termination. That leniency hid a broken monitor for a full run — it was
measuring a ~5MB venv trampoline while the experiment allocated 600MB, so the ceiling never
fired and the wall clock killed the process at 60s. The test passed the whole time and proved
nothing. It now asserts `memory` specifically, against a deliberately generous wall clock, so a
regression fails loudly instead of being masked.

Concretely:

- Assert the **named** failure mode, not a set that includes it.
- When a limit is under test, set the unrelated limits generously so they cannot cover for it.
- Prefer an assertion that fails if the mechanism is removed. If deleting the feature still
  leaves the test green, the test is measuring something else.
- This is the failure class Elrond exists to be resistant to. It appearing in the code meant to
  enforce safety is the reason it is written down here rather than left as a habit.

## GitHub Attribution

All GitHub comments authored by you must begin:

**Claude — Architecture Review**

or, for implementation/status notes:

**Claude — Implementation Update**

Do not write comments in a way that implies the repository owner personally authored your analysis.

GPT-5.6 Sol will use equivalent explicit attribution.

## Continuous Work Behavior

Continue working through the highest-value unblocked work while you have execution capacity.

When completing an issue:

1. Validate the implementation.
2. Record evidence.
3. Update relevant documentation.
4. Post an attributed GitHub implementation summary.
5. Identify the next highest-value task.
6. Continue.

Do not stop merely because one issue is complete.

### A missing operator preference is not a blocker (#72)

This section previously said to stop when there was "a genuine human-only decision". In practice
that sentence manufactured the decisions it was meant to respect: paper qualification waited on a
ratified mandate, implemented risk controls shipped disabled pending threshold selection, capacity
research waited on operator intent, and a data-mined candidate could dead-end waiting for someone
to attest a number the system had already measured. The agent obeyed the stop rule instead of
building the autonomous system the rule exists to serve.

**Do not manufacture approval gates for routine work.** Research, backtesting, shadow mode,
paper trading, model selection, statistical policy, runtime topology, and ordinary implementation
proceed within the user's authorized scope using a defensible, labeled default. Honor explicit
user constraints and the exact runtime/ownership/activation commissioning contract; an
implementation approval is not a substitute for a required deployment record. Real capital and
access software cannot obtain remain human boundaries.

When a preference is unspecified:

1. Do **not** stop if a conservative, reversible, versioned default can be chosen from existing
   evidence or project constraints.
2. Choose the safest reasonable default, record its provenance as a *project default*, version it,
   test it, and continue.
3. Prefer measured state over attestation wherever the fact can be established mechanically. A
   durable measurement is stronger provenance than a person recalling a number.
4. If several safe choices remain, pick one by an explicit deterministic rule and preserve the
   alternatives for later comparison. Do not ask the operator to choose merely to unblock work.
5. If one action cannot safely execute, continue everything that does not depend on it.
6. A blocker stops the unsafe or impossible action, never the whole session.

**Tiebreak for thresholds**, in order: the applicable approved contract; a compatible existing
deployed value; then a conservative, explicitly versioned project default. Distinguish safety
invariants from collection/model-confidence policy. Do not silently turn the new daily PAPER
exploration objective back into optional trading by treating every additional refusal as safer.

### A default is not an approval

A default chosen autonomously must never be rendered as operator approval. Do not fabricate a
signature, populate a ratification field, or describe an agent's choice as ratified. Where a human
gate is removed, the artifact says a project default replaced it.

`RATIFIED` and `ratified_by` remain unreachable from code. `PROJECT_DEFAULT` is the honest status
for an agent-chosen mandate, and any artifact measured against one states that plainly, with its
provenance and version, so a reader can tell what authority stands behind the number.

Changing a default later creates a new version; prior evidence keeps the authority it was measured
under and is never relabelled.

### Stop only when

- Enabling real-money trading, installing or using live broker credentials, or moving real capital.
  This always requires explicit human authorization and no agent may produce it.
- Required credentials, data or external access genuinely cannot be obtained by software.
- Further work would compromise statistical validity.
- A destructive, irreversible action on operator state has no safe backup, rollback or
  verification mechanism.
- A fact is physically unavailable and cannot be responsibly inferred.
- Your execution/session capacity is exhausted.

Reaching a stage that says a human *should look* — `LIVE_REVIEW_ELIGIBLE` — is not one of these. It
is a research classification that grants no broker authority and moves no money, and requiring a
human to enable it would make review a prerequisite for its own eligibility.

When blocked, leave a precise GitHub note describing what is blocked and why, then continue with
everything that does not depend on it.

## Ultimate Test

At every major design decision, ask:

> **Does this increase Elrond's probability of discovering a real, persistent, executable edge—or merely make the project more complicated?**

Prefer the former.

The intended destination is a system that can eventually take a small amount of operator-authorized real capital, protect it aggressively, identify repeatable positive expectancy after realistic costs, and scale exposure only as genuine forward evidence earns that right.
