"""Explicit future read-only commissioning acquisition; preparation never calls this."""

import asyncio
from pathlib import Path
from typing import Any

from quantbot.paper_core.authority import RuntimeCommission
from quantbot.paper_core.canonical import snapshot_canonical_metadata
from quantbot.paper_core.release import PreparedRelease, readonly_connection
from quantbot.research.manifest import content_id


async def acquire_commission_document(
    release: PreparedRelease,
    broker: Any,
    request: dict[str, Any],
) -> dict[str, Any]:
    from quantbot.paper_core.__main__ import derived_core

    config = release.config
    if (
        request.get("release_id") != release.id
        or not request.get("approval_reference")
        or not request.get("fenced_writer_evidence_ids")
    ):
        raise ValueError("exact release approval and fenced writer evidence required")
    async with asyncio.timeout(5):
        first = await broker.get_account()
        positions = tuple(await broker.get_positions())
        open_orders = tuple(await broker.get_open_orders())
        second = await broker.get_account()
        second_positions = tuple(await broker.get_positions())
    if first != second or positions != second_positions or open_orders:
        raise ValueError("fresh stable account/positions with no pending orders required")
    account = first.account
    if account.account_id != broker.account_id:
        raise ValueError("fresh account identity mismatch")
    core = derived_core(release, account.account_id)
    owner_map = dict(request["legacy_owners"])
    inherited = tuple(sorted((p.symbol, owner_map[p.symbol]) for p in positions if p.quantity != 0))
    if any(not owner for _, owner in inherited):
        raise ValueError("explicit inherited owner provenance required")
    with readonly_connection(Path(config.store_path)) as connection:
        identity = dict(connection.execute("SELECT * FROM schema_version").fetchone())
        stop = dict(connection.execute("SELECT * FROM kill_switch_state").fetchone())
        if connection.execute(
            "SELECT intent_id FROM order_intents "
            "WHERE state IN ('SUBMITTING','SUBMISSION_UNKNOWN') LIMIT 1"
        ).fetchone():
            raise ValueError("original unresolved submission must recover before commissioning")
    if identity["store_identity"] != config.store_identity:
        raise ValueError("commissioning store lineage mismatch")
    canonical = snapshot_canonical_metadata(Path(config.holdout_source), config.canonical_datasets)
    now = broker.now
    source = {
        "provider": "alpaca",
        "endpoint": broker.endpoint,
        "account": account,
        "positions": [p.model_dump(mode="json") for p in positions],
        "open_orders": open_orders,
        "captured_at": now.isoformat(),
        "acquisition": "TWO_EQUAL_BOUNDED_RETAIL_READS",
        "store": identity,
    }
    source_id = content_id(source)
    baseline = {
        "source": "BROKER_PAPER_APPROVED_INITIAL_STATE",
        "provider": "alpaca",
        "endpoint": broker.endpoint,
        "account": account.model_dump(mode="json"),
        "positions": [p.model_dump(mode="json") for p in positions],
        "quantities": tuple(
            sorted((p.symbol, str(p.quantity)) for p in positions if p.quantity != 0)
        ),
        "source_ids": (source_id,),
        "captured_at": now.isoformat(),
        "recorded_stop": stop,
        "canonical_metadata": canonical,
        "provider_acquisition": source,
        "fenced_writer_evidence_ids": request["fenced_writer_evidence_ids"],
        "unknown_orders": [],
    }
    commission = RuntimeCommission(
        release_id=release.id,
        config_id=content_id(core),
        store_identity=config.store_identity,
        store_path=config.store_path,
        account_id=account.account_id,
        owner=core.owner,
        generation=core.generation,
        approved_at=now,
        approval_reference=request["approval_reference"],
        initial_model_id=config.initial_model.id,
        activation_policy=config.activation_policy,
        learning_origin=config.learning_origin,
        legacy_owners=inherited,
        baseline_source_ids=(source_id,),
        canonical_metadata_id=content_id(canonical),
    )
    return {
        "commission": commission.model_dump(mode="json"),
        "baseline": baseline,
        "scope": "READ_ONLY_ACQUISITION_NOT_COMMISSIONED_NOT_SUBMITTED",
    }
