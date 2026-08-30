"""What the console is allowed to know, read from authoritative state (#82 slice 2).

The console is a **control surface over existing authority**, not a second authority. Every value
here comes from the same domain and storage code the CLI and runtime use; nothing is recomputed
in the view layer, because a second implementation of a rule eventually disagrees with the first
and the disagreement is invisible until it matters.

**Read-only by construction, not by discipline.** This module has no writer, and the API that
consumes it exposes no mutation. Slice 3 adds a control plane; until then the honest thing is a
surface that cannot change anything, which is also the right order to build in -- a console that
can render the system truthfully before it can alter it.

**Every number carries what kind of thing it is.** #15 established `EvidenceState` and
`Provenance` with a constructor that refuses to render a model narrative as an observation, and
those travel as API fields rather than as CSS classes chosen in a template. A polished console is
mechanically a machine for making an unproven research system look like a product, and the
labels are what stop it.

**Secrets and account identifiers never reach the browser.** The configuration registry already
redacts by type rather than by name, and the console reads it rather than the environment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.orm import Session

from quantbot.operations.registry import ConfigSetting, config_registry
from quantbot.research.promotion import Stage
from quantbot.storage import StorageRepository

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HealthPanel(FrozenModel):
    """Is Elrond healthy, and is anything dangerous true right now.

    Danger state is reported first and unconditionally. A kill switch that is engaged is the most
    important fact on the page, and a layout that made it scroll would be a design decision about
    an operational emergency.
    """

    kill_switch_engaged: bool
    kill_switch_reason: str | None
    kill_switch_updated_at: datetime | None
    #: `None` when no reconciliation has ever run, which is different from one that ran clean.
    reconciliation_clean: bool | None
    reconciliation_diffs: int
    last_run_at: datetime | None
    #: Absent rather than zero when the account has never been snapshotted.
    account_equity: str | None
    open_positions: int
    trading_mode: str
    broker_environment: str


class StrategyRow(FrozenModel):
    """One strategy's position on the promotion ladder."""

    strategy_id: Text
    stage: Text
    #: True only at the terminal stage, which grants no broker authority.
    live_review_eligible: bool
    hypothesis_id: str | None
    updated_at: datetime | None
    reason: str | None


class RiskPanel(FrozenModel):
    """The policy in force and what it refused, read from the frozen objects themselves."""

    policy_identity: Text
    policy_version: int
    authority: Text
    #: Whether this is the recorded rule or the shipped defaults with nothing recorded yet. Once
    #: a change has been applied the two differ, and a console reporting the defaults would be
    #: describing a rule the runtime is not running -- worse than reporting nothing.
    policy_source: Text
    enabled_vetoes: tuple[str, ...]
    #: Vetoes declared with no evaluator behind them. Empty is the correct current state and is
    #: asserted rather than assumed, because a declared-but-absent control reads as protection.
    unimplemented_vetoes: tuple[str, ...]
    recent_refusals: int
    refusals_by_gate: Mapping[str, int]


class MarketDataPanel(FrozenModel):
    """What the feed can actually see. Never presented as more than it is."""

    feed: Text
    #: `CONSOLIDATED`, `SINGLE_VENUE`, `NO_CONSOLIDATED_TAPE` or `UNKNOWN`. #56 measured that an
    #: IEX volume-shock trigger scores 36.6% precision against SIP, so a console that let a venue
    #: feed read as full-market volume would be repeating the defect the detector refuses.
    volume_coverage: Text
    supports_volume_triggers: bool
    adjustment: Text
    stored_symbols: int
    latest_session: date | None


class CapacityPanel(FrozenModel):
    """Whether an edge survives the capital it would be traded at (#51)."""

    #: `None` when no assessment has been persisted. That is a fact about the store, and it is
    #: rendered as unassessed rather than as a passing check.
    verdict: str | None
    strategy_id: str | None
    as_of: str | None
    binding_symbol: str | None
    binding_constraint: str | None
    #: Inputs that rested on a versioned project default rather than a measurement. Shown, so a
    #: reader cannot mistake a modelled verdict for a measured one.
    modelled_inputs: tuple[str, ...]


class ConsoleState(FrozenModel):
    """Everything the read-only console renders, assembled in one pass."""

    generated_at: datetime
    health: HealthPanel
    strategies: tuple[StrategyRow, ...]
    risk: RiskPanel
    market_data: MarketDataPanel
    capacity: CapacityPanel
    settings: tuple[ConfigSetting, ...]

    @property
    def attention(self) -> tuple[str, ...]:
        """What needs an operator, ordered by how much it matters.

        Deliberately short and deliberately not a metric wall. A research loop generates a large
        volume of uninteresting activity by design, and a panel that showed throughput without
        its denominator would read as momentum.
        """
        items: list[str] = []
        if self.health.kill_switch_engaged:
            items.append(
                f"kill switch ENGAGED: {self.health.kill_switch_reason or 'no reason recorded'}"
            )
        if self.health.reconciliation_clean is False:
            items.append(
                f"reconciliation found {self.health.reconciliation_diffs} difference(s) against "
                "the broker"
            )
        if self.health.reconciliation_clean is None:
            items.append("no reconciliation has ever run against the broker")
        if self.risk.unimplemented_vetoes:
            items.append(
                f"{', '.join(self.risk.unimplemented_vetoes)} are declared but have no evaluator"
            )
        if self.capacity.verdict is None:
            items.append(
                "no capacity assessment has been persisted, so whether an edge survives the "
                "capital it would be traded at is unmeasured (#51)"
            )
        if not self.market_data.supports_volume_triggers:
            items.append(
                f"the {self.market_data.feed} feed reports {self.market_data.volume_coverage} "
                "volume, so volume-derived anomaly detection is unavailable on it"
            )
        return tuple(items)


def _health(session: Session, settings_source: Mapping[str, ConfigSetting]) -> HealthPanel:
    from quantbot.config import Settings
    from quantbot.storage.repositories import RecordNotFoundError

    repository = StorageRepository(session)
    try:
        kill = repository.get_kill_switch_state()
    except RecordNotFoundError:
        # A store with no kill-switch singleton is a broken store, and the safe reading of a
        # missing safety control is that it is engaged. Never the reverse.
        kill = None
    reconciliation = repository.get_latest_reconciliation()
    # Read from `Settings` rather than the registry: the registry deliberately reports the
    # account id as `[configured]` so it never reaches a browser, which makes it useless as a
    # lookup key. The console still never renders it.
    expected = Settings().EXPECTED_ACCOUNT_ID
    account = repository.get_latest_account_snapshot(expected) if expected else None
    runs = repository.list_runs()
    return HealthPanel(
        kill_switch_engaged=True if kill is None else bool(kill.engaged),
        kill_switch_reason=(
            "no kill-switch state is recorded in this store" if kill is None else kill.reason
        ),
        kill_switch_updated_at=None if kill is None else kill.updated_at,
        reconciliation_clean=(
            None if reconciliation is None else not reconciliation.diffs
        ),
        reconciliation_diffs=0 if reconciliation is None else len(reconciliation.diffs),
        last_run_at=runs[-1].started_at if runs else None,
        account_equity=None if account is None else str(account.account.equity),
        open_positions=0 if account is None else len(account.positions),
        trading_mode=str(settings_source["TRADING_MODE"].effective_value or "UNKNOWN"),
        broker_environment=str(settings_source["BROKER_ENVIRONMENT"].effective_value or "UNKNOWN"),
    )


def _strategies(session: Session) -> tuple[StrategyRow, ...]:
    return tuple(
        StrategyRow(
            strategy_id=record.strategy_id,
            stage=record.stage,
            live_review_eligible=Stage(record.stage) is Stage.LIVE_REVIEW_ELIGIBLE,
            hypothesis_id=record.hypothesis_id,
            updated_at=record.updated_at,
            reason=record.reason,
        )
        for record in StorageRepository(session).list_promotions()
    )


def _risk(session: Session) -> RiskPanel:
    from quantbot.risk.policy_versions import recorded_policy_version
    from quantbot.risk.vetoes import UNIMPLEMENTED, PortfolioVetoPolicy

    # The recorded head, never a fresh construction of the defaults (#82). Read-only: this must
    # not bootstrap a version to answer a GET, so an empty store reports the defaults -- which is
    # what the runtime would be enforcing until it starts and records them.
    version = recorded_policy_version(StorageRepository(session))
    policy = version.policy() if version is not None else PortfolioVetoPolicy()
    source = "recorded" if version is not None else "shipped defaults, none recorded yet"
    refusals = StorageRepository(session).list_rejected_candidates()
    by_gate: dict[str, int] = {}
    for _, reasons in refusals:
        for reason in reasons:
            gate = str(reason["gate_id"])
            by_gate[gate] = by_gate.get(gate, 0) + 1
    return RiskPanel(
        policy_identity=policy.identity,
        policy_version=policy.version,
        authority=policy.authority.value,
        policy_source=source,
        enabled_vetoes=tuple(sorted(item.value for item in policy.enabled)),
        unimplemented_vetoes=tuple(sorted(item.value for item in UNIMPLEMENTED)),
        recent_refusals=len(refusals),
        refusals_by_gate=dict(sorted(by_gate.items())),
    )


def _market_data(session: Session) -> MarketDataPanel:
    from quantbot.anomaly.capability import supports_volume_trigger, volume_coverage
    from quantbot.runtime import market_data_settings

    feed = market_data_settings()
    bars = StorageRepository(session).list_bars()
    symbols = {bar.symbol for bar in bars}
    latest = max((bar.timestamp.date() for bar in bars), default=None)
    return MarketDataPanel(
        feed=feed.feed.value,
        volume_coverage=volume_coverage(feed.feed).value,
        supports_volume_triggers=supports_volume_trigger(feed.feed),
        adjustment=feed.adjustment.value,
        stored_symbols=len(symbols),
        latest_session=latest,
    )


def _capacity(session: Session, strategies: Sequence[StrategyRow]) -> CapacityPanel:
    repository = StorageRepository(session)
    for strategy in strategies:
        stored = repository.latest_capacity_assessment(strategy.strategy_id)
        if stored is not None:
            modelled = str(stored.get("modelled_inputs") or "")
            return CapacityPanel(
                verdict=str(stored["verdict"]),
                strategy_id=str(stored["strategy_id"]),
                as_of=str(stored["as_of"]),
                binding_symbol=(
                    None if stored.get("binding_symbol") is None else str(stored["binding_symbol"])
                ),
                binding_constraint=(
                    None
                    if stored.get("binding_constraint") is None
                    else str(stored["binding_constraint"])
                ),
                modelled_inputs=tuple(item for item in modelled.split(",") if item),
            )
    return CapacityPanel(
        verdict=None,
        strategy_id=None,
        as_of=None,
        binding_symbol=None,
        binding_constraint=None,
        modelled_inputs=(),
    )


def read_console_state(session: Session, *, now: datetime) -> ConsoleState:
    """Assemble everything the console renders, from authoritative state alone.

    One pass, one transaction. A page whose panels each opened their own read could show two
    different moments beside each other and give an operator a picture that never existed.
    """
    settings = {item.key: item for item in config_registry()}
    strategies = _strategies(session)
    return ConsoleState(
        generated_at=now,
        health=_health(session, settings),
        strategies=strategies,
        risk=_risk(session),
        market_data=_market_data(session),
        capacity=_capacity(session, strategies),
        settings=tuple(settings.values()),
    )


__all__ = [
    "CapacityPanel",
    "ConsoleState",
    "HealthPanel",
    "MarketDataPanel",
    "RiskPanel",
    "StrategyRow",
    "read_console_state",
]
