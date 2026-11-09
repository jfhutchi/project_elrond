"""Real adapter failures retain diagnostics without granting settlement authority."""

import hashlib
import json
from typing import cast

import pytest
from pydantic import SecretStr
from tests.fault.test_ambiguous_submit import (
    ScriptedAmbiguousBroker,
    _coordinator,
    _submit,
)

from quantbot.brokers import BrokerAdapter, BrokerRequestError, SubmissionUncertainError
from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.base import BrokerTransportError, BrokerTransportResponse
from quantbot.domain import IntentState
from quantbot.paper_core.account_control import LocalRequestBudgetRefusal
from quantbot.paper_core.settlement import definitively_unsent
from quantbot.storage import Database, StorageRepository

BODY = b'{"code":42210000,"message":"synthetic paper-secret account-sensitive"}'


class ResponseTransport:
    def __init__(self, outcome):
        self.outcome = outcome

    async def request(self, method, url, **kwargs):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if method == "GET":
            return BrokerTransportResponse(status_code=404, headers={}, content=b"{}")
        return BrokerTransportResponse(status_code=self.outcome, headers={}, content=BODY)


class AdapterSubmissionBroker(ScriptedAmbiguousBroker):
    def __init__(self, outcome):
        super().__init__([], [None, None])
        self.adapter = AlpacaPaperBrokerAdapter(
            api_key=SecretStr("paper-key"),
            api_secret=SecretStr("paper-secret"),
            transport=ResponseTransport(outcome),
            max_retries=0,
        )

    async def submit_order(self, intent):
        return await self.adapter.submit_order(intent)


@pytest.mark.asyncio
async def test_real_422_retains_bounded_provenance_with_rejected_state(tmp_path):
    database = Database.create(tmp_path / "rejection.db")
    try:
        broker = AdapterSubmissionBroker(422)
        with pytest.raises(BrokerRequestError) as caught:
            await _submit(_coordinator(database, cast(BrokerAdapter, broker)))
        with database.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent("intent-1").state is IntentState.REJECTED
            events = repo.list_order_events(intent_id="intent-1")
            assert len(events) == 1
            event = events[0]
            assert event.event_type == "BROKER_SUBMISSION_REJECTED"
            assert event.broker_order_id is None
            detail = event.detail
            assert detail["client_order_id"] == "stable-client-id"
            assert detail["intent_id"] == "intent-1"
            assert detail["contract"] == "BROKER_REJECTION_DIAGNOSTIC_V1"
            provenance = detail["response_provenance"]
            assert provenance["provider"] == "alpaca"
            assert provenance["method"] == "POST"
            assert provenance["path"] == "/v2/orders"
            assert provenance["status_code"] == 422
            assert provenance["provider_error_code"] == "42210000"
            assert provenance["response_sha256"] == hashlib.sha256(BODY).hexdigest()
            assert len(provenance["request_sha256"]) == 64
            assert caught.value.response_provenance.model_dump(mode="json") == provenance
            encoded = json.dumps(detail)
            assert "paper-secret" not in encoded
            assert "account-sensitive" not in encoded
            assert len(encoded) < 1500
            assert "definitively_unsent" not in detail
            assert not definitively_unsent(repo, "intent-1")
    finally:
        database.close()


@pytest.mark.asyncio
async def test_rejection_event_failure_rolls_back_state_transition(tmp_path, monkeypatch):
    database = Database.create(tmp_path / "rollback.db")
    original = StorageRepository.record_order_event

    def fail_receipt(self, *args, **kwargs):
        if kwargs.get("event_type") == "BROKER_SUBMISSION_REJECTED":
            raise RuntimeError("synthetic receipt failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(StorageRepository, "record_order_event", fail_receipt)
    try:
        with pytest.raises(RuntimeError, match="synthetic receipt failure"):
            await _submit(_coordinator(database, AdapterSubmissionBroker(422)))
        with database.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent("intent-1").state is IntentState.SUBMITTING
            assert not repo.list_order_events(intent_id="intent-1")
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [500, BrokerTransportError("synthetic timeout")])
async def test_ambiguous_failures_do_not_create_rejection_receipts(tmp_path, outcome):
    database = Database.create(tmp_path / "ambiguous.db")
    try:
        with pytest.raises(SubmissionUncertainError):
            await _submit(_coordinator(database, AdapterSubmissionBroker(outcome)))
        with database.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent("intent-1").state is IntentState.SUBMISSION_UNKNOWN
            assert not repo.list_order_events(intent_id="intent-1")
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        BrokerRequestError("legacy", status_code=422),
        LocalRequestBudgetRefusal("POST", "stable-client-id"),
    ],
    ids=["legacy", "local-budget"],
)
async def test_error_without_metadata_retains_state_without_receipt(tmp_path, error):
    database = Database.create(tmp_path / "legacy.db")
    try:
        broker = ScriptedAmbiguousBroker([error], [])
        with pytest.raises(BrokerRequestError):
            await _submit(_coordinator(database, broker))
        with database.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent("intent-1").state is IntentState.REJECTED
            assert not repo.list_order_events(intent_id="intent-1")
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [b"<html>" + b"x" * 100_000, b"invalid JSON", b'{"code":"secret"}', b"[" * 2000 + b"]" * 2000],
    ids=["large-html", "invalid-json", "non-numeric-code", "deep-json"],
)
async def test_invalid_or_large_body_keeps_http_error_without_free_text(body):
    class BodyTransport:
        async def request(self, *args, **kwargs):
            return BrokerTransportResponse(status_code=422, headers={}, content=body)

    broker = AdapterSubmissionBroker(422)
    broker.adapter._transport = BodyTransport()
    with pytest.raises(BrokerRequestError) as caught:
        await broker.adapter._request("POST", "/v2/orders", json_body={"client_order_id": "test"})
    metadata = caught.value.response_provenance
    assert metadata.provider_error_code is None
    assert metadata.response_sha256 == hashlib.sha256(body).hexdigest()
    assert len(metadata.model_dump_json()) < 1000


@pytest.mark.asyncio
async def test_get_response_cannot_masquerade_as_submission_rejection(tmp_path):
    adapter = AdapterSubmissionBroker(422).adapter
    with pytest.raises(BrokerRequestError) as caught:
        await adapter._request("GET", "/v2/account")
    database = Database.create(tmp_path / "wrong-method.db")
    try:
        broker = ScriptedAmbiguousBroker([caught.value], [])
        with pytest.raises(BrokerRequestError):
            await _submit(_coordinator(database, broker))
        with database.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent("intent-1").state is IntentState.REJECTED
            assert not repo.list_order_events(intent_id="intent-1")
    finally:
        database.close()
