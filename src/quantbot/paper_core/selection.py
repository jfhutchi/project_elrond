"""Deterministic PAPER selection and existing-outbox reservation accounting."""

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from quantbot.paper_core.contracts import (
    CoreConfig,
    Decision,
    ModelArtifact,
    ObservationCohort,
    ReconciliationProof,
    SelectionEvidence,
)
from quantbot.paper_core.entry_policy import EntryPolicy, entry_policy_in_session, symbols
from quantbot.paper_core.store import CoreStore, table
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError, StorageRepository

SIGNAL_OR_EXPLORE_SESSION_LIMIT = 6


def boundary_id(config: CoreConfig, cohort: ObservationCohort) -> str:
    cutoff = cohort.source_end
    if any(o.feature_context == "OPENING_QUOTE_GAP_V1" for o in cohort.observations):
        # Live quote captures vary within a boundary; they must not create new
        # reservations after a restart or a later quote in the same five minutes.
        cutoff = cutoff.replace(minute=cutoff.minute // 5 * 5, second=0, microsecond=0)
    return content_id(
        {
            "account": config.account_id,
            "owner": config.owner,
            "session_open": cohort.session_open,
            "session_close": cohort.session_close,
            "source_end": cutoff,
        }
    )


def cohort_document(store: CoreStore, cohort: ObservationCohort) -> dict[str, Any]:
    return {"selection_cohort": cohort, "config_id": store.config_id}


def record_cohort(store: CoreStore, cohort: ObservationCohort) -> str:
    from quantbot.paper_core.store import put

    validate_cohort(store.config, cohort, entry_policy=store.entry_policy)
    document = cohort_document(store, cohort)
    identity = content_id(document)
    with store.database.transaction() as session:
        for observation in cohort.observations:
            put(session, "core_observations", observation.id, observation)
        put(session, "core_provider_acquisitions", identity, document)
    return identity


def validate_cohort(
    config: CoreConfig, cohort: ObservationCohort, *, entry_policy: EntryPolicy | None = None
) -> None:
    members = [o.symbol for o in cohort.observations] + [s for s, _ in cohort.unavailable]
    quote_context = bool(cohort.observations) and all(
        o.feature_context == "OPENING_QUOTE_GAP_V1" for o in cohort.observations
    )
    if entry_policy is not None and entry_policy.dynamic:
        d = cohort.discovery
        if (
            d is None
            or len(d.symbols) != len(set(d.symbols))
            or set(d.symbols) != set(members)
            or {s.symbol for s in d.samples} != set(members)
            or len(d.samples) != len(members)
            or not d.scan_receipt_ids
            or any(s.scan_receipt_id not in d.scan_receipt_ids for s in d.samples)
            or d.eligible_count < len(members)
            or not 0 <= (d.recorded_at - d.catalog_at).total_seconds() <= 3600
            or quote_context
            and not 0 <= (cohort.source_end - d.catalog_at).total_seconds() <= 3600
            or not (
                0 <= (cohort.source_end - d.recorded_at).total_seconds() <= 120
                if quote_context
                else 0 <= (d.recorded_at - cohort.source_end).total_seconds() <= 120
            )
            or any(not entry_policy.permits(s) for s in members)
            or any(
                not 0 <= (d.recorded_at - s.source_at).total_seconds() <= 90
                or not 0 <= (d.recorded_at - s.received_at).total_seconds() <= 90
                or quote_context
                and (
                    not 0 <= (cohort.source_end - s.source_at).total_seconds() <= 90
                    or not 0 <= (cohort.source_end - s.received_at).total_seconds() <= 90
                )
                or s.dollar_volume <= 0
                or not 0 <= s.spread_bps <= 10
                for s in d.samples
            )
        ):
            raise ValueError("discovery cohort requires exact fresh captured membership")
    if (
        len(members) != len(set(members))
        or len({o.feature_context for o in cohort.observations}) > 1
        or set(members) != set(cohort_symbols(config, cohort, entry_policy))
        or any(not reason for _, reason in cohort.unavailable)
        or any(
            t.tzinfo is None for t in (cohort.source_end, cohort.session_open, cohort.session_close)
        )
        or not cohort.session_open <= cohort.source_end < cohort.session_close
        or not quote_context
        and cohort.source_end.timestamp() % 300 != 0
        or any(
            o.source_end != cohort.source_end
            or o.session_open != cohort.session_open
            or o.session_close != cohort.session_close
            for o in cohort.observations
        )
    ):
        raise ValueError("cohort must capture the complete universe at one verified boundary")


def cohort_symbols(
    config: CoreConfig, cohort: ObservationCohort, policy: EntryPolicy | None
) -> tuple[str, ...]:
    if policy is not None and policy.dynamic:
        if cohort.discovery is None:
            raise ValueError("discovery cohort evidence required")
        return cohort.discovery.symbols
    return symbols(config, policy)


def reservation_state(
    store: CoreStore,
    session: Session,
    cohort: ObservationCohort,
    *,
    exclude_intent: str | None = None,
) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...], frozenset[str]]:
    """Only definitive unsent proof releases usage; completion never releases daily usage."""
    from quantbot.paper_core.settlement import definitively_unsent_many

    connection = session.connection()
    driver = connection.connection.driver_connection
    assert driver is not None, "SQLITE_DRIVER_UNAVAILABLE"
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN")

    links, intents, decisions = (
        table(n) for n in ("core_intent_links", "order_intents", "core_decisions")
    )
    repo = StorageRepository(session)
    daily: Counter[str] = Counter()
    prior: Counter[str] = Counter()
    boundaries = set()
    # Hydrate scalar selection metadata only, rather than every historic cohort's features.
    rows = list(
        session.execute(
            select(
                links.c.intent_id,
                intents.c.state,
                func.json_extract(decisions.c.payload, "$.symbol"),
                func.json_extract(decisions.c.payload, "$.recorded_at_utc"),
                func.json_extract(decisions.c.payload, "$.selection.cohort.session_open"),
                func.json_extract(decisions.c.payload, "$.selection.cohort.session_close"),
                func.json_extract(decisions.c.payload, "$.selection.boundary_id"),
            )
            .outerjoin(intents, intents.c.intent_id == links.c.intent_id)
            .join(decisions, decisions.c.id == links.c.decision_id)
            .where(
                links.c.role == "ENTRY",
                or_(intents.c.strategy_id == store.config.owner, intents.c.intent_id.is_(None)),
                or_(
                    intents.c.created_at < cohort.session_close.isoformat(),
                    intents.c.intent_id.is_(None),
                ),
            )
        )
    )
    unsent_by_id = definitively_unsent_many(
        repo, [row[0] for row in rows if row[0] != exclude_intent]
    )
    for identity, _state, symbol, recorded_at, session_open, session_close, boundary in rows:
        if identity == exclude_intent or unsent_by_id[identity]:
            continue
        if session_open is not None and session_close is not None:
            opening, closing = (
                datetime.fromisoformat(session_open),
                datetime.fromisoformat(session_close),
            )
            if opening == cohort.session_open and closing == cohort.session_close:
                daily[symbol] += 1
                boundaries.add(boundary)
            elif closing <= cohort.session_open:
                prior[symbol] += 1
        elif cohort.session_open <= datetime.fromisoformat(recorded_at) < cohort.session_close:
            daily[symbol] += 1
        elif datetime.fromisoformat(recorded_at) < cohort.session_open:
            prior[symbol] += 1
    return tuple(sorted(daily.items())), tuple(sorted(prior.items())), frozenset(boundaries)


def evaluate_cohort(
    config: CoreConfig,
    cohort: ObservationCohort,
    model: ModelArtifact,
    inventory: ReconciliationProof,
    *,
    session_counts: tuple[tuple[str, int], ...] = (),
    prior_counts: tuple[tuple[str, int], ...] = (),
    health_reasons: dict[str, tuple[str, ...]] | None = None,
    entry_policy: EntryPolicy | None = None,
    **authority: Any,
) -> Decision:
    from quantbot.paper_core.trader import evaluate_entry

    validate_cohort(config, cohort, entry_policy=entry_policy)
    universe = cohort_symbols(config, cohort, entry_policy)
    if config.selection_policy != "SIGNAL_OR_EXPLORE_V1":
        raise ValueError("cohort requires SIGNAL_OR_EXPLORE_V1")
    health = health_reasons or {}
    members = {
        o.symbol: evaluate_entry(
            config, o, model, inventory, _cohort_member=True, entry_policy=entry_policy, **authority
        )
        for o in cohort.observations
    }
    exclusions: dict[str, tuple[str, ...]] = {s: (reason,) for s, reason in cohort.unavailable}
    for symbol, decision in members.items():
        reasons = tuple(dict.fromkeys((*decision.gates, *health.get(symbol, ()))))
        if cohort.discovery is not None and entry_policy is not None and entry_policy.dynamic:
            sample = next(s for s in cohort.discovery.samples if s.symbol == symbol)
            if decision.quantity * decision.price > sample.dollar_volume / 10000:
                reasons += ("DISCOVERY_LIQUIDITY_CAPACITY",)
        if reasons:
            exclusions[symbol] = reasons
    eligible = [s for s in universe if s in members and s not in exclusions]
    daily, prior = dict(session_counts), dict(prior_counts)
    predictions = {s: d.prediction_bps for s, d in members.items() if d.prediction_bps is not None}
    strong = [s for s in eligible if s in predictions and predictions[s] > 10]

    if strong:
        chosen = min(
            strong,
            key=lambda s: (
                daily.get(s, 0),
                -predictions[s],
                prior.get(s, 0),
                universe.index(s),
            ),
        )
        kind = "SIGNAL"
    elif eligible:
        chosen = min(
            eligible,
            key=lambda s: (
                daily.get(s, 0),
                prior.get(s, 0),
                universe.index(s),
            ),
        )
        kind = "EXPLORATION"
    else:
        # Keep an actual observed member as the WAIT anchor, with every exclusion retained.
        if not members:
            raise ValueError("cohort has no available observation: " + repr(cohort.unavailable))
        chosen, kind = next(s for s in universe if s in members), "UNAVAILABLE"
    decision = members[chosen]
    gates = exclusions.get(chosen, ())
    from quantbot.paper_core.economics import limits

    daily_limit = limits(config, entry_policy=entry_policy).daily_entry_limit
    if daily_limit is not None and sum(daily.values()) >= daily_limit:
        gates = (*gates, "SESSION_RESERVATION_LIMIT")
    evidence = SelectionEvidence(
        kind=kind,
        cohort=cohort,
        boundary_id=boundary_id(config, cohort),
        session_counts=session_counts,
        prior_counts=prior_counts,
        exclusions=tuple((s, exclusions[s]) for s in universe if s in exclusions),
    )
    return decision.model_copy(
        update={"selection": evidence, "gates": gates, "action": "WAIT" if gates else "BUY"}
    )


def reservation_objections(
    store: CoreStore,
    session: Session,
    decision: Decision,
    *,
    intent_id: str | None = None,
) -> tuple[str, ...]:
    if store.config.selection_policy != "SIGNAL_OR_EXPLORE_V1":
        return ()

    if store.parallel_entries:
        from quantbot.paper_core.batch import member_objections

        return member_objections(store, session, decision, intent_id)
    try:
        policy = entry_policy_in_session(store, session)
    except StateConflictError:
        return ("CORE_RUNTIME_BINDING_MISMATCH",)
    if (
        decision.selection is None
        or not (
            policy.permits(decision.symbol)
            if policy
            else decision.symbol in symbols(store.config, policy)
        )
        or decision.entry_policy_id != (policy.id if policy else None)
    ):
        return ("SELECTION_AUTHORITY_MISSING",)
    cohort = decision.selection.cohort
    validate_cohort(store.config, cohort, entry_policy=policy)
    daily, _, boundaries = reservation_state(store, session, cohort, exclude_intent=intent_id)
    reasons = []
    if boundary_id(store.config, cohort) != decision.selection.boundary_id:
        reasons.append("SELECTION_BOUNDARY_MISMATCH")
    if decision.selection.boundary_id in boundaries:
        reasons.append("SELECTION_BOUNDARY_RESERVED")
    if sum(dict(daily).values()) >= SIGNAL_OR_EXPLORE_SESSION_LIMIT:
        reasons.append("SESSION_RESERVATION_LIMIT")
    return tuple(reasons)
