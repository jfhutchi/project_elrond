"""Verified no-send dispositions; never fills, profitability or entry authority."""

from datetime import datetime

from pydantic import Field

from quantbot.learner.experiment_contract import Digest
from quantbot.learner.experiment_lots import LotEvent
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.models import LearnerModel, Text
from quantbot.operations.cycle import SubmissionRequest
from quantbot.research.manifest import content_id
from quantbot.risk import OrderPurpose


class PreSendRejection(LearnerModel):
    experiment_id: Text
    intent_id: Text
    recorded_at: datetime
    request_hash: Digest
    reasons: tuple[Text, ...] = Field(min_length=1)


def verified_refused_lots(
    store: ExperimentStore, experiment_id: str, *, now: datetime
) -> dict[str, PreSendRejection]:
    events = store.read_as_of(experiment_id, now)
    lots = tuple(row.decode(LotEvent) for row in events if row.kind == "LotEvent")
    result: dict[str, PreSendRejection] = {}
    for event in events:
        if event.kind != "PreSendRejection":
            continue
        proof = event.decode(PreSendRejection)
        queued = store.get_intent(proof.intent_id)
        request = SubmissionRequest.model_validate_json(queued.document) if queued else None
        declarations = [
            row for row in lots if row.kind == "ENTRY_DECLARED" and row.order_id == proof.intent_id
        ]
        if (
            queued is None
            or request is None
            or queued.state != "ACKNOWLEDGED"
            or queued.experiment_id != experiment_id
            or proof.experiment_id != experiment_id
            or request.intent.intent_id != proof.intent_id
            or proof.request_hash != content_id(request)
            or request.purpose is not OrderPurpose.ENTRY
            or len(declarations) != 1
        ):
            raise ValueError("PRE_SEND_REJECTION_PROVENANCE_MISMATCH")
        declaration = declarations[0]
        links = [
            row for row in lots if row.lot_id == declaration.lot_id and row.kind == "ORDER_LINK"
        ]
        if (
            declaration.lot_id in result
            or declaration.symbol != request.intent.symbol
            or declaration.occurred_at > proof.recorded_at
            or len(links) != 1
            or links[0].order_id != proof.intent_id
            or links[0].side != "BUY"
            or links[0].source_hash != content_id(request.intent)
            or links[0].quantity != request.intent.quantity
            or any(
                row.kind not in {"ENTRY_DECLARED", "ORDER_LINK"}
                for row in lots
                if row.lot_id == declaration.lot_id
            )
        ):
            raise ValueError("PRE_SEND_REJECTION_LOT_CONFLICT")
        result[declaration.lot_id] = proof
    return result
