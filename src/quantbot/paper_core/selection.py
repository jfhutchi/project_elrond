"""Deterministic PAPER selection and existing-outbox reservation accounting."""

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from quantbot.domain import IntentState
from quantbot.paper_core.contracts import (
    CoreConfig,
    Decision,
    ModelArtifact,
    ObservationCohort,
    ReconciliationProof,
    SelectionEvidence,
)
from quantbot.paper_core.store import CoreStore, table
from quantbot.research.manifest import content_id
from quantbot.storage import StorageRepository

SIGNAL_OR_EXPLORE_SESSION_LIMIT = 6


def boundary_id(config: CoreConfig, cohort: ObservationCohort) -> str:
    return content_id(
        {
            "account": config.account_id,
            "owner": config.owner,
            "session_open": cohort.session_open,
            "session_close": cohort.session_close,
            "source_end": cohort.source_end,
        }
    )


def cohort_document(store: CoreStore, cohort: ObservationCohort) -> dict[str, Any]:
    return {"selection_cohort": cohort, "config_id": store.config_id}


def record_cohort(store: CoreStore, cohort: ObservationCohort) -> str:
    from quantbot.paper_core.store import put

    validate_cohort(store.config, cohort)
    document = cohort_document(store, cohort)
    identity = content_id(document)
    with store.database.transaction() as session:
        for observation in cohort.observations:
            put(session, "core_observations", observation.id, observation)
        put(session, "core_provider_acquisitions", identity, document)
    return identity


def validate_cohort(config: CoreConfig, cohort: ObservationCohort) -> None:
    members = [o.symbol for o in cohort.observations] + [s for s, _ in cohort.unavailable]
    if (
        len(members) != len(set(members))
        or set(members) != set(config.entry_symbols)
        or any(not reason for _, reason in cohort.unavailable)
        or any(
            t.tzinfo is None for t in (cohort.source_end, cohort.session_open, cohort.session_close)
        )
        or not cohort.session_open <= cohort.source_end < cohort.session_close
        or cohort.source_end.timestamp() % 300 != 0
        or any(
            o.source_end != cohort.source_end
            or o.session_open != cohort.session_open
            or o.session_close != cohort.session_close
            for o in cohort.observations
        )
    ):
        raise ValueError("cohort must capture the complete universe at one verified boundary")


def reservation_state(
    store: CoreStore,
    session: Session,
    cohort: ObservationCohort,
    *,
    exclude_intent: str | None = None,
) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...], frozenset[str]]:
    """Only definitive unsent proof releases usage; completion never releases daily usage."""
    from quantbot.paper_core.send_guard import definitively_unsent

    links, intents, decisions = (
        table(n) for n in ("core_intent_links", "order_intents", "core_decisions")
    )
    repo = StorageRepository(session)
    daily: Counter[str] = Counter()
    prior: Counter[str] = Counter()
    boundaries = set()
    # Hydrate scalar selection metadata only, rather than every historic cohort's features.
    rows = session.execute(
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
    for identity, state, symbol, recorded_at, session_open, session_close, boundary in rows:
        if (
            identity == exclude_intent
            or state == IntentState.ERROR.value
            and definitively_unsent(repo, identity)
        ):
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
    **authority: Any,
) -> Decision:
    from quantbot.paper_core.trader import evaluate_entry

    validate_cohort(config, cohort)
    if config.selection_policy != "SIGNAL_OR_EXPLORE_V1":
        raise ValueError("cohort requires SIGNAL_OR_EXPLORE_V1")
    health = health_reasons or {}
    members = {
        o.symbol: evaluate_entry(config, o, model, inventory, _cohort_member=True, **authority)
        for o in cohort.observations
    }
    exclusions: dict[str, tuple[str, ...]] = {s: (reason,) for s, reason in cohort.unavailable}
    for symbol, decision in members.items():
        reasons = tuple(dict.fromkeys((*decision.gates, *health.get(symbol, ()))))
        if reasons:
            exclusions[symbol] = reasons
    eligible = [s for s in config.entry_symbols if s in members and s not in exclusions]
    daily, prior = dict(session_counts), dict(prior_counts)
    unused = [s for s in eligible if not daily.get(s, 0)]
    tier = unused or eligible
    predictions = {s: d.prediction_bps for s, d in members.items() if d.prediction_bps is not None}
    strong = [s for s in eligible if s in predictions and predictions[s] > 10]

    def fairness(symbol: str) -> tuple[int, int]:
        return daily.get(symbol, 0) + prior.get(symbol, 0), config.entry_symbols.index(symbol)

    if strong:
        strong = [s for s in strong if not daily.get(s, 0)] or strong
        chosen = min(
            strong,
            key=lambda s: (
                fairness(s)[0],
                -predictions[s],
                config.entry_symbols.index(s),
            ),
        )
        kind = "SIGNAL"
    elif tier:
        chosen, kind = min(tier, key=fairness), "EXPLORATION"
    else:
        # Keep an actual observed member as the WAIT anchor, with every exclusion retained.
        if not members:
            raise ValueError("cohort has no available observation: " + repr(cohort.unavailable))
        chosen, kind = next(s for s in config.entry_symbols if s in members), "UNAVAILABLE"
    decision = members[chosen]
    gates = exclusions.get(chosen, ())
    if sum(daily.values()) >= SIGNAL_OR_EXPLORE_SESSION_LIMIT:
        gates = (*gates, "SESSION_RESERVATION_LIMIT")
    evidence = SelectionEvidence(
        kind=kind,
        cohort=cohort,
        boundary_id=boundary_id(config, cohort),
        session_counts=session_counts,
        prior_counts=prior_counts,
        exclusions=tuple((s, exclusions[s]) for s in config.entry_symbols if s in exclusions),
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
    if decision.selection is None or decision.symbol not in store.config.entry_symbols:
        return ("SELECTION_AUTHORITY_MISSING",)
    cohort = decision.selection.cohort
    validate_cohort(store.config, cohort)
    daily, _, boundaries = reservation_state(store, session, cohort, exclude_intent=intent_id)
    reasons = []
    if boundary_id(store.config, cohort) != decision.selection.boundary_id:
        reasons.append("SELECTION_BOUNDARY_MISMATCH")
    if decision.selection.boundary_id in boundaries:
        reasons.append("SELECTION_BOUNDARY_RESERVED")
    if sum(dict(daily).values()) >= SIGNAL_OR_EXPLORE_SESSION_LIMIT:
        reasons.append("SESSION_RESERVATION_LIMIT")
    return tuple(reasons)
