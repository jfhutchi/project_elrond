"""Read-only core experience projection; ledger counts never imply provider verification."""

import json
import sqlite3
from typing import Any


def experience_status(connection: sqlite3.Connection) -> dict[str, Any]:
    lanes: dict[str, dict[str, int]] = {}

    def lane(name: str) -> dict[str, int]:
        return lanes.setdefault(name, {})

    for table, label in (("core_observations", "observations"), ("core_decisions", "decisions")):
        for name, count in connection.execute(
            f"SELECT json_extract(payload,'$.evidence_lane'),count(*) FROM {table} GROUP BY 1"
        ):
            lane(name)[label] = count
    for name, action, count in connection.execute(
        "SELECT json_extract(payload,'$.evidence_lane'),json_extract(payload,'$.action'),"
        "count(*) FROM core_decisions GROUP BY 1,2"
    ):
        lane(name)["buy_decisions" if action == "BUY" else "abstentions"] = count
    for name, count, sessions, unknown in connection.execute(
        "SELECT p.evidence_lane,count(*),count(DISTINCT "
        "substr(json_extract(o.payload,'$.session_close'),1,10)),"
        "sum(json_extract(r.payload,'$.actual_fees') IS NULL) FROM core_outcome_index p "
        "JOIN core_outcome_revisions r ON r.id=p.outcome_id "
        "JOIN core_decisions d ON d.id=p.lot_id "
        "JOIN core_observations o ON o.id=json_extract(d.payload,'$.observation_id') GROUP BY 1"
    ):
        lane(name).update(
            completed_round_trips=count,
            sessions_with_completed_trades=sessions,
            outcomes_with_unknown_fees=unknown,
        )
    for name, count in connection.execute(
        "SELECT json_extract(d.payload,'$.evidence_lane'),count(DISTINCT f.fill_id) "
        "FROM fills f JOIN broker_orders b ON b.broker_order_id=f.broker_order_id "
        "JOIN order_intents i ON i.client_order_id=b.client_order_id "
        "JOIN core_intent_links l ON l.intent_id=i.intent_id "
        "JOIN core_decisions d ON d.id=l.decision_id GROUP BY 1"
    ):
        lane(name)["core_fills"] = count
    for name, count, orders in connection.execute(
        "SELECT json_extract(d.payload,'$.evidence_lane'),count(DISTINCT l.lot_id),"
        "count(DISTINCT b.broker_order_id) FROM core_intent_links l "
        "JOIN core_decisions d ON d.id=l.decision_id "
        "JOIN order_intents i ON i.intent_id=l.intent_id "
        "LEFT JOIN broker_orders b ON b.client_order_id=i.client_order_id "
        "WHERE l.role='ENTRY' GROUP BY 1"
    ):
        lane(name).update(entry_reservations=count, broker_entry_orders=orders)
    for counts in lanes.values():
        for key in (
            "observations",
            "decisions",
            "buy_decisions",
            "abstentions",
            "core_fills",
            "completed_round_trips",
            "sessions_with_completed_trades",
            "outcomes_with_unknown_fees",
            "entry_reservations",
            "broker_entry_orders",
        ):
            counts.setdefault(key, 0)
    active = connection.execute(
        "SELECT a.model_id,a.activation_id,m.payload FROM core_active_model a "
        "JOIN core_models m ON m.id=a.model_id"
    ).fetchone()
    model = None
    if active:
        citations = connection.execute(
            "SELECT count(*) FROM core_decisions WHERE json_extract(payload,'$.model_id')=? "
            "AND json_extract(payload,'$.activation_id')=? "
            "AND json_extract(payload,'$.evidence_lane')='BROKER_PAPER'",
            (active[0], active[1]),
        ).fetchone()[0]
        model = dict(
            model_id=active[0],
            activation_id=active[1],
            mode=json.loads(active[2])["mode"],
            paper_decision_citations=citations,
        )
    receipt_row = connection.execute(
        "SELECT id,payload FROM core_learning_runs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    learning = None
    if receipt_row:
        receipt = json.loads(receipt_row[1])
        excluded = {identity for identity, _ in receipt["exclusions"]}
        learning = dict(
            receipt_id=receipt_row[0],
            status=receipt["status"],
            reason=receipt["reason"],
            completed_session=receipt.get("completed_session"),
            considered=len(receipt["considered_ids"]),
            excluded=len(excluded),
            eligible=len(set(receipt["considered_ids"]) - excluded),
            validation_folds=len(receipt.get("fold_results", ())),
        )
    return dict(
        scope="ALL_RETAINED_CORE_HISTORY",
        broker_state="NOT_PROBED",
        by_lane=lanes,
        fit_attempts=connection.execute("SELECT count(*) FROM core_learning_attempts").fetchone()[
            0
        ],
        completed_fits=connection.execute(
            "SELECT count(*) FROM core_learning_runs "
            "WHERE json_extract(payload,'$.fit_completed_at') IS NOT NULL"
        ).fetchone()[0],
        active_model=model,
        latest_learning=learning,
    )
