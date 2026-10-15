"""Read-only attribution of PAPER handoffs to actual broker fills.

Price-label outcomes remain distinct from executed-trade outcomes. Unowned
protective exits, incomplete quantities and missing close confirmation stay
unknown. This reader has no broker, fitting, promotion or order API.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import Any

from quantbot.domain import IntentState, OrderIntent
from quantbot.learner.exploration import ExplorationObservation
from quantbot.learner.five_minute_forward import FiveMinutePrediction
from quantbot.learner.forward import _read, _save
from quantbot.learner.paper_deadlines import CLOSED_EVENT, EXIT_EVENT, HANDOFF, PaperDeadline
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.storage.lot_ownership import PROTECTIVE_EXIT, REGISTRATION


def _time(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("attribution timestamps must be timezone-aware")
    return timestamp.astimezone(UTC)


@dataclass(frozen=True)
class TradeEpisode:
    handoff_id: str
    prediction_id: str
    model_id: str
    strategy_id: str
    symbol: str
    status: str
    observed_at: datetime
    entry_fill_ids: tuple[str, ...]
    exit_fill_ids: tuple[str, ...]
    fees: Decimal | None
    realized_net_pnl: Decimal | None
    net_return: Decimal | None
    forecast_score: float | None
    entry_notional: Decimal
    exit_notional: Decimal
    feature_identity: str
    entry_reference_close: float


def _known_fee(row: sqlite3.Row) -> Decimal | None:
    # Historical zeros were fabricated by the old FILL parser. Reading one is
    # not fee authority, including on a copied pre-v31 store.
    if row["fee"] is None:
        return None
    if "fee_source" not in row.keys() or row["fee_source"] == "UNKNOWN":
        return None
    return Decimal(row["fee"])


def _required_fee(row: sqlite3.Row) -> Decimal:
    fee = _known_fee(row)
    if fee is None:
        raise ValueError("verified fee is required for net arithmetic")
    return fee


def _fills(
    connection: sqlite3.Connection,
    intent_id: str,
    record: PaperDeadline,
    side: str,
    now: datetime,
    *,
    exit_strategy_id: str | None = None,
    ownership_at: datetime | None = None,
) -> list[sqlite3.Row]:
    durable = connection.execute(
        "SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)
    ).fetchone()
    if durable is None:
        raise ValueError("owned intent missing")
    intent = OrderIntent.model_validate(dict(durable))
    if (
        side == "BUY"
        and intent.model_copy(update={"state": IntentState.RISK_APPROVED}) != record.entry_intent
    ):
        raise ValueError("durable entry intent differs from handoff")
    rows = connection.execute(
        "SELECT f.*, i.strategy_id, i.created_at, b.filled_quantity, b.status, b.submitted_at "
        "FROM order_intents i JOIN broker_orders b USING(client_order_id) "
        "JOIN fills f USING(broker_order_id) WHERE i.intent_id=? "
        "AND i.symbol=f.symbol AND i.side=f.side AND b.symbol=f.symbol AND b.side=f.side",
        (intent_id,),
    ).fetchall()
    visible = [r for r in rows if _time(r["occurred_at"]) <= now]
    for row in visible:
        numbers = tuple(Decimal(row[field]) for field in ("quantity", "price"))
        fee = _known_fee(row)
        if (
            row["strategy_id"] != (exit_strategy_id or record.identity.strategy_id)
            or row["symbol"] != record.entry_intent.symbol
            or row["side"] != side
            or _time(row["occurred_at"]) < record.recorded_at
            or _time(row["occurred_at"]) < _time(
                row["submitted_at"] if side == "SELL" else row["created_at"]
            )
            or (side == "SELL" and _time(row["created_at"]) < record.recorded_at)
            or (ownership_at is not None and _time(row["occurred_at"]) < ownership_at)
            or any(not value.is_finite() for value in numbers)
            or numbers[0] <= 0
            or numbers[1] <= 0
            or (fee is not None and (not fee.is_finite() or fee < 0))
        ):
            raise ValueError("fill provenance or numeric integrity mismatch")
    return sorted(visible, key=lambda r: (_time(r["occurred_at"]), r["fill_id"]))


def _protection_owner(connection: sqlite3.Connection, event: sqlite3.Row,
                      record: PaperDeadline) -> str | None:
    """An event alone cannot confer foreign-strategy attribution."""
    detail = json.loads(event["detail_json"])
    registration = connection.execute("SELECT * FROM signals WHERE signal_id=?",
        ("adaptive-lot-" + str(detail.get("lot_id")),)).fetchone()
    if registration is None:
        return None
    payload = json.loads(registration["payload_json"])
    document = payload.get("document")
    if (not isinstance(document, dict) or payload.get("kind") != REGISTRATION
        or payload.get("document_hash") != content_id(document)
        or detail.get("registration_hash") != content_id(document)
        or document.get("handoff_id") != record.handoff_id
        or document.get("handoff_transport_hash") != content_id(record.model_dump(mode="json"))
        or document.get("entry_intent_id") != record.entry_intent.intent_id
        or document.get("account_id_hash") != record.account_id_hash
        or detail.get("account_id_hash") != record.account_id_hash
        or document.get("symbol") != record.entry_intent.symbol
        or document.get("position_before_entry") != "0"
        or not record.recorded_at <= _time(registration["occurred_at"]) < record.entry_valid_until
        or _time(event["occurred_at"]) < record.recorded_at):
        return None
    action = connection.execute(
        "SELECT a.*, e.strategy_id, e.reconciled, e.quantity AS evaluated_quantity, "
        "e.account_id AS evaluated_account, e.symbol AS evaluated_symbol "
        "FROM position_protection_actions a JOIN position_protection_evaluations e "
        "ON a.trigger_evaluation_id=e.evaluation_id WHERE a.intent_id=?",
        (event["intent_id"],),
    ).fetchone()
    if (action is None or action["reconciled"] != 1 or detail.get("reconciled") is not True
        or action["strategy_id"] != detail.get("exit_strategy_id")
        or PaperDeadline.account_hash(action["account_id"]) != record.account_id_hash
        or action["evaluated_account"] != action["account_id"]
        or action["symbol"] != record.entry_intent.symbol
        or action["evaluated_symbol"] != record.entry_intent.symbol
        or action["side"] != "SELL"
        or Decimal(action["quantity"]) != Decimal(str(detail.get("quantity")))
        or Decimal(action["evaluated_quantity"]) != Decimal(action["quantity"])):
        return None
    return str(action["strategy_id"])


def _episode(connection: sqlite3.Connection, signal: sqlite3.Row, now: datetime) -> TradeEpisode:
    payload: dict[str, Any] = json.loads(signal["payload_json"])
    record = PaperDeadline.model_validate(payload["document"])
    if (
        payload["document_hash"] != record.handoff_id
        or signal["signal_id"]
        != "paper-deadline-" + content_id({"intent_id": record.entry_intent.intent_id})
        or signal["strategy_id"] != record.identity.strategy_id
        or signal["symbol"] != record.entry_intent.symbol
        or _time(signal["occurred_at"]) != record.recorded_at
    ):
        raise ValueError("handoff provenance mismatch")
    forecast = connection.execute(
        "SELECT * FROM signals WHERE signal_id=?", ("learner-forecast-" + record.prediction_id,)
    ).fetchone()
    if forecast is None:
        raise ValueError("exact durable forecast missing")
    document = json.loads(forecast["payload_json"])
    prediction: FiveMinutePrediction | ExplorationObservation
    if document["kind"] == "LEARNER_PAPER_EXPLORATION_V1":
        prediction = ExplorationObservation.model_validate(document["prediction"])
        # The retained binding stores the immutable policy hash, not its body.
        # The handoff also binds the exact observation ID; no forecast is invented.
        if record.binding.artifact_hash != prediction.policy_id:
            raise ValueError("exploration policy lineage mismatch")
        model_id, forecast_score = prediction.policy_id, None
    elif document["kind"] == "LEARNER_PAPER_FORECAST_V1":
        prediction = FiveMinutePrediction.model_validate(document["prediction"])
        model_id, forecast_score = prediction.model_id, prediction.score
    else:
        raise ValueError("unknown durable entry observation kind")
    if (
        prediction.prediction_id != record.prediction_id
        or model_id != record.model_id
        or prediction.features.symbol != record.entry_intent.symbol
        or forecast["strategy_id"] != record.identity.strategy_id
        or forecast["run_id"] != signal["run_id"]
        or forecast["symbol"] != record.entry_intent.symbol
        or _time(forecast["occurred_at"]) < prediction.recorded_at
        or _time(forecast["occurred_at"]) > min(record.recorded_at, now)
        or prediction.recorded_at > record.recorded_at
        or document["binding"] != record.binding.model_dump(mode="json")
    ):
        raise ValueError("forecast lineage mismatch")
    events = connection.execute(
        "SELECT * FROM order_events WHERE event_type IN (?,?,?)",
        (EXIT_EVENT, CLOSED_EVENT, PROTECTIVE_EXIT),
    ).fetchall()
    owned = [
        e
        for e in events
        if json.loads(e["detail_json"]).get("handoff_id") == record.handoff_id
        and _time(e["occurred_at"]) <= now
    ]
    protective = {e["intent_id"]: (actor, _time(e["occurred_at"])) for e in owned
                  if e["event_type"] == PROTECTIVE_EXIT
                  and (actor := _protection_owner(connection, e, record)) is not None}
    exit_ids = {e["intent_id"] for e in owned if e["event_type"] == EXIT_EVENT} | set(protective)
    if None in exit_ids or any(
        e["intent_id"] in exit_ids
        and e["event_type"] in {EXIT_EVENT, PROTECTIVE_EXIT}
        and json.loads(e["detail_json"]).get("handoff_id") != record.handoff_id
        for e in events
    ):
        raise ValueError("exit ownership ambiguous")
    closed = any(
        e["event_type"] == CLOSED_EVENT and e["intent_id"] == record.entry_intent.intent_id
        for e in owned
    )
    entries = _fills(connection, record.entry_intent.intent_id, record, "BUY", now)
    exits = [
        fill
        for identity in sorted(exit_ids)
        for fill in _fills(
            connection, identity, record, "SELL", now,
            exit_strategy_id=(protective[identity][0] if identity in protective else None),
            ownership_at=(protective[identity][1] if identity in protective else None),
        )
    ]
    entry_qty = sum((Decimal(r["quantity"]) for r in entries), Decimal(0))
    exit_qty = sum((Decimal(r["quantity"]) for r in exits), Decimal(0))
    known = [_known_fee(r) for r in (*entries, *exits)]
    fees = (None if any(f is None for f in known)
            else sum((f for f in known if f is not None), Decimal(0)))
    status = "OPEN" if entries else "AWAITING_ENTRY_FILLS"
    pnl = net_return = None
    if exits and not closed:
        status = "AWAITING_CLOSE_CONFIRMATION"
    if closed:
        # Closed events alone do not prove complete fill accounting, including
        # exits performed by a different protection owner.
        complete = entry_qty > 0 and entry_qty == exit_qty
        inventory = Decimal(0)
        chronological = sorted(
            (*entries, *exits),
            key=lambda r: (_time(r["occurred_at"]), 0 if r["side"] == "BUY" else 1, r["fill_id"]),
        )
        for _, grouped in groupby(chronological, key=lambda r: _time(r["occurred_at"])):
            group = list(grouped)
            sells = sum((Decimal(r["quantity"]) for r in group if r["side"] == "SELL"), Decimal(0))
            buys = sum((Decimal(r["quantity"]) for r in group if r["side"] == "BUY"), Decimal(0))
            # No broker sequence means equal-time buys cannot fund these sells.
            complete &= sells <= inventory
            inventory += buys - sells
        last_fill = max((_time(r["occurred_at"]) for r in chronological), default=now)
        complete &= any(
            e["event_type"] == CLOSED_EVENT
            and e["intent_id"] == record.entry_intent.intent_id
            and _time(e["occurred_at"]) >= last_fill
            for e in owned
        )
        for group in (entries, exits):
            by_order = {r["broker_order_id"] for r in group}
            for order in by_order:
                subset = [r for r in group if r["broker_order_id"] == order]
                complete &= sum((Decimal(r["quantity"]) for r in subset), Decimal(0)) == Decimal(
                    subset[0]["filled_quantity"]
                ) and subset[0]["status"] in {"filled", "canceled", "expired", "rejected"}
        status = "CLOSED" if complete else "INCOMPLETE_FILL_ATTRIBUTION"
        if complete and fees is None:
            status = "COSTS_PENDING"
        if complete and fees is not None:
            entry_cost = sum(
                (Decimal(r["price"]) * Decimal(r["quantity"]) + _required_fee(r)
                 for r in entries),
                Decimal(0),
            )
            proceeds = sum(
                (Decimal(r["price"]) * Decimal(r["quantity"]) - _required_fee(r)
                 for r in exits),
                Decimal(0),
            )
            pnl = proceeds - entry_cost
            net_return = pnl / entry_cost
    return TradeEpisode(
        record.handoff_id,
        record.prediction_id,
        record.model_id,
        record.identity.strategy_id,
        record.entry_intent.symbol,
        status,
        now,
        tuple(r["fill_id"] for r in entries),
        tuple(r["fill_id"] for r in exits),
        fees,
        pnl,
        net_return,
        forecast_score,
        sum((Decimal(r["price"]) * Decimal(r["quantity"]) for r in entries), Decimal(0)),
        sum((Decimal(r["price"]) * Decimal(r["quantity"]) for r in exits), Decimal(0)),
        content_id(prediction.features),
        prediction.features.reference_close,
    )


def read_trade_episodes(
    path: Path,
    *,
    now: datetime,
    strategy_id: str | None = None,
    guard: Callable[[datetime, datetime], bool] | None = None,
) -> tuple[TradeEpisode, ...]:
    """Snapshot-consistent read; absent store never created, future events excluded."""
    now = _time(now.isoformat())
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        signals = connection.execute(
            "SELECT * FROM signals WHERE (? IS NULL OR strategy_id=?) "
            "ORDER BY occurred_at,signal_id",
            (strategy_id, strategy_id),
        )
        records = [
            r
            for r in signals
            if (guard is None or guard(_time(r["occurred_at"]), _time(r["occurred_at"])))
            and json.loads(r["payload_json"]).get("kind") == HANDOFF
            and _time(r["occurred_at"]) <= now
        ]
        return tuple(_episode(connection, record, now) for record in records)


def journal_trade_episodes(root: Path, episodes: Sequence[TradeEpisode]) -> int:
    """Append content-addressed revisions to an isolated execution-learning journal.

    Re-reading unchanged fills is idempotent. Partial/open receipts remain
    explicit, and fee/fill corrections append a revision rather than rewriting
    history. Execution outcomes must not be substituted for market labels.
    """
    created = 0
    for episode in episodes:
        payload = asdict(episode)
        payload.pop("observed_at")
        payload.update(
            evidence="ACTUAL_PAPER_BROKER_FILLS",
            usable_executed_outcome=episode.status == "CLOSED",
            order_authority=False,
            interpretation="EXECUTION_DIAGNOSTIC_NOT_AN_UNBIASED_MARKET_RETURN_LABEL",
        )
        document = json.loads(canonical_result_json(payload))
        path = root / (content_id(document) + ".json")
        if path.exists():
            if _read(path) != document:
                raise ValueError("execution journal integrity conflict")
            continue
        _save(path, document)
        created += 1
    return created
