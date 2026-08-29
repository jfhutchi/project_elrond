"""What Elrond refused, kept so the refusing can eventually be judged (#57).

A risk governor can look prudent while quietly destroying edge, or look overly conservative while
preventing exactly the losses that would have ended the account. Nothing in this project could
tell those apart, because rejections were recorded as free-text strings inside a signal's JSON
payload: no queryable gate, no version, no parameters, no outcome. "How often did the liquidity
rule refuse, and at what settings" required parsing prose.

**This is the most dangerous dataset the project will hold.** Its explicit purpose is to argue for
changing risk thresholds, and the gates that produce it are the ones it would argue about. Every
structural decision below exists because of that.

**Phase 1 is the portfolio-veto shadow substrate, and nothing wider.** The hook records
refusals from `evaluate_portfolio_vetoes`. `size_entry` rejections still return their reasons into
the free-text signal payload, so this does not yet make *every* deterministic rejection
shadow-capable, and #57's acceptance criterion to that effect stays open.

**Phase 2 owes an append-only split.** `state` and `unavailable_reason` sit on the parent row
today, which is tolerable only because the single reachable state is `UNAVAILABLE`. Reaching
`EVALUATED` must **not** be done by rewriting the rejection after seeing the outcome: phase 2 adds
an outcome record keyed to the immutable rejection, and reporting joins them. A row that can be
edited once the answer is known is the shape this whole subsystem exists to refuse.

**The rejection and the counterfactual are two writes at two times.** What was refused is known at
decision time; what would have happened is not known until the horizon has elapsed. Collapsing
them forces either computing an outcome from data that does not exist yet, or deferring the record
until it does and losing the rejection to a restart. So `RejectedCandidate` is immutable and
written when the refusal happens, and its outcome starts as `COUNTERFACTUAL_UNAVAILABLE` -- the
honest state of every record this phase produces, and not a placeholder to be quietly overwritten
by an evaluator that has seen the answer.

**One candidate, many reasons.** `evaluate_portfolio_vetoes` deliberately does not short-circuit,
so a single entry can collect four refusals at once. Counting those as four observations inflates
the sample fourfold -- the manufacture of sample size that killed cycle 11's overnight result. A
rejection is therefore *one* event carrying *many* reasons, and `distinct_candidates` exists
beside any reason count so a report cannot show the larger number by accident.

**Nothing here can reach a broker.** The module imports no broker, no coordinator and no order
type, and `RejectedCandidate` has no path to an `OrderIntent`: it records a symbol and a
direction, never an order. That is the type-level version of criterion 9, and the test suite also
walks the module's imports so the property cannot be lost by someone adding a convenience.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.risk.evidence import LEGACY_EVIDENCE, DecisionEvidence, EvidenceKind

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CounterfactualState(StrEnum):
    """What is known about the outcome a rejection prevented.

    `UNAVAILABLE` is the initial and, for now, only reachable state. It is a recorded fact rather
    than a missing field: a record with no outcome and no explanation reads like an oversight,
    and one that says why nobody could compute it reads like the truth.
    """

    #: No outcome has been computed. Every record this phase writes is this.
    UNAVAILABLE = "COUNTERFACTUAL_UNAVAILABLE"
    #: An outcome was computed under a frozen hypothetical execution policy.
    EVALUATED = "COUNTERFACTUAL_EVALUATED"
    #: The horizon elapsed and the data needed to evaluate it never arrived. Terminal, and
    #: distinct from `UNAVAILABLE`: one is "not yet", the other is "never".
    UNEVALUABLE = "COUNTERFACTUAL_UNEVALUABLE"


class CandidateDirection(StrEnum):
    """What the refused candidate would have done. Not an order, and never becomes one."""

    LONG = "LONG"
    SHORT = "SHORT"


class ShadowModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RejectionReason(ShadowModel):
    """One rule refusing, with everything needed to attribute an outcome to it later.

    `gate_version` and `rule_parameters_hash` are what make an analysis honest across a threshold
    change: outcomes recorded under different parameters are different datasets, and pooling them
    is the mistake that would let a rule be judged on evidence gathered under a different rule.
    """

    gate_id: Text
    gate_version: int = Field(ge=1)
    rule_parameters_hash: Text
    detail: Text
    #: The contemporaneous state this rule refused on (#85). The parameter hash says which rule
    #: was in force; this says what it was looking at. Two refusals can share the first and not
    #: the second -- the same freshness policy refuses a missing Tuesday and a missing Thursday,
    #: the same correlation threshold refuses under a matrix measured in March and one measured
    #: in August -- and pooling those because the hashes match is the error one level down from
    #: pooling across a threshold change.
    evidence: DecisionEvidence = DecisionEvidence(kind=EvidenceKind.NONE, payload={})


class RejectedCandidate(ShadowModel):
    """One refusal, immutable, carrying every reason it was refused for.

    Deliberately *not* an order. There is no quantity, no intent id, no client order id and no
    side that an execution path would recognise -- a shadow observation that could be handed to a
    coordinator is one that eventually will be.
    """

    observation_id: Text
    #: Which decision this was, independent of what any gate concluded about it. Present so #57
    #: can say "this exact candidate was rejected" rather than inferring it from a gate set.
    candidate_id: Text
    strategy_id: Text
    configuration_hash: Text
    symbol: Text
    direction: CandidateDirection
    observed_at: datetime
    #: The Eastern session this belongs to, for the reason every other date in this project is.
    session_date: date
    #: The price the candidate would have referenced. Recorded because a counterfactual computed
    #: later needs the entry the decision actually saw, not the one hindsight would pick.
    reference_price: Decimal = Field(gt=0, allow_inf_nan=False)
    #: Every rule that refused, in the order they were evaluated. One event, many reasons.
    reasons: tuple[RejectionReason, ...] = Field(min_length=1)
    #: The frozen registration behind the strategy, when there is one.
    hypothesis_id: str | None = None
    #: Context this project cannot yet produce. Nullable and recorded as absent rather than
    #: defaulted, because a substituted context is indistinguishable from a measured one once it
    #: is inside an analysis (#59, #56).
    market_regime_snapshot_id: str | None = None
    catalyst_snapshot_id: str | None = None
    anomaly_event_id: str | None = None
    portfolio_snapshot_id: str | None = None
    state: CounterfactualState = CounterfactualState.UNAVAILABLE
    #: Why no outcome exists. `Text` is min-length-1, so a record cannot be silent about it --
    #: the constraint does the work rather than a validator repeating it.
    unavailable_reason: Text = "no hypothetical execution policy is frozen yet (#57 phase 2)"

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.symbol != self.symbol.upper():
            raise ValueError("symbol must be uppercase")
        gates = [reason.gate_id for reason in self.reasons]
        if len(gates) != len(set(gates)):
            raise ValueError(
                "one gate cannot refuse the same candidate twice; a repeated gate id would "
                "count one refusal as two observations"
            )
        return self

    @property
    def gate_ids(self) -> tuple[str, ...]:
        return tuple(reason.gate_id for reason in self.reasons)

    def explain(self) -> str:
        return (
            f"{self.symbol} {self.direction.value} refused by "
            + ", ".join(self.gate_ids)
            + f" ({self.state.value})"
        )


def candidate_id(
    *,
    strategy_id: str,
    configuration_hash: str,
    symbol: str,
    session_date: date,
    candidate_key: str,
) -> str:
    """Identity for the *candidate*, established before any gate has ruled on it.

    The first version of this module identified a refusal by strategy, symbol, session and the
    set of gates that refused it. GPT-5.6 Sol's review found what that declares: **two materially
    different candidates are the same observation** whenever they share those four things. That
    breaks the moment a different configuration evaluates the same symbol in a session, or #56
    emits more than one anomaly for one symbol in a day -- and it breaks silently, by merging two
    observations into one.

    So identity comes from what the candidate *is*, not from what happened to it:

    * `configuration_hash` -- #57 requires a material config change to start a new observation
      family, and it cannot if the id does not carry it.
    * `candidate_key` -- content and provenance of this particular candidate. Today the decision's
      bar-set hash, which is rerun-stable and changes when the evidence does. When #56 lands it
      becomes the anomaly event id, and the semantics are the ones Sol asked for: replaying a
      frozen anomaly does not manufacture another sample, a genuinely new anomaly does.

    Deliberately **not** `observed_at`. Retry timing must not manufacture a candidate.
    """
    payload = {
        "strategy_id": strategy_id,
        "configuration_hash": configuration_hash,
        "symbol": symbol,
        "session_date": session_date.isoformat(),
        "candidate_key": candidate_key,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"candidate-{digest[:32]}"


def observation_id(*, candidate: str, policy_identity: str) -> str:
    """Identity for one candidate's refusal *under one policy*.

    Two components, because they answer different questions. The candidate says which decision
    this was; the policy says which rules were in force when it was refused. The same candidate
    re-evaluated under changed thresholds is a new observation -- correctly, since a different
    rule was being tested -- while a plain rerun under unchanged rules is not.
    """
    digest = hashlib.sha256(f"{candidate}|{policy_identity}".encode()).hexdigest()
    return f"rejected-{digest[:32]}"


class GateTally(ShadowModel):
    """How often one gate refused, counted two ways because they are different numbers.

    `reasons` counts refusals and `distinct_candidates` counts candidates. A single entry can
    collect four refusals at once, so the first is always at least the second and a report that
    quotes only the larger one overstates how much the gate has actually been tested.
    """

    gate_id: Text
    rule_parameters_hash: Text
    reasons: int = Field(ge=0)
    distinct_candidates: int = Field(ge=0)
    evaluated_outcomes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_tally(self) -> Self:
        if self.distinct_candidates > self.reasons:
            raise ValueError("a gate cannot refuse more candidates than it issued refusals")
        if self.evaluated_outcomes > self.distinct_candidates:
            raise ValueError("more outcomes than candidates to have outcomes about")
        return self

    @property
    def powered(self) -> bool:
        """Whether this gate has been tested enough to say anything about its value.

        Always false while nothing is evaluated, which is the current state of every gate. The
        report leans on this rather than showing a point estimate with an interval nobody reads:
        a gate that has refused nine trades and had none of them evaluated has told us nothing,
        and the honest output is `UNDERPOWERED` rather than a number.
        """
        return self.evaluated_outcomes >= MINIMUM_EVALUATED_OUTCOMES


#: Evaluated counterfactuals a gate needs before its value is reportable as anything but
#: `UNDERPOWERED`. Deliberately the same order of magnitude as the forward-evidence floor: the
#: question is the same shape, and a smaller number here would let this dataset make claims the
#: promotion ladder would refuse.
MINIMUM_EVALUATED_OUTCOMES = 30


def tally_by_gate(observations: Sequence[RejectedCandidate]) -> tuple[GateTally, ...]:
    """Per-gate counts, grouped by gate **and by the parameters in force**.

    Grouping on the parameter hash as well as the gate is the whole point: outcomes recorded
    before and after a threshold change are two datasets, and pooling them would judge a rule on
    evidence gathered under a different rule. Combining them has to be a deliberate act
    somewhere else, with its assumption stated.
    """
    reasons: dict[tuple[str, str], int] = {}
    candidates: dict[tuple[str, str], set[str]] = {}
    evaluated: dict[tuple[str, str], set[str]] = {}
    for observation in observations:
        for reason in observation.reasons:
            key = (reason.gate_id, reason.rule_parameters_hash)
            reasons[key] = reasons.get(key, 0) + 1
            candidates.setdefault(key, set()).add(observation.observation_id)
            if observation.state is CounterfactualState.EVALUATED:
                evaluated.setdefault(key, set()).add(observation.observation_id)
    return tuple(
        GateTally(
            gate_id=gate,
            rule_parameters_hash=parameters,
            reasons=count,
            distinct_candidates=len(candidates[(gate, parameters)]),
            evaluated_outcomes=len(evaluated.get((gate, parameters), set())),
        )
        for (gate, parameters), count in sorted(reasons.items())
    )


def as_rows(
    observation: RejectedCandidate,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Flatten one refusal into the parent row and its reason rows.

    Lives here rather than in `storage` so that layer keeps not importing `risk` -- the same
    boundary `PromotionRecord` holds against `research`, and for the same reason: the rules
    belong to the module that owns them, and storage stores what it is given.
    """
    parent: dict[str, object] = {
        "observation_id": observation.observation_id,
        "candidate_id": observation.candidate_id,
        "strategy_id": observation.strategy_id,
        "configuration_hash": observation.configuration_hash,
        "symbol": observation.symbol,
        "direction": observation.direction.value,
        "observed_at": observation.observed_at.astimezone(UTC).isoformat(),
        "session_date": observation.session_date.isoformat(),
        "reference_price": str(observation.reference_price),
        "hypothesis_id": observation.hypothesis_id,
        "market_regime_snapshot_id": observation.market_regime_snapshot_id,
        "catalyst_snapshot_id": observation.catalyst_snapshot_id,
        "anomaly_event_id": observation.anomaly_event_id,
        "portfolio_snapshot_id": observation.portfolio_snapshot_id,
        "state": observation.state.value,
        "unavailable_reason": observation.unavailable_reason,
    }
    for reason in observation.reasons:
        if not reason.evidence.recorded:
            # The legacy marker exists so rows written before #85 read back honestly. Live code
            # writing one would be claiming an absence it did not have, which is the one thing a
            # provenance field must never be able to say. Checked here rather than on the model
            # because reading a genuine legacy row has to construct the same type.
            raise ValueError(
                f"{reason.gate_id} carries LEGACY_UNRECORDED, which describes a row persisted "
                "before decision evidence was durable and cannot be written now"
            )
    reasons: list[dict[str, object]] = [
        {
            "observation_id": observation.observation_id,
            "gate_id": reason.gate_id,
            "gate_version": reason.gate_version,
            "rule_parameters_hash": reason.rule_parameters_hash,
            "detail": reason.detail,
            "evidence_kind": reason.evidence.kind.value,
            "evidence_schema_version": reason.evidence.schema_version,
            # Canonical, sorted, and hashed by the same rule that produced `evidence_identity`,
            # so two rows with the same basis are byte-identical here rather than merely
            # equivalent under some later parser.
            "evidence_payload": json.dumps(
                dict(sorted(reason.evidence.payload.items())),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "evidence_identity": reason.evidence.identity,
        }
        for reason in observation.reasons
    ]
    return parent, reasons


def reasons_from_rows(rows: Sequence[Mapping[str, object]]) -> tuple[RejectionReason, ...]:
    """Rebuild reasons from stored rows, with legacy rows honestly marked (#85).

    A row whose `evidence_kind` is NULL predates v13. It reads back as `LEGACY_UNRECORDED`
    rather than as `NONE`, because those are different facts: `NONE` says the veto was asked and
    had no structured context, and this says nobody asked. An analysis that pooled them would be
    treating "not recorded" as "recorded empty", which is how an absence becomes a measurement.

    Nothing is reconstructed from current state. Recomputing an expected session from today's
    calendar would answer what the rule *would* refuse now, not what it *did* refuse then.
    """
    rebuilt: list[RejectionReason] = []
    for row in rows:
        stored = row.get("evidence_kind")
        if stored is None:
            evidence = LEGACY_EVIDENCE
        else:
            payload = row.get("evidence_payload")
            evidence = DecisionEvidence(
                kind=EvidenceKind(str(stored)),
                schema_version=int(str(row["evidence_schema_version"])),
                payload=json.loads(str(payload)) if payload else {},
            )
        rebuilt.append(
            RejectionReason(
                gate_id=str(row["gate_id"]),
                gate_version=int(str(row["gate_version"])),
                rule_parameters_hash=str(row["rule_parameters_hash"]),
                detail=str(row["detail"]),
                evidence=evidence,
            )
        )
    return tuple(rebuilt)


def session_of(moment: datetime) -> date:
    """The Eastern session a refusal belongs to."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("a session date needs a timezone-aware moment")
    return moment.astimezone(XNYS_TIMEZONE).date()


__all__ = [
    "MINIMUM_EVALUATED_OUTCOMES",
    "as_rows",
    "reasons_from_rows",
    "candidate_id",
    "CandidateDirection",
    "CounterfactualState",
    "GateTally",
    "RejectedCandidate",
    "RejectionReason",
    "observation_id",
    "session_of",
    "tally_by_gate",
]
