"""Exact component applicability and independent cash; unknown never becomes zero."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from quantbot.brokers.base import BrokerModel
from quantbot.domain import Fill
from quantbot.paper_core.contracts import CostItem
from quantbot.research.manifest import content_id


def pending_cost_revision_source(account_id: str) -> str:
    return content_id({"kind": "OWNED_COST_CONTRACT_REVISIONS_PENDING", "account": account_id})


class PaperCostComponent(BrokerModel):
    component: Literal["COMMISSION", "REGULATORY", "BORROW", "CLEARING", "OTHER"]
    document_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)
    applicable_account_evidence_id: str = Field(min_length=1)
    amount: Literal[0] = 0


class PaperCostContract(BrokerModel):
    version: Literal["paper-component-zero-v1"] = "paper-component-zero-v1"
    account_id: str
    endpoint: Literal["https://paper-api.alpaca.markets"]
    asset_class: Literal["US_LISTED_EQUITY"]
    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    components: tuple[PaperCostComponent, ...]

    @property
    def id(self) -> str:
        return content_id(self)

    def fee(self, fill: Fill, *, account_id: str, at: datetime, release_id: str) -> CostItem | None:
        if (
            account_id != self.account_id
            or fill.symbol not in self.symbols
            or not self.start <= fill.occurred_at <= self.end
            or {c.component for c in self.components}
            != {"COMMISSION", "REGULATORY", "BORROW", "CLEARING", "OTHER"}
            or len(self.components) != 5
        ):
            return None
        return CostItem(
            recorded_at_utc=at,
            evidence_lane="BROKER_PAPER",
            release_id=release_id,
            fill_id=fill.fill_id,
            amount=Decimal(0),
            source="CONTRACTUAL_ZERO",
            document_id=self.id,
            effective_start=self.start,
            effective_end=self.end,
        )
