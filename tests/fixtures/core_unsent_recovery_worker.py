"""Fresh-process proof of local unsent retirement and immutable old replay."""

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from tests.fixtures.core_m2 import NOW, JournalBroker  # noqa: E402

from quantbot.paper_core.contracts import (  # noqa: E402
    CoreConfig,
    Decision,
    ModelArtifact,
    Observation,
)
from quantbot.paper_core.guardian import guardian_cycle  # noqa: E402
from quantbot.paper_core.outcomes import recover_intents  # noqa: E402
from quantbot.paper_core.store import CoreStore  # noqa: E402
from quantbot.paper_core.trader import trader_cycle  # noqa: E402
from quantbot.storage import Database, StorageRepository  # noqa: E402


async def run(path):
    broker = JournalBroker(path / "broker.json")
    broker.now = NOW + timedelta(minutes=3)
    with Database(path / "core.db") as db:
        store = CoreStore(db, CoreConfig(account_id="synthetic-account", symbol="XLE"))
        old_link = store.links()[0]
        original = store.read("core_decisions", old_link["decision_id"], Decision)
        old_observation = store.read("core_observations", old_link["observation_id"], Observation)
        await recover_intents(store, broker)
        await guardian_cycle(store, broker, now=broker.now)
        fresh = old_observation.model_copy(
            update={
                "recorded_at_utc": broker.now,
                "available_at": broker.now,
                "source_start": broker.now - timedelta(minutes=5),
                "source_end": broker.now,
                "dependency_available_at": (broker.now,),
                "sequence": 2,
                "raw_data_digest": "restart-fresh-reuse",
            }
        )
        await trader_cycle(
            store, broker, fresh, ModelArtifact(), store.latest_proof(), now=broker.now
        )
        replay = await trader_cycle(
            store, broker, old_observation, ModelArtifact(), store.latest_proof(), now=broker.now
        )
        assert replay.id == original.id
        with db.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.get_order_intent(old_link["intent_id"])
            refusal = [
                e.detail
                for e in repo.list_order_events(intent_id=intent.intent_id)
                if e.event_type == "CORE_ENTRY_SEND_REFUSED"
            ]
        broker.save()
        print(
            "M2_FRESH_UNSENT:"
            + json.dumps(
                {
                    "old_intent": intent.intent_id,
                    "old_state": intent.state.value,
                    "old_decision": original.id,
                    "replayed_decision": replay.id,
                    "refusal": refusal,
                    "post_ids": [i.intent_id for i in broker.posts],
                    "post_count": len(broker.posts),
                    "links": len(store.links()),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1])))
