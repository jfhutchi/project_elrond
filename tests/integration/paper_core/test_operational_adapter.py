"""Offline provider contracts; none of these fixtures certifies the actual account."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.base import BrokerTransportResponse
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.calendar import AlpacaMarketCalendar
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.contracts import CoreConfig
from quantbot.paper_core.release import RuntimeConfig


class ProviderFixture:
    def __init__(self, now):
        self.now, self.calls = now, []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("params")))
        if url.endswith("/calendar"):
            payload = [
                {
                    "date": (self.now.date() + timedelta(days=d)).isoformat(),
                    "open": "09:30",
                    "close": "16:00",
                }
                for d in (-1, 0, 1)
            ]
        elif url.endswith("/clock"):
            payload = {
                "timestamp": self.now.isoformat(),
                "is_open": True,
                "next_open": (self.now + timedelta(days=1)).isoformat(),
                "next_close": self.now.replace(hour=20, minute=0, second=0).isoformat(),
            }
        elif url.endswith("/bars"):
            end = self.now.replace(second=0, microsecond=0)
            payload = {
                "bars": {
                    s: [
                        {
                            "t": (end - timedelta(minutes=35 - i * 5)).isoformat(),
                            "o": 100,
                            "h": 100,
                            "l": 100,
                            "c": 100,
                            "v": 100,
                        }
                        for i in range(7)
                    ]
                    for s in ("XLE", "SPY")
                },
                "next_page_token": None,
            }
        else:
            payload = []
        return BrokerTransportResponse(
            status_code=200, headers={}, content=json.dumps(payload).encode()
        )


@pytest.mark.asyncio
async def test_operational_features_accept_real_receipt_after_bar_boundary(tmp_path):
    from quantbot.market_data.base import TransportResponse

    now = datetime(2026, 10, 14, 14, 5, 3, tzinfo=UTC)
    provider = ProviderFixture(now)

    class MarketTransport:
        async def request(self, *args, **kwargs):
            response = await provider.request(*args, **kwargs)
            return TransportResponse.model_validate(response.model_dump())

    class Clock:
        process_id = "adapter-fixture"

        def sample(self):
            return now, 1.0

    config = RuntimeConfig(
        repository=str(Path(__file__).resolve().parents[3]),
        store_path=str(tmp_path / "fixture.db"),
        store_identity="fixture",
        expected_account_hash=hashlib.sha256(b"paper").hexdigest(),
        core=CoreConfig(
            account_id="paper",
            symbol="XLE",
            release_id="UNBOUND_PREPARED_RELEASE",
            evidence_lane="BROKER_PAPER",
            clock_profile="PAPER_TIME_V2",
        ),
        holdout_source=str(tmp_path / "canonical.db"),
        learning_origin=now - timedelta(days=20),
        canonical_datasets=("us-equities-daily",),
    )
    key, secret = SecretStr("offline-key"), SecretStr("offline-secret")
    broker = AlpacaCoreBroker(
        AlpacaPaperBrokerAdapter(api_key=key, api_secret=secret, transport=provider),
        AlpacaMarketDataClient(api_key=key, api_secret=secret, transport=MarketTransport()),
        AlpacaMarketCalendar(api_key=key, api_secret=secret, transport=MarketTransport()),
        config,
        Clock(),
        account_id="paper",
    )
    observed = await broker.observation()
    assert observed.source_end == now - timedelta(seconds=3)
    assert observed.available_at == observed.recorded_at_utc == now
    assert observed.dependencies == ("SPY",) and len(observed.features) == 10
    assert observed.evidence_lane == "BROKER_PAPER" and observed.feed == "alpaca"
    assert all(call[0] == "GET" for call in provider.calls)
    print("M6_ADAPTER_OBSERVATION:" + observed.model_dump_json())


def test_documented_partial_paper_zero_does_not_become_all_in_zero():
    from quantbot.domain import Fill, OrderSide
    from quantbot.paper_core.operational_costs import PaperCostComponent, PaperCostContract

    now = datetime(2026, 10, 14, 14, 5, tzinfo=UTC)
    component = PaperCostComponent(
        component="REGULATORY",
        document_id="paper-doc-v20261014",
        source_url="https://docs.alpaca.markets/us/docs/paper-trading",
        source_sha256="a" * 64,
        applicable_account_evidence_id="OFFLINE_APPLICABILITY_FIXTURE",
    )
    contract = PaperCostContract(
        account_id="paper",
        endpoint="https://paper-api.alpaca.markets",
        asset_class="US_LISTED_EQUITY",
        symbols=("XLE",),
        start=now - timedelta(days=1),
        end=now + timedelta(days=1),
        components=(component,),
    )
    fill = Fill(
        fill_id="fill",
        broker_order_id="order",
        symbol="XLE",
        side=OrderSide.SELL,
        quantity=1,
        price=100,
        occurred_at=now,
    )
    assert contract.fee(fill, account_id="paper", at=now, release_id="fixture") is None
    assert fill.fee is None


@pytest.mark.asyncio
@pytest.mark.parametrize("precise_cash_time", [True, False])
@pytest.mark.parametrize("delayed_zero_import", [False, True])
async def test_fee_only_after_overlap_revises_retained_contract_bounded_restart(
    tmp_path, monkeypatch, precise_cash_time, delayed_zero_import
):
    from decimal import Decimal
    from types import SimpleNamespace

    from tests.fixtures.core_m2 import NOW, events, fee_events, prepare

    from quantbot.paper_core.costs import current_costs
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.learning import _guard
    from quantbot.paper_core.operational_costs import PaperCostComponent, PaperCostContract
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.store import put, table
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import content_id

    db, store, simulated, proof, observation, model = prepare(tmp_path)
    try:
        if delayed_zero_import:
            observation = observation.model_copy(
                update={"feature_version": "five-minute-price-alpha-v1"}
            )
            model = model.model_copy(update={"feature_version": "five-minute-price-alpha-v1"})
        decision = await trader_cycle(store, simulated, observation, model, proof, now=NOW)
        simulated.now = NOW + timedelta(minutes=30)
        simulated.set_mark("XLE", Decimal(102), as_of=simulated.now)
        await guardian_cycle(store, simulated, now=simulated.now)
        import_broker_events(store, events(store, simulated))
        contract = PaperCostContract(
            account_id=simulated.account_id,
            endpoint="https://paper-api.alpaca.markets",
            asset_class="US_LISTED_EQUITY",
            symbols=("XLE",),
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(days=10),
            components=tuple(
                PaperCostComponent(
                    component=c,
                    document_id="EXPLICIT_OFFLINE_APPLICABILITY_" + c,
                    source_url="https://docs.alpaca.markets/us/docs/paper-trading",
                    source_sha256="a" * 64,
                    applicable_account_evidence_id="MOCK_ONLY_NOT_ACTUAL_PAPER_AUTHORITY",
                )
                for c in ("COMMISSION", "REGULATORY", "BORROW", "CLEARING", "OTHER")
            ),
        )
        zeros = []
        for source in fee_events(store, simulated):
            item = source.cost.model_copy(
                update={"source": "CONTRACTUAL_ZERO", "document_id": contract.id}
            )
            zeros.append(
                source.model_copy(
                    update={"cost": item, "event_id": item.id, "raw_digest": content_id(item)}
                )
            )
        if not delayed_zero_import:
            import_broker_events(store, tuple(zeros))
        from quantbot.paper_core.contracts import RoundTrip
        from quantbot.paper_core.release import read_export

        process_outcomes(store, cutoff=simulated.now)
        before = RoundTrip.model_validate(read_export(db.path, decision.id)["current_outcome"])
        assert before.decision_id == decision.id
        if delayed_zero_import:
            assert before.status == "COSTS_PENDING" and before.actual_fees is None
        else:
            assert before.status == "COMPLETE" and before.actual_fees == 0
        late = NOW + timedelta(days=3)

        class ActivityProvider(ProviderFixture):
            async def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs.get("params")))
                return BrokerTransportResponse(
                    status_code=200,
                    headers={},
                    content=json.dumps(
                        [
                            {
                                "id": "late-unallocated-fee",
                                "activity_type": "FEE",
                                "net_amount": "-.10",
                                **(
                                    {"created_at": late.isoformat()}
                                    if precise_cash_time
                                    else {"date": late.date().isoformat()}
                                ),
                            }
                        ]
                    ).encode(),
                )

        provider = ActivityProvider(late)
        cfg = RuntimeConfig(
            repository=str(Path(__file__).resolve().parents[3]),
            store_path=str(db.path),
            store_identity=db.store_identity,
            expected_account_hash=hashlib.sha256(simulated.account_id.encode()).hexdigest(),
            core=store.config.model_copy(
                update={"release_id": "UNBOUND_PREPARED_RELEASE", "clock_profile": "PAPER_TIME_V2"}
            ),
            holdout_source=str(tmp_path / "canonical.db"),
            learning_origin=NOW,
            cost_contract=contract,
        )
        monkeypatch.setattr(
            "quantbot.paper_core.authority.commission_in_session",
            lambda *_: SimpleNamespace(approved_at=NOW - timedelta(hours=1), legacy_owners=()),
        )
        with db.transaction() as session:
            initial_id = content_id({"offline_initial_cursor": True})
            put(session, "core_provider_acquisitions", initial_id, {"offline_initial_cursor": True})
            session.execute(
                table("core_provider_progress")
                .insert()
                .values(
                    account_id=simulated.account_id,
                    watermark=(late - timedelta(seconds=1)).isoformat(),
                    document_id=initial_id,
                )
            )

        def adapter():
            return AlpacaCoreBroker(
                AlpacaPaperBrokerAdapter(
                    api_key=SecretStr("offline"),
                    api_secret=SecretStr("offline"),
                    transport=provider,
                ),
                None,
                None,
                cfg,
                SimpleNamespace(sample=lambda: (late, 1.0)),
                account_id=simulated.account_id,
            )

        broker = adapter()
        broker.cost_revision_batch_size = 1
        first = await broker.owned_events(store)
        if delayed_zero_import:
            assert not first
            broker.accept_activity_cursor(store)
            imported = import_broker_events(store, tuple(zeros))
            assert len(imported) == 2
            with db.transaction() as session:
                assert not current_costs(session, cutoff=late)
            process_outcomes(store, cutoff=late)
            after = RoundTrip.model_validate(read_export(db.path, decision.id)["current_outcome"])
            assert after.status == "COSTS_PENDING"
            assert after.actual_fees is None and after.net_pnl is None
            assert not import_broker_events(store, tuple(zeros))
            refusals = [
                d for d in store.documents("core_provider_acquisitions")
                if d.get("kind") == "CONTRACTUAL_ZERO_ADMISSION_REFUSED"
            ]
            assert len(refusals) == 2
            raw_sources = store.documents("core_broker_event_sources")
            assert {e.event_id for e in zeros}.issubset({e["event_id"] for e in raw_sources})
            restarted = adapter()
            assert not await restarted.owned_events(store)
            restarted.accept_activity_cursor(store)
            with db.transaction() as session:
                from quantbot.paper_core.contracts import LearningPolicy
                from quantbot.paper_core.learning import _select

                selected, excluded, considered = _select(
                    session,
                    LearningPolicy(dataset="EXPLICIT_SYNTHETIC", evidence_lane="SYNTHETIC_TEST"),
                    late,
                    execution_now=late,
                )
                assert not selected and considered
                assert any(reason == "COSTS_PENDING" for _, reason in excluded)
                assert _guard(
                    session, "EXPLICIT_SYNTHETIC", NOW.date(), late.date(), now=late.date()
                ) is None
            print("M6_FIX2_DELAYED_ZERO:" + json.dumps({
                "generated_zeros": [e.model_dump(mode="json") for e in zeros],
                "raw_sources": raw_sources,
                "refusals": refusals,
                "after": after.model_dump(mode="json"),
                "learning_exclusions": excluded,
                "guard_after_restart": None,
                "precise_cash_time": precise_cash_time,
                "scope": "MOCK_ONLY_NOT_ACTUAL_BROKER_AUTHORITY",
            }))
            return
        assert len(first) == 1 and first[0].cost.source == "UNKNOWN"

        # Replayed provider-derived cost economics in the explicitly SYNTHETIC ledger.
        def synthetic_cost_events(batch):
            converted = []
            for event in batch:
                cost = event.cost.model_copy(update={"evidence_lane": store.config.evidence_lane})
                converted.append(
                    event.model_copy(
                        update={
                            "evidence_lane": store.config.evidence_lane,
                            "cost": cost,
                            "event_id": cost.id,
                            "raw_digest": content_id(cost),
                        }
                    )
                )
            return tuple(converted)

        import_broker_events(store, synthetic_cost_events(first))
        broker.accept_activity_cursor(store)
        with db.transaction() as session:
            assert (
                _guard(session, "EXPLICIT_SYNTHETIC", NOW.date(), late.date(), now=late.date())
                == "COST_CONTRACT_CONFLICT_REVISIONS_PENDING"
            )
        restarted = adapter()
        restarted.cost_revision_batch_size = 1
        second = await restarted.owned_events(store)
        assert len(second) == 1 and second[0].cost.source == "UNKNOWN"
        assert first[0].cost.fill_id != second[0].cost.fill_id
        import_broker_events(store, synthetic_cost_events(second))
        restarted.accept_activity_cursor(store)
        with db.transaction() as session:
            costs = current_costs(session, cutoff=late)
            assert all(costs[(f.fill_id, "FEE")].source == "UNKNOWN" for f in simulated.fills)
            assert (
                _guard(session, "EXPLICIT_SYNTHETIC", NOW.date(), late.date(), now=late.date())
                == "COST_CONTRACT_CONFLICT_REVISIONS_PENDING"
            )
        after = process_outcomes(store, cutoff=late).outcomes[0]
        assert (
            after.status == "COSTS_PENDING" and after.actual_fees is None and after.net_pnl is None
        )
        assert after.supersedes == before.id
        for _ in range(10):
            assert not await restarted.owned_events(store)
            restarted.accept_activity_cursor(store)
            with db.transaction() as session:
                if (
                    _guard(session, "EXPLICIT_SYNTHETIC", NOW.date(), late.date(), now=late.date())
                    is None
                ):
                    break
        else:
            pytest.fail("finite conflict/outcome scan did not complete after catch-up")
        assert not await restarted.owned_events(store)
        assert all(c[0] == "GET" and c[1].endswith("/account/activities") for c in provider.calls)
        print(
            "M6_FIX1_LATE_FEE:"
            + json.dumps(
                {
                    "before": before.model_dump(mode="json"),
                    "after": after.model_dump(mode="json"),
                    "first_cost": first[0].cost.model_dump(mode="json"),
                    "second_cost": second[0].cost.model_dump(mode="json"),
                    "provider_calls": provider.calls,
                    "precise_cash_time": precise_cash_time,
                    "scope": "MOCK_ONLY_NOT_ACTUAL_BROKER_AUTHORITY",
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("different_scope", [None, "ACCOUNT", "CONTRACT"])
async def test_zero_import_admission_is_exact_and_replay_safe(tmp_path, different_scope):
    from tests.fixtures.core_m2 import NOW, events, fee_events, prepare

    from quantbot.paper_core.costs import current_costs
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.operational_costs import cost_contract_conflict_source
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.store import put, table
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import content_id

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, events(store, broker))
        contract_id = content_id({"EXPLICIT_SYNTHETIC_CONTRACT": True})
        generated = []
        for event in fee_events(store, broker):
            cost = event.cost.model_copy(
                update={"source": "CONTRACTUAL_ZERO", "document_id": contract_id}
            )
            generated.append(event.model_copy(
                update={"cost": cost, "event_id": cost.id, "raw_digest": cost.id}
            ))
        source = {"kind": "MOCK_FEE_CONTRADICTION", "raw_fee": "-.10"}
        source_id = content_id(source)
        with db.transaction() as session:
            put(session, "core_provider_acquisitions", source_id, source)
            session.execute(table("core_source_cursors").insert().values(
                source_id=cost_contract_conflict_source(
                    broker.account_id + ("-other" if different_scope == "ACCOUNT" else ""),
                    contract_id + ("-other" if different_scope == "CONTRACT" else ""),
                ),
                event_id=source_id,
                available_at=broker.now.isoformat(),
            ))
        assert len(import_broker_events(store, tuple(generated))) == 2
        assert not import_broker_events(store, tuple(generated))
        with db.transaction() as session:
            costs = current_costs(session, cutoff=broker.now)
            assert len(costs) == (0 if different_scope is None else 2)
        process_outcomes(store, cutoff=broker.now)
        refusals = [d for d in store.documents("core_provider_acquisitions")
            if d.get("kind") == "CONTRACTUAL_ZERO_ADMISSION_REFUSED"]
        assert len(refusals) == (2 if different_scope is None else 0)
        assert all(d["first_conflict_source"] == source_id for d in refusals)
        print("M6_FIX2_SCOPE:" + json.dumps({
            "different_scope": different_scope, "authoritative_cost_count": len(costs),
            "refusals": refusals, "generated_cost_ids": [e.cost.id for e in generated],
            "scope": "EXPLICIT_SYNTHETIC_IMPORT_SEAM",
        }))
    finally:
        db.close()
