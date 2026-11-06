"""Broker fee evidence: asset debits are neither USD fees nor exact fill allocations."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.brokers.base import BrokerModel, BrokerProtocolError
from quantbot.research.manifest import content_id


def _finite(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation) as error:
        raise BrokerProtocolError("FEE_ACTIVITY_INVALID_" + field.upper()) from error
    if not result.is_finite():
        raise BrokerProtocolError("FEE_ACTIVITY_NONFINITE_" + field.upper())
    return result


def _timestamp(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise BrokerProtocolError("FEE_ACTIVITY_INVALID_" + field.upper()) from error
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(UTC)


class CryptoFeeActivity(BrokerModel):
    """An immutable observation, with no claim that an account fee belongs to a lot."""

    contract: Literal["ALPACA_FEE_ACTIVITY_V1"] = "ALPACA_FEE_ACTIVITY_V1"
    activity_id: str
    activity_type: Literal["CFEE", "FEE"]
    raw_symbol: str | None
    symbol: str | None
    asset_currency: str | None
    quantity_delta: Decimal | None
    net_cash: Decimal
    reported_price: Decimal | None
    raw_date: str | None
    time_precision: Literal["TIMESTAMP", "DATE", "UNKNOWN"]
    effective_at: datetime | None
    posted_at: datetime | None
    status: str | None
    fill_id: str | None
    order_id: str | None
    source_document_id: str
    raw_digest: str

    @property
    def id(self) -> str:
        return content_id(self)

    def external_quantity_event(
        self, *, account_id: str, baseline_at: datetime
    ) -> dict[str, Any] | None:
        """Only an explicitly timed, executed USD-pair asset debit changes custody ledger.

        Posting time and a calendar date cannot locate a debit relative to commissioning.
        This remains an account event; it never supplies exact-fill economic authority.
        """
        if (
            self.activity_type != "CFEE"
            or self.symbol is None
            or self.asset_currency is None
            or self.quantity_delta is None
            or self.quantity_delta == 0
            or self.net_cash != 0
            or self.status != "executed"
            or self.effective_at is None
            or self.effective_at <= baseline_at
        ):
            return None
        return dict(
            event_id=self.activity_id,
            symbol=self.symbol,
            owner="ACCOUNT_CRYPTO_FEE",
            quantity_delta=str(self.quantity_delta),
            net_cash="0",
            source_document=self.source_document_id,
            account_id=account_id,
            occurred_at=self.effective_at.isoformat(),
            activity_type=self.activity_type,
            asset_currency=self.asset_currency,
            raw_digest=self.raw_digest,
        )


def parse_fee_activity(
    raw: Mapping[str, object], *, symbol_aliases: Mapping[str, str], source_document_id: str
) -> CryptoFeeActivity:
    kind = raw.get("activity_type")
    if kind not in {"CFEE", "FEE"} or not raw.get("id"):
        raise BrokerProtocolError("FEE_ACTIVITY_ID_OR_TYPE_INVALID")
    net_cash = _finite(raw.get("net_amount"), "net_amount")
    quantity = _finite(raw["qty"], "qty") if raw.get("qty") is not None else None
    price = _finite(raw["price"], "price") if raw.get("price") is not None else None
    raw_symbol = str(raw["symbol"]) if raw.get("symbol") else None
    symbol = symbol_aliases.get(raw_symbol or "")
    if symbol is not None and (not symbol.endswith("/USD") or symbol.count("/") != 1):
        raise BrokerProtocolError("FEE_ACTIVITY_UNSUPPORTED_PAIR")
    raw_date = str(raw["date"]) if raw.get("date") else None
    effective = _timestamp(raw.get("transaction_time"), "transaction_time")
    precision: Literal["TIMESTAMP", "DATE", "UNKNOWN"] = "UNKNOWN"
    if raw_date:
        try:
            date.fromisoformat(raw_date)
            precision = "DATE"
        except ValueError:
            dated = _timestamp(raw_date, "date")
            if dated is not None and effective is None:
                effective = dated
    if effective is not None:
        precision = "TIMESTAMP"
    return CryptoFeeActivity(
        activity_id=str(raw["id"]),
        activity_type=kind,
        raw_symbol=raw_symbol,
        symbol=symbol,
        asset_currency=symbol.split("/")[0] if symbol and kind == "CFEE" else None,
        quantity_delta=quantity,
        net_cash=net_cash,
        reported_price=price,
        raw_date=raw_date,
        time_precision=precision,
        effective_at=effective,
        posted_at=_timestamp(raw.get("created_at"), "created_at"),
        status=str(raw["status"]) if raw.get("status") else None,
        fill_id=str(raw["fill_id"]) if raw.get("fill_id") else None,
        order_id=str(raw["order_id"]) if raw.get("order_id") else None,
        source_document_id=source_document_id,
        raw_digest=content_id(dict(raw)),
    )


def nonzero_fee_activity(raw: Mapping[str, object]) -> bool:
    """Zero USD cash does not prove zero crypto fee; malformed values fail closed."""
    if raw.get("activity_type") not in {"CFEE", "FEE"}:
        return False
    cash = _finite(raw.get("net_amount"), "net_amount")
    quantity = _finite(raw["qty"], "qty") if raw.get("qty") is not None else Decimal(0)
    return cash != 0 or raw.get("activity_type") == "CFEE" and quantity != 0


def retain_fee_activity(
    session: Session, evidence: CryptoFeeActivity, *, account_id: str, received_at: datetime
) -> CryptoFeeActivity:
    """Retain immutable provider identity; contradictory replay is not a revision."""
    from quantbot.paper_core.store import put, table
    from quantbot.storage import StateConflictError

    cursor = table("core_source_cursors")
    key = content_id(
        {"kind": "ALPACA_FEE_ACTIVITY", "account": account_id, "activity": evidence.activity_id}
    )
    old_id = session.execute(
        select(cursor.c.event_id).where(cursor.c.source_id == key)
    ).scalar_one_or_none()
    if old_id is not None:
        encoded = session.execute(
            select(table("core_provider_acquisitions").c.payload).where(
                table("core_provider_acquisitions").c.id == old_id
            )
        ).scalar_one()
        old = CryptoFeeActivity.model_validate_json(encoded)
        if old.raw_digest != evidence.raw_digest or (
            old.symbol is not None and evidence.symbol is not None and old.symbol != evidence.symbol
        ):
            raise StateConflictError("FEE_ACTIVITY_CONFLICT")
        if old.symbol is not None or evidence.symbol is None:
            return old
        # Later broker asset metadata can explain the unchanged raw symbol. Keep both receipts.
        session.execute(
            cursor.update()
            .where(cursor.c.source_id == key)
            .values(event_id=evidence.id, available_at=received_at.isoformat())
        )
    else:
        session.execute(
            cursor.insert().values(
                source_id=key, event_id=evidence.id, available_at=received_at.isoformat()
            )
        )
    put(session, "core_provider_acquisitions", evidence.id, evidence)
    return evidence
