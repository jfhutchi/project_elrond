"""Reproducible M1 synthetic joined trace export. Only a TemporaryDirectory store is written."""
import asyncio
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.trader import trader_cycle
from quantbot.research.manifest import canonical_result_json
from tests.integration.paper_core.test_thin_loop import NOW, prepare


async def run() -> None:
    with TemporaryDirectory(prefix="paper-core-m1-evidence-") as directory:
        db, store, broker, proof, observation, model = prepare(Path(directory), inherited=True)
        try:
            decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
            broker.now = NOW + timedelta(minutes=30)
            await guardian_cycle(store, broker, now=broker.now)
            print(canonical_result_json({"model": model, "bundle": store.export_loop(decision.id),
                "provider_posts": broker.posts,
                "final_provider_account": broker.snapshot().account,
                "final_provider_positions": broker.position_snapshot()}))
        finally:
            db.close()


if __name__ == "__main__":
    asyncio.run(run())
