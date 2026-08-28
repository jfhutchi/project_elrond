# The research subsystem

How Elrond decides what counts as evidence, and the mechanisms that stop it being talked into
something. This complements [STATUS.md](../STATUS.md), which covers the trading side.

---

## The recurring problem

Almost every defect found in this subsystem has had the same shape:

> **A gate reads its input from the thing it is supposed to constrain.**

It has appeared at least five times, in five unrelated modules:

| Where | The gate | What it trusted |
|---|---|---|
| #19 power gate | Is the sample big enough? | `available_observations`, declared by the registrant |
| #23 trial burden | How many candidates were searched? | `search_cardinality`, self-reported by an agent |
| #16 promotion | Has it traded 30 forward days? | `ForwardObservation` objects a caller could build |
| #18 provenance | Is the result traceable? | A manifest the caller could decline to persist |
| #38 kill switch | Is it safe to resume? | `BROKER_HEALTHY=true` in a dotenv |
| #53 deployment role | Has this claim been refuted? | `hypothesis_id`, chosen by the caller asking |
| #54 economic success | Did the forward window clear the bar? | A mandate the agent wrote itself |

None of these were *missing* controls. Each was a real, correct control fed by a claim. A gate
that validates a number it was handed by the party who benefits from the number is decoration.

The last two rows are the same shape one level up, and they are worth stating separately because
the fix is different. #53 was not a value a caller could inflate; it was a *relationship* — which
hypothesis stands behind a deployment — supplied at the moment it was checked. A strategy whose
mechanism had been refuted could be offered a different, unrefuted registration and the gate
would faithfully report finding no refutation of the thing it had been handed. #54 is the same
move against the success criterion rather than the evidence: an agent that writes the hurdle and
then announces its evidence cleared it has not been gated, it has been asked politely.

**A relationship is fixed by making it durable, not by making it a token.** The claim behind a
strategy is now written to `promotion_state` at `REGISTERED` and read everywhere after; the id is
refused as an argument at every other transition. A definition of success is fixed by requiring a
signature from outside the system: `PAPER_QUALIFIED` and `LIVE_REVIEW_ELIGIBLE` both require a
`RATIFIED` mandate, and a `ratified_by` that reads as an automated identity is refused at load.

---

## The pattern that fixes it

**Make the trusted input a type that only the verifying path can produce.**

```python
_TOKEN = object()

class MeasuredThing:
    def __init__(self, ..., token: object) -> None:
        if token is not _TOKEN:
            raise TypeError("this comes from measure(); it cannot be constructed, only measured")
```

The gate then takes `MeasuredThing` rather than a value object. A caller cannot assemble one, so
"this must be verified" stops being a convention and becomes something the type system enforces.

Used in four places, each guarding a different irreversible resource:

| Type | Produced by | Guards |
|---|---|---|
| `ProtectedAccess` | `ExperimentRunner._spend` | Reading a protected holdout |
| `LedgerObservations` | `observe_forward_days` | Incrementing paper qualification |
| `MeasuredReadiness` | `measure_readiness` | Clearing the kill switch |
| `SearchOrigin.MEASURED` | a durable `search_runs` row | Lowering the multiple-testing burden |

Two supporting rules, learned the hard way:

- **The runtime check matters, not the annotation.** A plain `list` satisfies every static
  reading of `Sequence[ForwardObservation]`. That *is* the attack, so the check is `isinstance`
  at runtime and the test exercises it.
- **Where tests need to mint one**, they use a designated issuer and a separate test walks
  `src/` asserting nothing in production calls it. The boundary is enforced where it matters
  rather than by making every unrelated fixture awkward.

---

## Data roles

Four roles, and the distinction between them is the point.

| Role | May be searched | Notes |
|---|---|---|
| `DISCOVERY` | Yes | Search burden is recorded |
| `VALIDATION` | Yes | Cumulative burden still recorded |
| `PROTECTED_EVALUATION` | **No** | Unreachable until a hypothesis is frozen and gates pass |
| `FORWARD_PAPER` | n/a | Real future observations only. Never reconstructed |

**Consumption happens before the measurement, never after.** An experiment that reads a holdout
and then crashes has still read it; recording consumption on success would make failing a way to
look at data for free.

**Reservation is not consumption.** A registration reserves a window; the reservation expires and
can be released. Only `CONSUMED` counts toward the burden. Conflating them meant registering a
hypothesis and never running it burned the holdout permanently — a research system that punished
you for thinking about an experiment.

**Exhaustion is global, attribution is per-family.** The multiple-testing burden belongs to the
data, not to the question's label. Forty hypotheses on 2016–2026 raise the luck bar for the
forty-first whatever family it belongs to; scoping exhaustion per family would hand out fresh
significance for the price of renaming the question. `WindowConsumption.by_family` gives the
reporting value without the loophole.

---

## Absence of evidence vs evidence of absence

Every connector distinguishes three outcomes, and collapsing them is how a research system
concludes that no company has ever filed anything:

| Outcome | Means |
|---|---|
| `RESULTS` | The source holds matching work |
| `NO_RESULTS` | We looked and it holds none — *evidence of absence* |
| `OUTAGE` | We could not look — *absence of evidence*, and never the same thing |

`searched_before()` counts only completed searches. A novelty check treating a failed attempt as
a completed one concludes the literature was checked when it was not.

Real captured responses drive the tests, because no invented fixture would have taught the
parsers about them: arXiv returns an `<entry>` titled "Error" for a bad query; SEC EDGAR returns
**HTML with status 200** for a CIK that does not exist, and an HTML 403 titled *"Your Request
Originates from an Undeclared Automated Tool"* to any client that does not name a contact.

---

## Verdicts that must not be collapsed

| Verdict | Means | Must never become |
|---|---|---|
| `REFUTED` | Measured, and it failed | — |
| `UNDERPOWERED` | The data could not resolve it | `REFUTED` — it is not evidence against the mechanism |
| `INCONCLUSIVE` | It ran; the number cannot be read | A verdict the run declined to make |
| `FAILED` | The experiment did not complete | Any research verdict at all |
| `EXPLORATORY_ONLY` | Measured outside a registration | Confirmatory evidence |

A crashed cycle stage becomes `BLOCKED`, never `REFUTED`. A crashed experiment becomes an
`ANALYSIS_DEFECT` record — kept, because this project's own error rate is evidence about the
project even when it says nothing about the market.

---

## External workers

Kronos, TradingAgents, RD-Agent, Qlib and language models sit **behind** the trust boundary.

- A worker that will not disclose its search cardinality is `EXPLORATORY_ONLY`. It may raise
  questions; it may not produce confirmatory evidence. There is no configuration that changes
  this — undisclosed search laundered into a clean-looking result is the failure the rule exists
  for.
- A worker claiming disclosure and reporting nothing is **refused**, not charged zero. An
  invented figure in the multiple-testing correction is worse than a known gap in it.
- A timeout is a failure, never "it searched and found nothing" — that would turn an
  infrastructure problem into a research finding.
- Worker stderr never reaches an exception message: it is arbitrary text that may echo the input
  manifest back, and a manifest can carry a key.
- `worker_adapter.py` imports nothing from `brokers` or `execution`, enforced by an import-graph
  test in both directions.

The same test walks the trading path and fails if it imports research code, so a research budget
refusal can never interrupt the paper account.

---

## Sandboxing generated code

Two backends, and the difference is stated rather than implied.

| | Windows (`runner.py`) | WSL2 (`wsl.py`) |
|---|---|---|
| Filesystem | Concealment — an absolute path still resolves | Mount namespace; the drives **do not exist** |
| Network | In-process `socket` patch, monkeypatchable | Network namespace; the syscall fails in the kernel |
| Memory | Polled by the parent | `RLIMIT_AS` — the allocation fails when made |
| CPU | Wall clock | `RLIMIT_CPU` — the kernel kills it |
| Network enabled | Allowlist, in-process ceiling | **Refused** |

The WSL backend refuses a network-enabled policy rather than half-enforcing it: filtering a
namespace to an allowlist needs privileges this process lacks, and opening the namespace would
give the weaker guarantee under the stronger name.

Every isolation test carries a **control** proving the same operation succeeds outside the
namespace. Without it, a backend that failed at everything would pass — a broken invocation
refuses to read the ledger very convincingly.

---

## Promotion and forward evidence

```
CANDIDATE → REGISTERED → RESEARCH_SURVIVOR → SHADOW → PAPER_OBSERVATION
          → PAPER_QUALIFIED → LIVE_REVIEW_ELIGIBLE
```

There is no stage after `LIVE_REVIEW_ELIGIBLE`. The transition table has no destination past it,
which is how "no agent can move a strategy into live trading" is implemented.

Paper qualification requires 30 forward days **and** 30 trades, derived from three agreeing
durable facts: a deployment record matching this exact configuration, a `qualification_days` row
the trading system wrote, and fills reached through `order_intents`. Trades are attributed by
`signal_date` rather than a UTC timestamp, because a fill at 20:00 Eastern is stored as 00:00 UTC
the next day and would credit a session that had not happened.

Clearing that floor is **authenticity, not economics**. Thirty losing trades satisfy it
completely. `PAPER_QUALIFIED` additionally requires `assess_forward_evidence` to return
`FORWARD_EDGE_SUPPORTED`: a benchmark-relative, beta-adjusted, adequately powered, positive result
on a claim frozen before the window opened, measured against a **ratified** economic objective.

Session returns are paired on **adjacent exchange sessions**, not on consecutive entries in the
list of qualified dates. A session the deployment failed on leaves no entry, and zipping the list
against itself turns two sessions of market movement into one session's return — inflating the
mean, the t, the Sharpe and the drawdown together. Adjacency is checked against the union of
benchmark bar dates and `qualification_days` rows of either flag; a pair spanning a known session
is dropped and counted rather than rescaled.

Three durable facts hang off the promotion record and nothing else may supply them:

| Field | Written at | Answers |
|---|---|---|
| `hypothesis_id` / `hypothesis_version` | `REGISTERED` | What claim is this strategy testing? |
| `objective_identity` | `PAPER_QUALIFIED` | Which economic objective was it judged under? |
| `evidence_identity` | `PAPER_QUALIFIED` | Which forward assessment justified the stage? |

An economic stage cannot be represented without all three, `enter()` refuses to place a strategy
at one, and `LIVE_REVIEW_ELIGIBLE` refuses a mandate whose identity differs from the one that
backed qualification — so a looser objective forces reassessment instead of inheriting a label.

`DeploymentRole` distinguishes an `OPERATIONAL_BASELINE` from an `EDGE_CANDIDATE` and is derived
from those durable facts rather than stored as a flag. Keeping a refuted strategy trading is
legitimate and useful — it is where slippage, reconciliation and recovery evidence comes from —
but it must not convert running time into progress toward a claim about edge.
`demote_on_refuted_claims` runs the derivation rather than waiting for the next qualification
attempt to notice, because a derived answer nobody derives is a stale answer.

An unresolved integrity incident demotes a strategy out of the forward track. Demotion does
**not** erase forward evidence — the ledger is unchanged, so resolving and re-promoting restores
days that really happened. An incident casts doubt on evidence; it does not make sessions
un-occur. The same is true of demotion on a refuted claim: the sessions happened, they stop
counting toward edge qualification, and they remain available as execution evidence.

---

## Edge progress, kept separate from engineering progress

`quantbot.research.milestone`. Six levels, derived from durable state, reported by
`quantbot research-status` beside the task queue and by the dashboard's edge-progress panel.

The failure mode it exists for is a project-management one: infrastructure and model integrations
are easy to measure and alpha is not, so issue closure quietly becomes a surrogate objective and a
long list of completed architecture issues visually overwhelms an unproven economic thesis.

| Level | Reached when | Derived from |
|---|---|---|
| M0 | the platform has run experiments and honestly refuted them | refutations in research memory |
| M1 | a frozen candidate cleared the survivor gate, unrefuted | `promotion_state` at or above `RESEARCH_SURVIVOR` with a live claim |
| M2 | the claim spans several instruments and a finding records it surviving | registry universe plus a `SURVIVED` record |
| M3 | authentic forward sessions accrue against that claim | the trading ledger |
| M4 | `PAPER_QUALIFIED` under #47, with capital economics under #51 | the ladder, plus a measurement that does not exist yet |
| M5 | every gate clear and a human may look — still not authorisation | `LIVE_REVIEW_ELIGIBLE` |

Three rules make it hard to flatter:

- **Contiguous.** A level counts only when every level below it does. Forward evidence with no
  surviving candidate behind it reports M0, not M3.
- **`unassessed` blocks.** M4 carries one permanently until #51 lands: the paper account holds
  $100 and the mandate's range reaches $10,000, and no amount of evidence at the smaller size
  distinguishes an effect that survives at scale from one that does not.
- **A reached level names an artifact.** "Nothing blocks it" is not "something shows it".

M2 rests on one inference worth stating: probe results are not stored in a queryable table, so it
reads the probe *requirement* a multi-instrument universe implies rather than the probe list that
ran. `compile_experiment` adds `cross-asset-generality` exactly when the universe is wider than
one instrument, and `survivor_objections` refuses a survivor whose planned probes did not all
pass. A second experiment compiler would break that chain, and
`test_a_multi_instrument_registration_implies_the_generality_probe` is what fails when it does.

### Before a new integration or subsystem

Answer three questions in the issue, before implementing:

1. **Which profitability milestone does this unblock?** "It is interesting" is not a level.
2. **What cheaper thing would answer the same question?** Usually a power calculation.
3. **What measurable result would cause us to reject or defer it?** Decide the bounded rejection
   test before running it, for the same reason a hypothesis is frozen before it is measured.

Worked examples, from #52: RD-Agent/Qlib may unblock M1 by generating genuinely novel factors —
but only where clean confirmatory capacity exists, which is now a query rather than a guess.
Moving the coordinator off the Pi protects M3/M4 evidence collection and does not itself advance
M1. Another dashboard feature advances nothing unless it exposes a decision-critical measurement.

This is a review convention, not a machine check: nothing in the code can tell whether an issue's
stated milestone is honest. It is written down so a reviewer has something specific to hold an
architecture proposal against.

### `NO_EDGE_ESTABLISHED` is a destination

`MilestoneReport.no_edge_established` reports when the project sits at M0 with no clean
confirmatory capacity left. That is a research result, not a software failure, and having a name
for it is what stops the alternative — adding another framework because the ladder has no way to
say stop.

---

## Statistical capacity

`quantbot.research.capacity` answers the question `window_consumption` does not:

> What observations remain unused for confirmatory testing, and what classes of hypotheses can
> legitimately use them?

`window_consumption` answers "is *this* range spent" for a range someone already knows to ask
about. That is the wrong shape for the decision it feeds, which is whether an expensive search is
worth starting at all. Read `quantbot research-capacity`, or the dashboard's statistical-capacity
panel, before believing that a completed data-platform issue created research budget.

**Declared sources, derived status.** `DATA_SOURCES` is hand-maintained for the same reason
`OBSERVATION_ALIASES` is: what a dataset observes, and which claims it can bear on, is a judgement
somebody makes and is accountable for. Everything else on a row — consumption, exhaustion, who
spent it, which family, what is reserved, what remains — is computed from durable state.

Four things the inventory refuses to do, each of which would overstate what is left:

| Would say | Actually |
|---|---|
| Two vendors serving the same dates are two budgets | One observation identity, one row, one budget |
| A capability the enum declares is capacity | `UNSUPPORTED` until a provider serves it |
| Crypto sessions confirm a US-equity claim | A different market is a different question |
| A source with no vintages can answer a backtest | `QUALITY_BLOCKED`: it says what is true now |

**The only new equity capacity is time, counted in sessions rather than days.** SIP US equities
2016–2026 cost 68 trials and is spent. A rolling feed keeps producing sessions, and nobody
searched a session that had not happened — so each row reports the session dates the database
actually holds observations for, after the last consumed one, attributed by `bars.provider`
prefix. An earlier version derived this from the calendar and reported a Saturday as capacity;
sessions exist when there is a bar for them and not otherwise, and a source with no local
observation store claims none.

`capacity_stage` is the pre-compute gate: it wraps the director's PROPOSED handler and blocks
before the memory query when the capacity a question needs is already spent, so a search that
could never be confirmed does not reach the expensive stages.

**It is per-claim when the task declares its datasets, and project-wide when it does not.**
`ResearchTask.required_datasets` names sources from `DATA_SOURCES` as **structured data** — never
inferred from the question text, because a wrong permissive semantic match hands out significance
that was already spent, which is the same reason `datasets.py` refuses to guess.

Three rules make the declared path hard to flatter:

- **Every named dataset must still have capacity, not merely one of them.** A claim needing two
  sources needs both; advancing on whichever happens to be unspent is the permissive direction.
- **An unrecognised name blocks.** A typo must not read as "no constraint", or the cheapest route
  past the gate would be to misspell the source a question depends on.
- **The undeclared fallback still gates.** It asks the only question answerable without knowing
  what the claim is about — does *any* source still have confirmatory capacity — and the reason
  string names which of the two checks ran, so a reader is never guessing.

The difference is not cosmetic. On an inventory where US equities are exhausted and macro is
untouched, the project-wide gate advances an equity question on the strength of macro capacity.
The declared path refuses it.

---

## Portfolio hard vetoes

`quantbot.risk.vetoes`. Rules that **reject rather than resize** (#58).

Everything in `risk/sizing.py` is a cap: when it binds, the order goes through smaller. That is
right for per-trade risk and wrong for concentration — downsizing a position that breaches a
sector limit still adds sector exposure. So the vetoes are a separate type from `SizingCapName`,
and the separation is the protection: an edit cannot turn a veto into a haircut without changing
which enum it belongs to.

They compose in `evaluate_order_gate` beside the kill switch and the drawdown halt, and apply to
`ENTRY` only. A veto that blocked an exit or a liquidation would rebuild the absorbing halt
`STATUS.md` records as a severity-1 defect, one layer up.

| Veto | Refuses when | Needs |
|---|---|---|
| `NET_EXPOSURE` | \|long − short\| breaches the ceiling | positions, equity |
| `STALE_MARKET_DATA` | the newest observation is too old, or stamped in the future | bar timestamps |
| `DAILY_LOSS` | down more than the limit from the session's opening equity | an equity mark from this session |
| `LIQUIDITY` | the order is too large a share of average daily volume | 20 sessions of volume |

**Fail closed, and name the missing input.** An enabled veto that cannot measure rejects, and
reports `unmeasured` so an operator fixes the cause rather than the threshold. That includes the
reference price: the first version of the paper-smoke path substituted zero when a symbol was not
already held, which let a real proposed entry contribute no projected exposure while
`NET_EXPOSURE` reported itself as evaluated. Zero is not a conservative stand-in for an unknown
price — it is the most permissive value the term can take. A missing price is now
`REFERENCE_PRICE_UNAVAILABLE` and refuses. `evaluate_order_gate`
refuses an entry with no veto decision at all (`PORTFOLIO_VETOES_NOT_EVALUATED`) — the optional
parameter exists so it could be added without rewriting every call site, not so it can be skipped.

**Versioned identity on every rejection.** #57 asks rejections to carry `gate_id`, `gate_version`
and `rule_parameters_hash` so a counterfactual study can attribute an outcome to a rule *at
particular settings*. Every rejection carries all three, and every veto speaks even when an
earlier one refused — a reason that never reached the decision cannot be evaluated.

**Which vetoes are enforced is operator policy.** The default enables `NET_EXPOSURE` and
`STALE_MARKET_DATA`, which a thin ledger can always answer. `DAILY_LOSS` and `LIQUIDITY` are
implemented and tested and **off by default**, because on a fresh ledger they correctly refuse
everything and would halt the entries this project needs to accumulate forward evidence at all.
Turning them on is the operator's call, exactly as ratifying the economic mandate is.

**One veto is declared and unimplemented.** `CORRELATION_CONCENTRATION` is a member of the enum
with no evaluator, and the policy **refuses to be configured with it enabled**. It needs a frozen
covariance — lookback, source, as-of and method identity all pinned — because a matrix
reconstructed later is not the object the veto had at decision time.

**`EXPOSURE_CONCENTRATION` landed with #59's first slice, and is named for exposure rather than
sector deliberately.** The original entry here said sector classification was look-ahead inside the
risk layer. That is right for single stocks and does not bind for this universe: all 23 instruments
are ETFs, and an ETF's exposure is its published mandate rather than a vendor's estimate of a
company's business. What survives from the original objection is that mandates *change*, so
`config/exposure-classification-v1.yaml` carries an `effective_from` and **refuses to classify any
decision earlier than it**. The date is a verification date, not an inception date — the table
declines to speak about history nobody checked, which costs the ability to backtest the veto and
is correct, because the veto could not have run over that history.

The rename is the substantive part. A sector-shaped rule would rate SPY + QQQ + XLK + IWM + MDY as
five uncorrelated bets when it is US equity beta wearing five tickers, and a limit that reports
itself as enforced while the concentration it exists to catch walks through it is worse than no
limit. A symbol carries every exposure its mandate implies and counts in each, which can only make
the veto more likely to refuse.

**Unknown blocks**, and that is the whole asymmetry: an unclassified instrument belongs to no
group, so it would add to no group's concentration and pass every limit by not being recognised.
An unclassifiable *held* position blocks too, because it makes every group total an understatement.

It ships **disabled**, with a threshold that deliberately does not bind. This universe is mostly US
equity — a roster of 10 from 23 can be near 100% `US_EQUITY` while being exactly what the strategy
is designed to hold — so one global ceiling is too loose for the dominant group and would be far
too tight for the narrow ones if set for it. Per-group ceilings are the likely next step, and
choosing them is a decision about risk appetite that belongs to the operator.

**A defect worth recording.** The staleness ceiling first shipped at 900 seconds, which encoded an
intraday assumption into a system that decides once per session on daily bars — the newest
observation is always the previous close, so it refused every entry and the wired-cycle
integration test caught it. The default is four days: a long weekend passes, a feed genuinely
behind does not. Calendar seconds remain a coarse proxy for "sessions behind"; the precise rule
needs the exchange calendar.

---

## Statistical machinery

- Jobson–Korkie–Memmel variance for Sharpe differences, because two series of the same asset are
  not independent samples.
- The luck bar rises as `sqrt(2 · ln N)` with cumulative trials. Trials never refund.
- Power is recomputed **at execution** against the burden as it then stands. A hypothesis
  adequately powered when frozen can be underpowered by the time it runs, and is refused rather
  than downgraded.
- Feature lookback and forward-outcome overlap are different things and are not interchangeable.

Invariants run against this project's own documented failures rather than synthetic cases. One
fixture was backwards on the first attempt: a strategy going from +427.9% gross to −28.8% net is
*disappointing*, not impossible, and an invariant firing on it would flag honest accounting as
fraud. It is now a pair — the real case that must pass, and the impossible case (costs improving
a return) that must fail.

---

## Reproducibility

**The bundle named a command that did not exist.** Every reproducible manifest recorded
`quantbot reproduce-experiment --experiment <id>` in `reproduction_command`, and no CLI
implemented it — an instruction inside the evidence record whose whole authority is that its
claims are mechanical.

The command exists now and does what can honestly be done today: it reports whether a frozen
bundle *could* be re-executed, and states in the same object that it did not try.

    quantbot reproduce-experiment --experiment <id> [--manifests DIR] [--patch FILE]

Three things fail closed:

| Condition | Result |
|---|---|
| A recorded input is missing, edited or truncated | blocks |
| The original ran dirty with no preserved patch | blocks, permanently |
| A dependency-lock difference | reported as drift, never silent |

Inputs are checked by hash against **the bytes the runner wrote**, recorded in the manifest at
run time as `reproduction_inputs`. Before that field existed the bundle claimed reproducibility
and recorded nothing about what a rerun would need, so a reader could not tell whether the
`.inputs` directory beside it was complete, edited, or the right directory at all. The manifest
now refuses to hold one half without the other: a command with no recorded inputs and inputs with
no command are both unrepresentable.

`performed` is a field on the report rather than an omission. A report that merely never mentions
running the experiment reads, to a hurried reader, exactly like one that ran it and found no
differences — and that reader is the one deciding whether to trust the evidence.

**What is still missing, listed by name in `OUTSTANDING`** so a clean readiness report cannot be
mistaken for a reproduction: resolving dataset snapshots back to bytes, reconstructing the
compiled plan, executing it through the authoritative path, producing a new manifest and
comparing, and replaying a frozen external-worker artifact rather than re-running its inference.

**#48 stays open**, and its first acceptance criterion is the reason. It asks that one real
historical confirmatory experiment be rerun from its manifest. There is no such manifest: nothing
outside an integration test has ever produced one, which is consistent with the project standing
at M0 with no confirmatory experiment to its name. A reproducer built now could only be exercised
against a synthetic fixture, and a feature nobody has exercised is a feature nobody may claim.

Every confirmatory run writes an immutable manifest **before** returning; the runner cannot be
constructed without a directory to write to. Both the success and crash paths record, because an
archive that only keeps what worked is the selection bias this project exists to resist.

`quantbot verify-manifest` checks invariants and reports code drift. `skipped` checks are
surfaced rather than folded into the pass — a check that quietly did nothing because the result
lacked the figure it needs looks exactly like a check that passed.

An earlier version of this section said `reproduction_command` was deliberately empty, on the
grounds that an unrunnable reproduction command is worse than an absent one because it reads as
reproducibility somebody verified. The reasoning was right and the description was **wrong**: the
runner populated the field whenever a measurement produced inputs, so the bundle had been making
exactly the claim this paragraph warned against. That is what the section above fixes.

---

## Open design questions

Recorded rather than resolved by implementation:

- **[#35](https://github.com/jfhutchi/project_elrond/issues/35)** — should registration-time
  power require a *verified* observation count, or is the declared figure correct at that gate?
  GPT-5.6 Sol and Claude disagree. Execution-time verification is settled and in place; the
  question is whether pre-registration should also pay for measurement, given that an expensive
  pre-registration step is one agents route around.
