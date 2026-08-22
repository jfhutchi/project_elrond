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

None of these were *missing* controls. Each was a real, correct control fed by a claim. A gate
that validates a number it was handed by the party who benefits from the number is decoration.

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

An unresolved integrity incident demotes a strategy out of the forward track. Demotion does
**not** erase forward evidence — the ledger is unchanged, so resolving and re-promoting restores
days that really happened. An incident casts doubt on evidence; it does not make sessions
un-occur.

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

Every confirmatory run writes an immutable manifest **before** returning; the runner cannot be
constructed without a directory to write to. Both the success and crash paths record, because an
archive that only keeps what worked is the selection bias this project exists to resist.

`quantbot verify-manifest` checks invariants and reports code drift. `skipped` checks are
surfaced rather than folded into the pass — a check that quietly did nothing because the result
lacked the figure it needs looks exactly like a check that passed.

`reproduction_command` is deliberately **empty**. Nothing can yet re-execute a compiled plan, and
an unrunnable reproduction command is worse than an absent one because it reads as
reproducibility somebody verified.

---

## Open design questions

Recorded rather than resolved by implementation:

- **[#35](https://github.com/jfhutchi/project_elrond/issues/35)** — should registration-time
  power require a *verified* observation count, or is the declared figure correct at that gate?
  GPT-5.6 Sol and Claude disagree. Execution-time verification is settled and in place; the
  question is whether pre-registration should also pay for measurement, given that an expensive
  pre-registration step is one agents route around.
