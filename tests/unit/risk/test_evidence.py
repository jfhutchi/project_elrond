"""What a refusal rested on, and the ways that fact can be lost between decision and ledger (#85).

Every test here refuses to read `detail`. That is the point of the issue: the structured fields
existed on the live decision and reached storage as prose, so an analysis wanting to group by
decision basis had to parse English or recompute the world. A test satisfied by a substring in
`detail` would be measuring exactly the thing being replaced.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.risk.evidence import (
    EVIDENCE_FIELDS,
    LEGACY_EVIDENCE,
    DecisionEvidence,
    EvidenceKind,
)
from quantbot.risk.shadow import (
    CandidateDirection,
    RejectedCandidate,
    RejectionReason,
    as_rows,
    reasons_from_rows,
)
from quantbot.risk.vetoes import VetoName, VetoRejection

NOW = datetime(2026, 8, 27, 20, 5, tzinfo=UTC)

#: Fields on `VetoRejection` that identify the *rule*, as opposed to what it was looking at.
#: Everything outside this set is decision context and must be claimed by an evidence kind.
RULE_FIELDS = frozenset({"veto", "policy_version", "rule_parameters_hash", "reason", "unmeasured"})


def freshness_rejection(**overrides: object) -> VetoRejection:
    fields: dict[str, object] = {
        "veto": VetoName.STALE_MARKET_DATA,
        "policy_version": 1,
        "rule_parameters_hash": "veto-policy-v1-aaaa",
        "reason": "the newest SPY observation is from a session the market has moved past",
        "freshness_policy_identity": "freshness-policy-v1-bbbb",
        "expected_session": date(2026, 8, 27),
        "observed_session": date(2026, 8, 25),
        "market_state": "CLOSED",
    }
    fields.update(overrides)
    return VetoRejection(**fields)  # type: ignore[arg-type]


def rejected(reasons: tuple[RejectionReason, ...]) -> RejectedCandidate:
    return RejectedCandidate(
        observation_id="rejected-1",
        candidate_id="candidate-1",
        strategy_id="adaptive-momentum",
        configuration_hash="cfg-abc",
        symbol="SPY",
        direction=CandidateDirection.LONG,
        observed_at=NOW,
        session_date=date(2026, 8, 27),
        reference_price=Decimal("500"),
        reasons=reasons,
    )


def as_reason(rejection: VetoRejection) -> RejectionReason:
    return RejectionReason(
        gate_id=rejection.veto.value,
        gate_version=rejection.policy_version,
        rule_parameters_hash=rejection.rule_parameters_hash,
        detail=rejection.reason,
        evidence=rejection.decision_evidence(),
    )


def round_trip(observation: RejectedCandidate, tmp_path: Path) -> tuple[RejectionReason, ...]:
    """Through the real repository and schema, not a fixture that agrees with the model."""
    from quantbot.storage import Database, StorageRepository

    database = Database.create(tmp_path / "shadow.db")
    parent, reasons = as_rows(observation)
    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_rejected_candidate(parent, reasons) is True
        stored = repository.list_rejected_candidates()
    database.close()

    assert len(stored) == 1
    return reasons_from_rows(stored[0][1])


def test_every_structured_field_on_a_rejection_is_claimed_by_an_evidence_kind() -> None:
    """The guard that stops the *next* veto losing its evidence the way these two did.

    #85 asks for a generic contract rather than a column per field, precisely so the next veto
    with structured context does not silently drop it. A contract is only generic if adding a
    field to `VetoRejection` without declaring where it belongs fails, so this compares the
    model's own fields against the union of the declared kinds.
    """
    declared = frozenset().union(*EVIDENCE_FIELDS.values())
    structured = frozenset(VetoRejection.model_fields) - RULE_FIELDS

    unclaimed = structured - declared
    assert not unclaimed, (
        f"{sorted(unclaimed)} is decision context on VetoRejection that no EvidenceKind claims, "
        "so it would be recorded on the live decision and dropped at the ledger"
    )
    stale = declared - structured
    assert not stale, f"{sorted(stale)} is declared as evidence but no longer exists on the model"


def test_a_freshness_refusal_round_trips_with_the_exact_session_it_saw(tmp_path: Path) -> None:
    """The same policy refuses a missing Tuesday and a missing Thursday. Those are two datasets."""
    rebuilt = round_trip(rejected((as_reason(freshness_rejection()),)), tmp_path)

    assert len(rebuilt) == 1
    evidence = rebuilt[0].evidence
    assert evidence.kind is EvidenceKind.MARKET_FRESHNESS
    assert evidence.payload == {
        "freshness_policy_identity": "freshness-policy-v1-bbbb",
        "expected_session": "2026-08-27",
        "observed_session": "2026-08-25",
        "market_state": "CLOSED",
    }
    assert evidence.recorded


def test_a_correlation_refusal_round_trips_with_the_matrix_it_clustered_on(
    tmp_path: Path,
) -> None:
    """`rule_parameters_hash` names the threshold. It does not name the coefficients (#58/#84)."""
    correlation = VetoRejection(
        veto=VetoName.CORRELATION_CONCENTRATION,
        policy_version=1,
        rule_parameters_hash="veto-policy-v1-aaaa",
        reason="QQQ, SPY move together and would exceed the cluster ceiling",
        dependence_snapshot_identity="dependence-v1-cccc",
    )

    rebuilt = round_trip(rejected((as_reason(correlation),)), tmp_path)

    assert rebuilt[0].evidence.kind is EvidenceKind.DEPENDENCE
    assert rebuilt[0].evidence.payload == {"dependence_snapshot_identity": "dependence-v1-cccc"}


def test_the_same_rule_under_different_evidence_stays_distinguishable(tmp_path: Path) -> None:
    """The property the whole issue rests on, asserted without reading a word of `detail`.

    Two refusals by the same gate under the same parameters, differing only in which session was
    missing. Before this they were indistinguishable in the ledger except as prose, so a
    counterfactual study grouping by `rule_parameters_hash` would pool them.
    """
    tuesday = as_reason(freshness_rejection(observed_session=date(2026, 8, 25)))
    thursday = as_reason(freshness_rejection(observed_session=date(2026, 8, 20)))

    assert tuesday.rule_parameters_hash == thursday.rule_parameters_hash
    assert tuesday.evidence.identity != thursday.evidence.identity

    _, rows = as_rows(rejected((tuesday,)))
    _, other = as_rows(rejected((thursday,)))
    assert rows[0]["evidence_identity"] != other[0]["evidence_identity"]

    rebuilt = round_trip(rejected((tuesday,)), tmp_path)
    assert rebuilt[0].evidence.payload["observed_session"] == "2026-08-25"


def test_a_row_written_before_this_existed_says_so_rather_than_claiming_nothing_happened(
    tmp_path: Path,
) -> None:
    """`NONE` is a measurement; `LEGACY_UNRECORDED` is the absence of one.

    A v12 row has NULL evidence columns. Reading that back as `NONE` would say the veto was
    asked and had no structured context, when in truth nobody asked -- and an analysis pooling
    the two would be treating "not recorded" as "recorded empty".

    Nothing is reconstructed. Recomputing an expected session from today's calendar answers what
    the rule *would* refuse now, not what it *did* refuse then.
    """
    legacy = reasons_from_rows(
        [
            {
                "gate_id": "STALE_MARKET_DATA",
                "gate_version": 1,
                "rule_parameters_hash": "veto-policy-v1-aaaa",
                "detail": "recorded before decision evidence was durable",
                "evidence_kind": None,
                "evidence_schema_version": None,
                "evidence_payload": None,
                "evidence_identity": None,
            }
        ]
    )

    assert legacy[0].evidence is LEGACY_EVIDENCE
    assert legacy[0].evidence.kind is EvidenceKind.LEGACY_UNRECORDED
    assert legacy[0].evidence.recorded is False
    assert legacy[0].evidence.payload == {}


def test_live_code_cannot_write_the_legacy_marker() -> None:
    """A provenance field must never be able to claim an absence it did not have."""
    forged = RejectionReason(
        gate_id="STALE_MARKET_DATA",
        gate_version=1,
        rule_parameters_hash="veto-policy-v1-aaaa",
        detail="pretending to predate the schema",
        evidence=LEGACY_EVIDENCE,
    )

    with pytest.raises(ValueError, match="cannot be written now"):
        as_rows(rejected((forged,)))


def test_a_half_filled_payload_is_refused_rather_than_stored() -> None:
    """"Unknown future evidence fields fail explicitly rather than silently disappearing."

    Exact field sets, not minimums. A missing key means a writer that half-populated the
    payload; an extra key means one writing a field no reader expects. Both are version changes
    and both belong at the boundary, not in an analysis that quietly skipped the rows.
    """
    with pytest.raises(ValueError, match="missing"):
        DecisionEvidence(
            kind=EvidenceKind.MARKET_FRESHNESS,
            payload={"freshness_policy_identity": "freshness-policy-v1-bbbb"},
        )
    with pytest.raises(ValueError, match="unexpected"):
        DecisionEvidence(
            kind=EvidenceKind.DEPENDENCE,
            payload={
                "dependence_snapshot_identity": "dependence-v1-cccc",
                "a_field_from_the_future": "surprise",
            },
        )


def test_the_identity_moves_with_the_payload_the_kind_and_the_version() -> None:
    """Grouping by basis only works if the basis is what the hash covers."""
    base = freshness_rejection().decision_evidence()

    assert base.identity.startswith("evidence-v1-")
    assert base.identity != freshness_rejection(market_state="OPEN").decision_evidence().identity
    assert (
        base.identity
        != DecisionEvidence(
            kind=base.kind, schema_version=2, payload=base.payload
        ).identity
    )
    # Key order in the mapping is not part of the basis.
    reordered = DecisionEvidence(kind=base.kind, payload=dict(reversed(list(base.payload.items()))))
    assert reordered.identity == base.identity


def test_an_unmeasurable_veto_records_no_evidence_rather_than_a_guess() -> None:
    """A veto that refused *because* it could not measure has nothing contemporaneous to record.

    The kind is derived from which fields are populated rather than from the veto name, so
    `STALE_MARKET_DATA` refusing for want of a calendar reports `NONE` rather than a freshness
    payload full of blanks that a later reader would take for measurements.
    """
    unmeasurable = VetoRejection(
        veto=VetoName.STALE_MARKET_DATA,
        policy_version=1,
        rule_parameters_hash="veto-policy-v1-aaaa",
        reason="no exchange calendar was available, so what the feed owes is unknown",
        unmeasured="FRESHNESS_METADATA_UNAVAILABLE",
    )

    evidence = unmeasurable.decision_evidence()
    assert evidence.kind is EvidenceKind.NONE
    assert evidence.payload == {}
    assert evidence.recorded, "NONE is a measurement, and must not read as an absent one"


def test_the_stored_payload_is_canonical_json(tmp_path: Path) -> None:
    """Two rows with the same basis are byte-identical, not merely equivalent under a parser."""
    _, rows = as_rows(rejected((as_reason(freshness_rejection()),)))
    stored = str(rows[0]["evidence_payload"])

    assert stored == json.dumps(json.loads(stored), sort_keys=True, separators=(",", ":"))
    assert list(json.loads(stored)) == sorted(json.loads(stored))


def test_the_production_path_attaches_evidence_to_every_reason(tmp_path: Path) -> None:
    """The composition in `runners._record_rejection`, not a helper that agrees with it.

    Written after a mutation check found the gap: deleting `evidence=...` from the runner left
    every other test in this file green, because they all built their reasons locally. That is
    the defect class this branch keeps meeting -- a control that is correct in isolation and
    never reached in production -- and #85 is precisely a report of evidence being produced and
    then dropped on the way to the ledger.

    `_record_rejection` reads only `_identity` and `_veto_policy` off `self`, so it is called
    unbound against a stand-in, the pattern `test_pending_reservations.py` already uses here.
    """
    from types import SimpleNamespace

    from quantbot.operations.runners import AdaptiveMomentumRunner
    from quantbot.risk.vetoes import PortfolioVetoDecision, PortfolioVetoPolicy
    from quantbot.storage import Database, StorageRepository

    correlation = VetoRejection(
        veto=VetoName.CORRELATION_CONCENTRATION,
        policy_version=1,
        rule_parameters_hash="veto-policy-v1-aaaa",
        reason="QQQ, SPY move together and would exceed the cluster ceiling",
        dependence_snapshot_identity="dependence-v1-cccc",
    )
    decision = PortfolioVetoDecision(
        symbol="SPY",
        policy_version=1,
        rule_parameters_hash="veto-policy-v1-aaaa",
        evaluated=(VetoName.STALE_MARKET_DATA, VetoName.CORRELATION_CONCENTRATION),
        rejections=(freshness_rejection(), correlation),
    )
    runner = SimpleNamespace(
        _identity=SimpleNamespace(strategy_id="adaptive-momentum", configuration_hash="cfg-abc"),
        _veto_policy=PortfolioVetoPolicy(),
    )

    database = Database.create(tmp_path / "production.db")
    with database.transaction() as session:
        repository = StorageRepository(session)
        AdaptiveMomentumRunner._record_rejection(  # noqa: SLF001 - the composition is the subject
            runner,
            repository,
            symbol="SPY",
            reference_price=Decimal("500"),
            observed_at=NOW,
            candidate_key="entry",
            vetoes=decision,
        )
        stored = repository.list_rejected_candidates()
    database.close()

    assert len(stored) == 1, "one event, however many rules refused it"
    by_gate = {reason.gate_id: reason for reason in reasons_from_rows(stored[0][1])}

    assert by_gate["STALE_MARKET_DATA"].evidence.payload == {
        "freshness_policy_identity": "freshness-policy-v1-bbbb",
        "expected_session": "2026-08-27",
        "observed_session": "2026-08-25",
        "market_state": "CLOSED",
    }
    assert by_gate["CORRELATION_CONCENTRATION"].evidence.payload == {
        "dependence_snapshot_identity": "dependence-v1-cccc"
    }
    assert all(reason.evidence.recorded for reason in by_gate.values())


def test_a_refusal_recorded_before_v13_survives_the_migration_unchanged(tmp_path: Path) -> None:
    """A real v12 row through a real migration, not a NULL dict standing in for one.

    #85 is explicit that prior rows must not be rewritten with inferred evidence. The
    temptation is real: the gate and the parameters are known, so an expected session could be
    recomputed from today's calendar and the row would look complete. It would also be a
    fabrication -- it answers what the rule *would* refuse now, not what it *did* refuse then,
    and once written nothing downstream can tell the difference.

    So this asserts twice: the refusal is still there and unaltered, and its evidence reads as
    absent rather than as empty.
    """
    from alembic import command
    from alembic.config import Config

    from quantbot.storage import Database, StorageRepository
    from quantbot.storage.schema import SCHEMA_VERSION

    path = tmp_path / "v12.db"
    config = Config()
    config.set_main_option(
        "script_location", str(Path("src/quantbot/storage/migrations").resolve())
    )
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path.as_posix()}")
    command.upgrade(config, "0012")

    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO rejected_candidates (observation_id, candidate_id, strategy_id, "
                "configuration_hash, symbol, direction, observed_at, session_date, "
                "reference_price, state, unavailable_reason) VALUES ('obs-v12', 'cand-v12', "
                "'adaptive-momentum', 'cfg-abc', 'SPY', 'LONG', '2026-08-27T20:05:00Z', "
                "'2026-08-27', '500', 'COUNTERFACTUAL_UNAVAILABLE', 'horizon has not elapsed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rejection_reasons (observation_id, gate_id, gate_version, "
                "rule_parameters_hash, detail) VALUES ('obs-v12', 'STALE_MARKET_DATA', 1, "
                "'veto-policy-v1-aaaa', 'refused before decision evidence was durable')"
            )
        )
    engine.dispose()

    database = Database.migrate(path)
    try:
        with database.transaction() as session:
            stored = StorageRepository(session).list_rejected_candidates()
        with database.engine.connect() as connection:
            marker = connection.exec_driver_sql(
                "SELECT version FROM schema_version"
            ).fetchone()
    finally:
        database.close()

    assert marker is not None and marker[0] == SCHEMA_VERSION
    assert len(stored) == 1, "the migration must not lose a refusal"

    rebuilt = reasons_from_rows(stored[0][1])
    assert len(rebuilt) == 1
    assert rebuilt[0].gate_id == "STALE_MARKET_DATA"
    assert rebuilt[0].detail == "refused before decision evidence was durable"
    assert rebuilt[0].evidence.kind is EvidenceKind.LEGACY_UNRECORDED
    assert rebuilt[0].evidence.recorded is False
    assert rebuilt[0].evidence.payload == {}, "nothing was invented for a row that had nothing"
