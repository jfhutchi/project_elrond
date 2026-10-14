"""Supported PAPER runtime roles and explicit read-only/preparation commands."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from quantbot.config import Settings
from quantbot.paper_core.authority import RuntimeCommission, commission_runtime
from quantbot.paper_core.contracts import CoreConfig
from quantbot.paper_core.release import (
    PreparedRelease,
    RuntimeConfig,
    prepare_release,
    read_export,
    read_status,
    verify_release,
)
from quantbot.paper_core.store import CoreStore
from quantbot.paper_core.time_evidence import SystemClocks
from quantbot.research.manifest import content_id
from quantbot.storage import Database
from quantbot.storage.lock import SingleWriterLock


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Explicit PAPER preparation and three-role runtime"
    )
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--manifest", type=Path)
    result.add_argument("--role", choices=("trader", "guardian", "outcomes"))
    result.add_argument(
        "--command",
        choices=(
            "prepare",
            "verify",
            "status",
            "export",
            "migrate",
            "acquire",
            "commission",
            "fence",
            "rollback-model",
        ),
    )
    result.add_argument("--decision-id")
    result.add_argument("--approval-document", type=Path)
    result.add_argument("--cycles", type=int)
    result.add_argument("--fixture-driver")
    result.add_argument(
        "--submission-disabled",
        action="store_true",
        help="disable-only preflight clamp; never enables a disabled config",
    )
    result.add_argument(
        "--entry-disabled",
        action="store_true",
        help="disable only new entries; independent protection continues",
    )
    return result


def load_config(path: Path) -> RuntimeConfig:
    if not path.is_absolute():
        raise ValueError("absolute config path required")
    return RuntimeConfig.model_validate_json(path.read_text(encoding="utf-8"))


def derived_core(release: PreparedRelease, account_id: str) -> CoreConfig:
    digest = hashlib.sha256(account_id.encode()).hexdigest()
    if digest != release.config.expected_account_hash:
        raise ValueError("configured account hash mismatch")
    return release.config.core.model_copy(
        update={"release_id": release.id, "account_id": account_id}
    )


async def _acquire(args: argparse.Namespace, release: PreparedRelease) -> None:
    from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
    from quantbot.brokers.transports import HttpxBrokerTransport
    from quantbot.market_data.alpaca import AlpacaMarketDataClient
    from quantbot.market_data.calendar import AlpacaMarketCalendar
    from quantbot.market_data.transports import HttpxMarketDataTransport
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.commissioning import acquire_commission_document
    from quantbot.research.manifest import canonical_result_json

    if args.approval_document is None or not args.approval_document.is_absolute():
        raise ValueError("exact approved acquisition request required")
    request = json.loads(args.approval_document.read_text(encoding="utf-8"))
    if request.get("release_id") != release.id or not request.get("approval_reference"):
        raise ValueError("exact release acquisition approval required")
    settings = Settings(_env_file=release.config.environment_file_reference)
    key, secret = settings.ALPACA_PAPER_API_KEY, settings.ALPACA_PAPER_API_SECRET
    if not settings.EXPECTED_ACCOUNT_ID or key is None or secret is None:
        raise ValueError("existing PAPER authority unavailable")
    from quantbot.config import validate_trading_authority

    if (
        settings.TRADING_MODE.value != "PAPER"
        or not validate_trading_authority(settings, settings.EXPECTED_ACCOUNT_ID).allowed
    ):
        raise ValueError("PAPER acquisition authority refused")
    derived_core(release, settings.EXPECTED_ACCOUNT_ID)
    transport, market_transport = HttpxBrokerTransport(), HttpxMarketDataTransport()
    try:
        adapter = AlpacaPaperBrokerAdapter(
            api_key=key,
            api_secret=secret,
            transport=transport,
            max_retries=0,
            timeout_seconds=Decimal(2),
        )
        market = AlpacaMarketDataClient(
            api_key=key,
            api_secret=secret,
            transport=market_transport,
            max_retries=0,
            timeout_seconds=Decimal(2),
        )
        calendar = AlpacaMarketCalendar(
            api_key=key,
            api_secret=secret,
            transport=market_transport,
            max_retries=0,
            timeout_seconds=Decimal(2),
        )
        broker = AlpacaCoreBroker(
            adapter,
            market,
            calendar,
            release.config,
            SystemClocks(),
            account_id=settings.EXPECTED_ACCOUNT_ID,
        )
        document = await acquire_commission_document(release, broker, request)
        print(canonical_result_json(document))
    finally:
        await transport.aclose()
        await market_transport.aclose()


async def _run(args: argparse.Namespace, release: PreparedRelease) -> None:
    from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
    from quantbot.brokers.transports import HttpxBrokerTransport
    from quantbot.market_data.alpaca import AlpacaMarketDataClient
    from quantbot.market_data.calendar import AlpacaMarketCalendar
    from quantbot.market_data.transports import HttpxMarketDataTransport
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.authority import commission_in_session
    from quantbot.paper_core.roles import Role, run_role

    config = release.config
    clocks = SystemClocks()
    clocks.execution_domain = config.core.evidence_lane + ":HOST_UTC_RUNTIME_V1"
    settings = None
    resources: list[Any] = []
    if config.core.evidence_lane == "BROKER_PAPER":
        if args.fixture_driver:
            raise ValueError("fixture driver cannot authorize PAPER")
        recorded = read_status(Path(config.store_path))
        if len(recorded["commissions"]) != 1:
            raise ValueError("CORE_RUNTIME_UNCOMMISSIONED")
        recorded_commission = RuntimeCommission.model_validate(recorded["commissions"][0])
        if (
            recorded_commission.release_id != release.id
            or recorded_commission.config_id
            != content_id(derived_core(release, recorded_commission.account_id))
        ):
            raise ValueError("CORE_RUNTIME_BINDING_MISMATCH")
        # Sole existing Settings credential source, reached only by separately commissioned runtime.
        settings = Settings(_env_file=config.environment_file_reference)
        if not settings.EXPECTED_ACCOUNT_ID:
            raise ValueError("existing expected account identity required")
        account_id = settings.EXPECTED_ACCOUNT_ID
        from quantbot.config import validate_trading_authority

        if (
            settings.TRADING_MODE.value != "PAPER"
            or not validate_trading_authority(settings, account_id).allowed
        ):
            raise ValueError("exact existing PAPER Settings authority required")
    else:
        if not args.fixture_driver or config.environment_file_reference:
            raise ValueError("explicit credential-free fixture driver required")
        account_id = config.core.account_id
    core = derived_core(release, account_id)
    with Database(Path(config.store_path)) as db:
        if db.store_identity != config.store_identity:
            raise ValueError("store lineage mismatch")
        store = CoreStore(db, core, clocks=clocks)
        store.runtime_role = args.role
        store.runtime_submission_enabled = (
            config.submission_enabled and not args.submission_disabled
        )
        store.runtime_entry_enabled = not args.entry_disabled
        store.runtime_settings = settings
        if settings is not None:
            with db.transaction() as session:
                commission_in_session(store, session)
            key, secret = settings.ALPACA_PAPER_API_KEY, settings.ALPACA_PAPER_API_SECRET
            if key is None or secret is None:
                raise ValueError("existing PAPER credentials unavailable")
            transport, market_transport = HttpxBrokerTransport(), HttpxMarketDataTransport()
            resources = [transport, market_transport]
            adapter = AlpacaPaperBrokerAdapter(
                api_key=key,
                api_secret=secret,
                transport=transport,
                max_retries=0,
                timeout_seconds=Decimal(2),
            )
            market = AlpacaMarketDataClient(
                api_key=key,
                api_secret=secret,
                transport=market_transport,
                max_retries=0,
                timeout_seconds=Decimal(2),
            )
            calendar = AlpacaMarketCalendar(
                api_key=key,
                api_secret=secret,
                transport=market_transport,
                max_retries=0,
                timeout_seconds=Decimal(2),
            )
            broker: Any = AlpacaCoreBroker(
                adapter, market, calendar, config, clocks, account_id=account_id
            )
            broker.store = store
        else:
            module, name = args.fixture_driver.split(":", 1)
            broker = getattr(importlib.import_module(module), name)(store, config)
        try:
            with SingleWriterLock(Path(config.store_path + "." + args.role + ".lock")):
                await run_role(store, broker, config, cast(Role, args.role), cycles=args.cycles)
        finally:
            for resource in resources:
                await resource.aclose()


def main() -> None:
    args = parser().parse_args()
    config = load_config(args.config)
    if args.role and args.command or not args.role and not args.command:
        raise ValueError("select exactly one role or command")
    if args.command in {"status", "export"}:
        result = (
            read_status(Path(config.store_path))
            if args.command == "status"
            else read_export(Path(config.store_path), args.decision_id or "")
        )
        print(json.dumps(result, sort_keys=True))
        return
    if args.command == "prepare":
        print(prepare_release(config).model_dump_json())
        return
    if args.manifest is None or not args.manifest.is_absolute():
        raise ValueError("exact absolute manifest required")
    release = PreparedRelease.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    if release.config != config:
        raise ValueError("manifest/config mismatch")
    evidence = verify_release(release)
    if evidence["objections"]:
        raise ValueError(str(evidence["objections"]))
    if args.command == "verify":
        print(json.dumps(evidence, sort_keys=True))
    elif args.command == "acquire":
        asyncio.run(_acquire(args, release))
    elif args.command == "migrate":
        with Database.migrate(Path(config.store_path)) as db:
            print(
                json.dumps(
                    {
                        "store_identity": db.store_identity,
                        "schema": release.schema_version,
                        "release_id": release.id,
                        "broker_state": "NOT_PROBED",
                    }
                )
            )
    elif args.command in {"commission", "fence", "rollback-model"}:
        if args.approval_document is None or not args.approval_document.is_absolute():
            raise ValueError("explicit exact approved nonsecret document required")
        document = json.loads(args.approval_document.read_text(encoding="utf-8"))
        commission = RuntimeCommission.model_validate(document["commission"])
        core = derived_core(release, commission.account_id)
        with Database(Path(config.store_path)) as db:
            store = CoreStore(db, core)
            if args.command == "commission":
                identity = commission_runtime(
                    store, release, commission, baseline=document["baseline"]
                )
                from quantbot.paper_core.activation import initialize_operational_model

                initialize_operational_model(store, config.initial_model, at=commission.approved_at)
                print(json.dumps({"commission_id": identity, "release_id": release.id}))
            elif args.command == "fence":
                from quantbot.paper_core.authority import fence_entries

                print(
                    fence_entries(
                        store,
                        reason=document["reason"],
                        evidence_ids=tuple(document["evidence_ids"]),
                    )
                )
            else:
                from quantbot.paper_core.activation import rollback_model
                from quantbot.paper_core.authority import commission_in_session

                with db.transaction() as session:
                    current = commission_in_session(store, session)
                    if current != commission or not document.get("approval_reference"):
                        raise ValueError("exact current commission and rollback approval required")
                event = rollback_model(
                    store,
                    expected_model_id=document["expected_model_id"],
                    target_id=document["target_model_id"],
                    expected_activation_id=document["expected_activation_id"],
                    reason=document["reason"],
                    at=store.clocks.sample()[0],
                )
                print(event.model_dump_json())
    else:
        asyncio.run(_run(args, release))


if __name__ == "__main__":
    main()
