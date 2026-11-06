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

from sqlalchemy import select

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
from quantbot.paper_core.store import CoreStore, table
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
            "transition-release",
            "transition-entry-policy",
            "resume",
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


def commissioned_core(release: PreparedRelease, recorded: dict[str, Any]) -> CoreConfig:
    """Validate executable binding before credential access; preserve original economic IDs."""
    from quantbot.paper_core.authority import ExecutableBinding

    if len(recorded["commissions"]) != 1 or len(recorded["economic_configs"]) != 1:
        raise ValueError("CORE_RUNTIME_UNCOMMISSIONED")
    commission = RuntimeCommission.model_validate(recorded["commissions"][0])
    economic = CoreConfig.model_validate(recorded["economic_configs"][0])
    binding = (
        ExecutableBinding.model_validate(recorded["executable_binding"])
        if recorded.get("executable_binding")
        else None
    )
    expected = derived_core(release, commission.account_id).model_copy(
        update={"release_id": commission.release_id}
    )
    if (
        economic != expected
        or content_id(economic) != commission.config_id
        or binding is None
        or binding.release != release
        or binding.commission_id != commission.id
    ):
        raise ValueError("CORE_RUNTIME_BINDING_MISMATCH")
    return economic


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
        if (
            release.config.entry_policy is not None
            and release.config.entry_policy.crypto is not None
        ):
            from quantbot.market_data.crypto import AlpacaCryptoDataClient

            broker.crypto_market = AlpacaCryptoDataClient(
                api_key=key,
                api_secret=secret,
                transport=market_transport,
                max_retries=0,
                timeout_seconds=Decimal(2),
                quote_price_bars=True,
            )
            await broker.initialize_crypto_assets()
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

    transport: Any
    market_transport: Any
    config = release.config
    from quantbot.paper_core.account_control import require_resource_control

    require_resource_control(config.entry_policy, config.request_control)
    clocks = SystemClocks()
    clocks.execution_domain = config.core.evidence_lane + ":HOST_UTC_RUNTIME_V1"
    settings = None
    resources: list[Any] = []
    if config.core.evidence_lane == "BROKER_PAPER":
        if args.fixture_driver:
            raise ValueError("fixture driver cannot authorize PAPER")
        recorded = read_status(Path(config.store_path))
        economic_core = commissioned_core(release, recorded)
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
    core = (
        economic_core
        if config.core.evidence_lane == "BROKER_PAPER"
        else derived_core(release, account_id)
    )
    observer = None
    if config.core.evidence_lane == "BROKER_PAPER":
        from quantbot.paper_core.sqlite_diagnostics import configured_diagnostics_deadline
        from quantbot.storage.transaction_diagnostics import stdout_transaction_observer

        deadline = configured_diagnostics_deadline(
            config.environment_file_reference, now=clocks.sample()[0]
        )
        if deadline is not None:
            observer = stdout_transaction_observer(
                expires_at_utc=deadline, process_epoch=clocks.process_id, role=args.role
            )
    with Database(Path(config.store_path), transaction_observer=observer) as db:
        if db.store_identity != config.store_identity:
            raise ValueError("store lineage mismatch")
        store = CoreStore(db, core, clocks=clocks, runtime_release_id=release.id)
        store.runtime_role = args.role
        store.runtime_submission_enabled = (
            config.submission_enabled and not args.submission_disabled
        )
        store.runtime_entry_enabled = not args.entry_disabled
        store.runtime_settings = settings
        store.runtime_account_control = config.request_control
        if settings is not None:
            with db.transaction() as session:
                commission_in_session(store, session)
            key, secret = settings.ALPACA_PAPER_API_KEY, settings.ALPACA_PAPER_API_SECRET
            if key is None or secret is None:
                raise ValueError("existing PAPER credentials unavailable")
            transport, market_transport = HttpxBrokerTransport(), HttpxMarketDataTransport()
            resources = [transport, market_transport]
            if config.request_control is not None:
                from quantbot.paper_core.account_control import BudgetedTransport

                transport = BudgetedTransport(transport, config.request_control, args.role, clocks)
                market_transport = BudgetedTransport(
                    market_transport, config.request_control, args.role, clocks
                )
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
                # Entry cohorts are larger reads; independent protection keeps its short bound.
                timeout_seconds=Decimal(5) if args.role == "trader" else Decimal(2),
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
            if config.entry_policy is not None and config.entry_policy.crypto is not None:
                from quantbot.market_data.crypto import AlpacaCryptoDataClient

                broker.crypto_market = AlpacaCryptoDataClient(
                    api_key=key,
                    api_secret=secret,
                    transport=market_transport,
                    max_retries=0,
                    timeout_seconds=Decimal(5) if args.role == "trader" else Decimal(2),
                    quote_price_bars=True,
                )
                unavailable = await broker.initialize_crypto_assets()
                if unavailable:
                    print(
                        json.dumps(
                            {
                                "event": "PAPER_CRYPTO_METADATA_UNAVAILABLE",
                                "role": args.role,
                                "scope": "SYMBOL",
                                "unavailable": unavailable,
                            }
                        ),
                        flush=True,
                    )
        else:
            module, name = args.fixture_driver.split(":", 1)
            broker = getattr(importlib.import_module(module), name)(store, config)
        lease = None
        try:
            if args.role == "trader" and config.request_control is not None:
                from quantbot.paper_core.account_control import AccountEntryLease

                lease = AccountEntryLease(config.request_control, db.store_identity, release.id)
                lease.acquire()
                with db.transaction() as session:
                    history = table("core_runtime_config_history")
                    encoded_binding = session.execute(
                        select(history.c.payload).where(history.c.id == lease.binding_id)
                    ).scalar_one()
                recorded_binding = json.loads(encoded_binding)
                if recorded_binding != lease.document:
                    raise ValueError("ACCOUNT_ENTRY_BINDING_AUDIT_MISMATCH")
                store.runtime_account_lease = lease
            with SingleWriterLock(Path(config.store_path + "." + args.role + ".lock")):
                await run_role(store, broker, config, cast(Role, args.role), cycles=args.cycles)
        finally:
            if lease is not None:
                lease.release()
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
    elif args.command in {
        "commission",
        "fence",
        "rollback-model",
        "transition-release",
        "transition-entry-policy",
        "resume",
    }:
        if args.approval_document is None or not args.approval_document.is_absolute():
            raise ValueError("explicit exact approved nonsecret document required")
        document = json.loads(args.approval_document.read_text(encoding="utf-8"))
        commission = RuntimeCommission.model_validate(document["commission"])
        if args.command == "commission":
            core = derived_core(release, commission.account_id)
            executable_id = release.id
        else:
            recorded = read_status(Path(config.store_path))
            core = CoreConfig.model_validate(recorded["economic_configs"][0])
            executable_id = recorded["executable_binding"]["release"]
            executable_id = PreparedRelease.model_validate(executable_id).id
            if args.command not in {"transition-release", "transition-entry-policy"}:
                commissioned_core(release, recorded)
        with Database(Path(config.store_path)) as db:
            store = CoreStore(db, core, runtime_release_id=executable_id)
            if args.command != "commission":
                with db.transaction() as session:
                    from quantbot.paper_core.authority import commission_in_session

                    if commission_in_session(store, session) != commission:
                        raise ValueError("exact current commission required")
            if args.command == "commission":
                identity = commission_runtime(
                    store, release, commission, baseline=document["baseline"]
                )
                print(json.dumps({"commission_id": identity, "release_id": release.id}))
            elif args.command == "transition-release":
                from quantbot.paper_core.authority import transition_release

                print(transition_release(store, release, document))
            elif args.command == "transition-entry-policy":
                from quantbot.paper_core.authority import transition_entry_policy

                print(transition_entry_policy(store, release, document))
            elif args.command == "resume":
                from quantbot.paper_core.authority import resume_entries

                print(resume_entries(store, document))
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
