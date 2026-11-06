"""Existing observational research task: free sources, separate store, no order interface."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantbot.config import Settings
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.transports import HttpxMarketDataTransport
from quantbot.operations.service import isolated_application_environment
from quantbot.paper_core.account_control import BudgetedTransport
from quantbot.paper_core.context_review_producer import run_review_producer
from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.discovery_research import run_research
from quantbot.paper_core.release import PreparedRelease, verify_release
from quantbot.paper_core.time_evidence import SystemClocks
from quantbot.research.manifest import canonical_result_json
from quantbot.storage import SingleWriterLock


def verify_seal(path: Path) -> dict[str, Any]:
    seal: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    repository = Path(seal["repository"]).resolve()
    if not seal["files"]:
        raise ValueError("RESEARCH_SOURCE_SEAL_EMPTY")
    for name, digest in seal["files"].items():
        source = (repository / name).resolve()
        if (
            not source.is_relative_to(repository)
            or hashlib.sha256(source.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("RESEARCH_SOURCE_SEAL_MISMATCH")
    return seal


def sync_catalog(source: Path, state: DiscoveryStore, now: datetime) -> None:
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1) as c:
        c.execute("begin")
        assets = [json.loads(r[0]) for r in c.execute("select payload from assets")]
        origin = json.loads(
            c.execute("select payload from metadata where key='catalog'").fetchone()[0]
        )
    if not assets or any(not isinstance(a.get("symbol"), str) for a in assets):
        raise ValueError("SOURCE_RESEARCH_CATALOG_INVALID")
    with state.connection() as c:
        c.execute("begin immediate")
        c.execute("delete from assets")
        c.executemany(
            "insert into assets values(?,?)",
            [(a["symbol"], canonical_result_json(a)) for a in assets],
        )
        receipt = dict(
            kind="RESEARCH_CATALOG_COPY_V1",
            source_catalog_id=origin["id"],
            source_catalog_at=origin["at"],
            copied_at=now.isoformat(),
            source_returned=origin["returned"],
            copied_eligible=len(assets),
        )
        receipt["id"] = state._receipt(c, receipt)
        state._set(c, "catalog", receipt)


async def run(manifest: Path, seal_path: Path, settings_path: Path) -> None:
    seal = verify_seal(seal_path)
    release = PreparedRelease.model_validate_json(manifest.read_text(encoding="utf-8-sig"))
    if verify_release(release)["objections"]:
        raise ValueError("TRADING_SOURCE_NOT_VERIFIED")
    config = release.config
    assert config.environment_file_reference is not None and config.request_control is not None
    state_path = Path(config.store_path).with_suffix(".research.db")
    stock_path = Path(config.store_path).with_suffix(".discovery.db")
    assert state_path != stock_path and state_path != Path(config.store_path)
    with SingleWriterLock(state_path.with_suffix(".research.lock")):
        state = DiscoveryStore(state_path)
        with isolated_application_environment(config.environment_file_reference):
            research_settings = json.loads(settings_path.read_text(encoding="utf-8"))
            contact = research_settings["sec_contact"]
            if (
                not isinstance(contact, str)
                or not contact.strip()
                or "\n" in contact
                or "\r" in contact
            ):
                raise ValueError("DECLARED_SEC_CONTACT_REQUIRED")
            os.environ["QUANTBOT_SEC_CONTACT"] = contact
            os.environ["QUANTBOT_FREE_NEWS_CONFIRMED"] = (
                "1" if research_settings.get("news_free_entitlement_confirmed") is True else "0"
            )
            settings = Settings()
            if str(settings.TRADING_MODE) != "PAPER" or settings.LIVE_TRADING_ACKNOWLEDGED:
                raise ValueError("RESEARCH_PAPER_ONLY")
            assert settings.ALPACA_PAPER_API_KEY is not None
            assert settings.ALPACA_PAPER_API_SECRET is not None
            transport = HttpxMarketDataTransport()
            clocks = SystemClocks()
            market = AlpacaMarketDataClient(
                api_key=settings.ALPACA_PAPER_API_KEY,
                api_secret=settings.ALPACA_PAPER_API_SECRET,
                transport=BudgetedTransport(transport, config.request_control, "trader", clocks),
                max_retries=0,
            )

            class Reader:
                def __init__(self) -> None:
                    self.market = market

                @property
                def now(self) -> datetime:
                    return datetime.now(UTC)

            async def catalogs() -> None:
                while True:
                    try:
                        sync_catalog(stock_path, state, datetime.now(UTC))
                    except Exception as error:
                        print(
                            json.dumps(
                                dict(
                                    kind="RESEARCH_CATALOG_UNAVAILABLE",
                                    at=datetime.now(UTC).isoformat(),
                                    error=type(error).__name__,
                                )
                            ),
                            flush=True,
                        )
                        await asyncio.sleep(60)
                    else:
                        await asyncio.sleep(3600)

            task = asyncio.create_task(catalogs())
            reviews = asyncio.create_task(run_review_producer(state))
            print(
                json.dumps(
                    dict(
                        kind="OBSERVATIONAL_RESEARCH_STARTED",
                        source_commit=seal["commit"],
                        order_authority=False,
                        state_path=str(state_path),
                    )
                ),
                flush=True,
            )
            try:
                await run_research(Reader(), state)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                reviews.cancel()
                try:
                    await reviews
                except asyncio.CancelledError:
                    pass
                await transport.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--seal", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.manifest, args.seal, args.settings))
