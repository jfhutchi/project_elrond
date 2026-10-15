"""Test-only separate guardian process consuming an independently saved synthetic broker state."""

import asyncio
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from quantbot.domain import Account, BrokerOrder, Fill, Position
from quantbot.paper_core.contracts import CoreConfig
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore
from quantbot.research.manifest import canonical_result_json
from quantbot.storage import Database


def main() -> None:
    state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    now = datetime.fromisoformat(state["now"])
    broker = SyntheticBroker(
        account_id=state["account_id"],
        now=now,
        initial_account=Account.model_validate(state["initial_account"]),
        initial_positions=tuple(Position.model_validate(p) for p in state["initial_positions"]),
    )
    broker.cash = Decimal(state["cash"])
    broker.quantities = {s: Decimal(q) for s, q in state["quantities"].items()}
    broker.basis = {s: Decimal(p) for s, p in state["basis"].items()}
    broker.orders = {s: BrokerOrder.model_validate(o) for s, o in state["orders"].items()}
    broker.fills = [Fill.model_validate(f) for f in state["fills"]]
    with Database(sys.argv[2]) as db:
        store = CoreStore(db, CoreConfig.model_validate(state["config"]))
        receipt = asyncio.run(guardian_cycle(store, broker, now=now))
        print(
            canonical_result_json(
                {
                    "receipt": receipt,
                    "new_posts": broker.posts,
                    "bundle": store.export_loop(state["decision_id"]),
                }
            )
        )


if __name__ == "__main__":
    main()
