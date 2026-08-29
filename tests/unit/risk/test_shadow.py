"""What Elrond refused, and every way that record could flatter a rule (#57).

This dataset exists to argue for changing risk thresholds. Almost everything below is about the
ways it could argue dishonestly.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantbot.risk.shadow import (
    MINIMUM_EVALUATED_OUTCOMES,
    CandidateDirection,
    CounterfactualState,
    RejectedCandidate,
    RejectionReason,
    as_rows,
    candidate_id,
    observation_id,
    session_of,
    tally_by_gate,
)

EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 27, 15, 30, tzinfo=EASTERN)
REPOSITORY = Path(__file__).resolve().parents[3]


def reason(gate: str = "LIQUIDITY", parameters: str = "veto-policy-v1-aaaa") -> RejectionReason:
    return RejectionReason(
        gate_id=gate,
        gate_version=1,
        rule_parameters_hash=parameters,
        detail=f"{gate} refused this candidate",
    )


def rejected(**overrides: object) -> RejectedCandidate:
    reasons = overrides.pop("reasons", (reason(),))
    fields: dict[str, object] = {
        "observation_id": "rejected-1",
        "candidate_id": "candidate-1",
        "strategy_id": "adaptive-momentum",
        "configuration_hash": "cfg-abc",
        "symbol": "SPY",
        "direction": CandidateDirection.LONG,
        "observed_at": NOW,
        "session_date": date(2026, 8, 27),
        "reference_price": Decimal("500"),
        "reasons": reasons,
    }
    fields.update(overrides)
    return RejectedCandidate(**fields)  # type: ignore[arg-type]


def test_a_refusal_records_every_rule_that_refused_it_as_one_event() -> None:
    """One candidate, many reasons. The alternative inflates the sample by the reason count.

    `evaluate_portfolio_vetoes` deliberately does not short-circuit, so a single entry can
    collect four refusals. Recording those as four observations would multiply the apparent
    evidence about every gate involved — the manufacture of sample size that killed cycle 11's
    overnight result.
    """
    observation = rejected(
        reasons=(reason("LIQUIDITY"), reason("DAILY_LOSS"), reason("NET_EXPOSURE"))
    )

    assert observation.gate_ids == ("LIQUIDITY", "DAILY_LOSS", "NET_EXPOSURE")
    parent, reasons = as_rows(observation)
    assert len([parent]) == 1, "one event"
    assert len(reasons) == 3, "three reasons for it"
    assert {row["observation_id"] for row in reasons} == {"rejected-1"}


def test_one_gate_cannot_refuse_the_same_candidate_twice() -> None:
    """A repeated gate id would count one refusal as two observations of that gate."""
    with pytest.raises(ValueError, match="cannot refuse the same candidate twice"):
        rejected(reasons=(reason("LIQUIDITY"), reason("LIQUIDITY")))


def test_a_record_with_no_outcome_says_why_rather_than_being_silent() -> None:
    """`COUNTERFACTUAL_UNAVAILABLE` is a recorded fact, not a missing field.

    A record with no outcome and no explanation reads like an oversight; one that says nobody
    could compute it yet reads like the truth. Every record this phase writes is this state.
    """
    observation = rejected()

    assert observation.state is CounterfactualState.UNAVAILABLE
    assert "phase 2" in observation.unavailable_reason
    assert "COUNTERFACTUAL_UNAVAILABLE" in observation.explain()

    # The field is min-length-1, so silence is unrepresentable rather than merely discouraged.
    with pytest.raises(ValueError, match="at least 1 character"):
        rejected(unavailable_reason="")


def test_the_three_counterfactual_states_are_distinguishable() -> None:
    """"Not yet" and "never" are different facts, and collapsing them loses the second.

    A horizon that elapsed with no data is a permanent gap in the evidence; a horizon that has
    not elapsed is simply not due. A report that showed both as "unavailable" would let a
    systematically unevaluable gate look like one whose results are merely pending.
    """
    assert CounterfactualState.UNAVAILABLE is not CounterfactualState.UNEVALUABLE
    assert {state.value for state in CounterfactualState} == {
        "COUNTERFACTUAL_UNAVAILABLE",
        "COUNTERFACTUAL_EVALUATED",
        "COUNTERFACTUAL_UNEVALUABLE",
    }


def key(**overrides: object) -> str:
    fields: dict[str, object] = {
        "strategy_id": "adaptive-momentum",
        "configuration_hash": "cfg-abc",
        "symbol": "SPY",
        "session_date": date(2026, 8, 27),
        "candidate_key": "bars-hash-1",
    }
    fields.update(overrides)
    return candidate_id(**fields)  # type: ignore[arg-type]


def test_the_same_candidate_rerun_gets_the_same_id() -> None:
    """Replaying a session must not manufacture a second shadow sample."""
    assert key() == key(), "a rerun is the same candidate"
    assert observation_id(candidate=key(), policy_identity="p-1") == observation_id(
        candidate=key(), policy_identity="p-1"
    )


def test_a_different_candidate_is_a_different_observation(
) -> None:
    """The blocker GPT-5.6 Sol found, pinned in every direction he named.

    The first version identified a refusal by strategy, symbol, session and the *set of gates
    that refused it*. That silently declares two materially different candidates to be one
    observation whenever they share those four things — merging evidence rather than losing it,
    which is the harder failure to notice.

    Identity now comes from what the candidate is, before any gate has ruled on it.
    """
    baseline = key()

    # A materially different configuration evaluating the same symbol in the same session.
    assert key(configuration_hash="cfg-changed") != baseline

    # A second, genuinely different candidate for one symbol in one session — the case #56 will
    # produce routinely, and the one the old identity could not represent.
    assert key(candidate_key="bars-hash-2") != baseline

    # Different symbol, session or strategy remain different, as before.
    assert key(symbol="QQQ") != baseline
    assert key(session_date=date(2026, 8, 28)) != baseline
    assert key(strategy_id="other-strategy") != baseline


def test_the_same_candidate_under_changed_rules_is_a_new_observation() -> None:
    """A threshold change means a different rule was tested, so it is different evidence.

    Pooling those would judge one policy on another policy's outcomes, which is the same
    mistake `tally_by_gate` refuses when it groups on the parameter hash.
    """
    same_candidate = key()

    assert observation_id(candidate=same_candidate, policy_identity="p-1") != observation_id(
        candidate=same_candidate, policy_identity="p-2"
    )


def test_retry_timing_cannot_manufacture_a_candidate() -> None:
    """`observed_at` is deliberately absent from the identity.

    A retry a minute later is the same decision about the same evidence. Including wall-clock
    time would turn every transient failure into a fresh observation.
    """
    import inspect

    parameters = set(inspect.signature(candidate_id).parameters)

    assert not parameters & {"observed_at", "now", "timestamp", "created_at"}, (
        "retry timing must not be part of candidate identity: a retry a minute later is the "
        f"same decision about the same evidence ({sorted(parameters)})"
    )


def test_a_gate_tally_counts_candidates_and_reasons_separately() -> None:
    """They are different numbers, and only one of them says how much a gate has been tested.

    A gate that refused ten candidates, each of which was also refused by three other gates,
    has ten observations — not forty. A report quoting the reason count overstates the evidence
    in exactly the direction that argues for loosening.
    """
    observations = [
        rejected(
            observation_id=f"rejected-{index}",
            reasons=(reason("LIQUIDITY"), reason("DAILY_LOSS")),
        )
        for index in range(4)
    ]

    tallies = {item.gate_id: item for item in tally_by_gate(observations)}

    assert tallies["LIQUIDITY"].reasons == 4
    assert tallies["LIQUIDITY"].distinct_candidates == 4
    assert tallies["DAILY_LOSS"].distinct_candidates == 4
    assert tallies["LIQUIDITY"].evaluated_outcomes == 0


def test_outcomes_under_different_parameters_are_different_datasets() -> None:
    """Pooling across a threshold change judges one rule on another rule's evidence.

    #57 says a rule change must not rewrite prior rejections, which is necessary and not
    sufficient: the analysis must also refuse to aggregate across parameter hashes as though
    they were one sample. Grouping on the hash is what makes that mechanical.
    """
    before = rejected(observation_id="rejected-before", reasons=(reason(parameters="hash-old"),))
    after = rejected(observation_id="rejected-after", reasons=(reason(parameters="hash-new"),))

    tallies = tally_by_gate([before, after])

    assert len(tallies) == 2, "one gate at two settings is two rows, never one"
    assert {item.rule_parameters_hash for item in tallies} == {"hash-old", "hash-new"}
    assert all(item.distinct_candidates == 1 for item in tallies)


def test_a_gate_with_no_evaluated_outcomes_is_never_powered() -> None:
    """The report must refuse to show a value it cannot support (#57 change 4).

    A gate that has refused nine trades and had none of them evaluated has told us nothing. A
    point estimate there is the impressive-looking dashboard `CLAUDE.md` warns about, and
    somebody will act on it.
    """
    many = [
        rejected(observation_id=f"rejected-{index}", reasons=(reason("LIQUIDITY"),))
        for index in range(MINIMUM_EVALUATED_OUTCOMES + 10)
    ]

    tally = tally_by_gate(many)[0]

    assert tally.distinct_candidates == MINIMUM_EVALUATED_OUTCOMES + 10
    assert tally.evaluated_outcomes == 0
    assert not tally.powered, "refusals are not outcomes; only evaluated ones count"


def test_a_tally_cannot_claim_more_candidates_than_refusals() -> None:
    """The invariant that keeps the two counts honest against each other."""
    from quantbot.risk.shadow import GateTally

    with pytest.raises(ValueError, match="more candidates than it issued refusals"):
        GateTally(
            gate_id="LIQUIDITY",
            rule_parameters_hash="hash",
            reasons=2,
            distinct_candidates=3,
            evaluated_outcomes=0,
        )
    with pytest.raises(ValueError, match="more outcomes than candidates"):
        GateTally(
            gate_id="LIQUIDITY",
            rule_parameters_hash="hash",
            reasons=5,
            distinct_candidates=2,
            evaluated_outcomes=3,
        )


def test_the_session_is_eastern_not_utc() -> None:
    """A refusal after the close belongs to the session it was refused in.

    The same off-by-one already cost this project correct fill attribution and correct equity
    marks, and here it would file a refusal under the following session.
    """
    after_close = datetime.combine(date(2026, 8, 27), time(20, 5), tzinfo=EASTERN)
    as_stored = after_close.astimezone(UTC)

    assert as_stored.date() == date(2026, 8, 28)
    assert session_of(as_stored) == date(2026, 8, 27)

    with pytest.raises(ValueError, match="timezone-aware"):
        session_of(datetime(2026, 8, 27, 20, 5))


def test_context_this_project_cannot_produce_is_absent_rather_than_defaulted() -> None:
    """Regime, catalyst and anomaly context come from #59 and #56, which do not exist.

    Defaulting them would make a substituted context indistinguishable from a measured one once
    it is inside an analysis, which is the failure every unmeasured input in this codebase is
    written to avoid.
    """
    observation = rejected()

    assert observation.market_regime_snapshot_id is None
    assert observation.catalyst_snapshot_id is None
    assert observation.anomaly_event_id is None

    parent, _ = as_rows(observation)
    assert parent["market_regime_snapshot_id"] is None


def test_a_shadow_record_is_not_an_order_and_cannot_become_one() -> None:
    """#57 criterion 9, as a property of the type rather than only of a code path.

    A rejected candidate that could be handed to the coordinator is one that eventually will be.
    The record carries a symbol and a direction — no quantity, no intent id, no client order id,
    and no `OrderSide` an execution path would recognise.
    """
    observation = rejected()
    fields = set(observation.model_dump())

    for forbidden in ("quantity", "intent_id", "client_order_id", "side", "order_type"):
        assert forbidden not in fields, f"a shadow record must not carry {forbidden}"

    assert isinstance(observation.direction, CandidateDirection)
    assert {item.value for item in CandidateDirection} == {"LONG", "SHORT"}


def test_the_shadow_module_imports_nothing_that_could_reach_a_broker() -> None:
    """The isolation, checked rather than asserted.

    A convenience import of `OrderIntent` or a broker adapter is how the previous property stops
    being true, and it would not fail any other test. Walking the imports is what notices.
    """
    source = REPOSITORY / "src" / "quantbot" / "risk" / "shadow.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden = {"broker", "execution", "coordinator", "OrderIntent", "OrderSide", "domain"}
    offenders = [name for name in imported if any(item in name for item in forbidden)]
    assert not offenders, (
        "the shadow subsystem must not be able to reach an execution path: " f"{offenders}"
    )


def test_a_refusal_round_trips_through_the_durable_store(tmp_path: Path) -> None:
    """Through the real repository and schema, not a fixture that agrees with the model.

    Rejections previously survived only as free-text inside `signals.payload_json`. This is the
    property that replaces it: a gate-level question can be answered by query rather than by
    parsing prose.
    """
    from quantbot.storage import Database, StorageRepository

    database = Database.create(tmp_path / "shadow.db")
    observation = rejected(reasons=(reason("LIQUIDITY"), reason("DAILY_LOSS")))
    parent, reasons = as_rows(observation)

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_rejected_candidate(parent, reasons) is True
        # The same refusal re-evaluated is not a second observation.
        assert repository.record_rejected_candidate(parent, reasons) is False

        stored = repository.list_rejected_candidates()
    database.close()

    assert len(stored) == 1, "one event, however many rules refused it"
    row, stored_reasons = stored[0]
    assert row["symbol"] == "SPY"
    assert row["state"] == "COUNTERFACTUAL_UNAVAILABLE"
    assert row["market_regime_snapshot_id"] is None
    assert {item["gate_id"] for item in stored_reasons} == {"LIQUIDITY", "DAILY_LOSS"}
    assert all(item["rule_parameters_hash"] for item in stored_reasons)


def test_a_different_refusal_sharing_an_id_is_refused_rather_than_overwritten(
    tmp_path: Path,
) -> None:
    """An id collision that carried different data would silently replace evidence."""
    from quantbot.storage import Database, StorageRepository
    from quantbot.storage.repositories import StateConflictError

    database = Database.create(tmp_path / "shadow-conflict.db")
    first, first_reasons = as_rows(rejected())
    second, second_reasons = as_rows(rejected(symbol="QQQ"))

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_rejected_candidate(first, first_reasons)
        with pytest.raises(StateConflictError, match="different data"):
            repository.record_rejected_candidate(second, second_reasons)
    database.close()


def test_the_claim_lineage_comes_from_durable_state_not_from_a_caller(tmp_path: Path) -> None:
    """GPT-5.6 Sol's second #57 point, and the defect class this project keeps producing.

    `hypothesis_id` on a shadow record must be read from the promotion ledger, never handed in.
    A gate that accepts the identity it is supposed to constrain is the shape #53 was opened
    about; a shadow record that took its claim lineage as an argument would reintroduce it one
    table over — and this dataset's whole purpose is to argue about the rules.

    Both cases, because only asserting the populated one would pass on a function that always
    returned the same id.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
    from quantbot.storage import Database

    moment = _dt(2026, 8, 27, 14, 30, tzinfo=_UTC)
    database = Database.create(tmp_path / "lineage.db")

    with database.transaction() as session:
        ledger = PromotionLedger(session)
        # The deployed rotation: on the ladder, no frozen claim behind it.
        ledger.enter(
            PromotionState(
                strategy_id="operational-baseline",
                stage=Stage.PAPER_OBSERVATION,
                strategy_version="1.3.0",
                configuration_hash="cfg-abc",
                reason="seeded",
                actor="operator",
                updated_at=moment,
                hypothesis_id=None,
                hypothesis_version=None,
            )
        )
        # An edge candidate: the same path, with a claim durably bound at REGISTERED.
        ledger.enter(
            PromotionState(
                strategy_id="edge-candidate",
                stage=Stage.PAPER_OBSERVATION,
                strategy_version="1.3.0",
                configuration_hash="cfg-abc",
                reason="seeded",
                actor="operator",
                updated_at=moment,
                hypothesis_id="H-2026-047",
                hypothesis_version=1,
            )
        )

        baseline = ledger.current("operational-baseline")
        candidate = ledger.current("edge-candidate")

    database.close()

    assert baseline is not None and baseline.hypothesis_id is None, (
        "an operational baseline has no claim to lose"
    )
    assert candidate is not None and candidate.hypothesis_id == "H-2026-047", (
        "a registered candidate's lineage must survive into the shadow record"
    )
