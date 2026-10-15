"""Deterministic clocks/provider samples; no external transport or credentials."""

from datetime import timedelta

from tests.fixtures.core_m2 import NOW
from tests.fixtures.core_m2 import prepare as prepare_m2


class Clocks:
    process_id = "m3-process-1"

    def __init__(self):
        self.wall = NOW
        self.mono = 100.0

    def sample(self):
        return self.wall, self.mono

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def prepare(path):
    from sqlalchemy import update

    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.store import CoreStore, table
    from quantbot.research.manifest import canonical_result_json, content_id

    db, old, broker, proof, observation, model = prepare_m2(path)
    config = CoreConfig(
        account_id=old.config.account_id, symbol="XLE", clock_profile="PAPER_TIME_V2"
    )
    # Explicit fixture commissioning, before any decision/claim; never production migration.
    with db.transaction() as session:
        session.execute(
            update(table("core_identities")).values(
                id=content_id(config), payload=canonical_result_json(config)
            )
        )
    clocks = Clocks()
    store = CoreStore(db, config, clocks=clocks)
    broker.clock_fault = None
    broker.offset = 0
    broker.clock_session_open = NOW - timedelta(hours=1)
    broker.clock_session_close = NOW + timedelta(hours=6)
    broker.clock_session_id = "2026-10-14"

    async def get_clock():
        from quantbot.paper_core.time_evidence import BrokerTime

        if broker.clock_fault:
            raise broker.clock_fault
        clocks.advance(0.01)
        broker.now = NOW + timedelta(seconds=clocks.mono - 100)
        return BrokerTime(
            timestamp=broker.now + timedelta(seconds=broker.offset),
            account_id=broker.account_id,
            provider=broker.provider,
            session_id=broker.clock_session_id,
            session_open=broker.clock_session_open,
            session_close=broker.clock_session_close,
            resolution_seconds=0.001,
        )

    broker.get_clock = get_clock
    return db, store, broker, proof, observation, model, clocks


def fresh(observation, clocks, *, sequence=2):
    return observation.model_copy(
        update={
            "recorded_at_utc": clocks.wall,
            "source_end": clocks.wall,
            "source_start": clocks.wall - timedelta(minutes=5),
            "available_at": clocks.wall,
            "dependency_available_at": (clocks.wall,),
            "sequence": sequence,
            "raw_data_digest": f"m3-bar-{sequence}",
        }
    )
