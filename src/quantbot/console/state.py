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
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quantbot.config import Settings
from quantbot.console.learner import LearnerPanel
from quantbot.console.market_status import MarketStatus
from quantbot.console.overview import OverviewPanel, read_overview
from quantbot.operations.registry import ConfigSetting, config_registry
from quantbot.research.promotion import Stage
from quantbot.storage import StorageRepository
from quantbot.storage.database import decode_utc
from quantbot.storage.schema import (
    position_protection_action_evaluations,
    position_protection_actions,
    position_protection_cycles,
    position_protection_evaluations,
    session_observations,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


#: Days without a cycle before the console says so. A `PROJECT_DEFAULT` (#72).
#:
#: Four covers a long weekend plus a public holiday, so an ordinary market closure does not
#: produce a notice while a genuinely stopped system does. Chosen to notice sooner rather than
#: later: a false notice costs a glance, and a missed one costs every downstream measurement.
STALE_RUN_DAYS = 4

#: Minutes without a market-hours observation before the console calls attention to the gap. A
#: `PROJECT_DEFAULT` (#72), derived rather than picked: `OBSERVATION_INTERVAL` is 15 minutes, so
#: three missed passes is the smallest gap that cannot be one slow broker call or a single
#: restart. Anything tighter reports a healthy daemon as stopped, which teaches an operator to
#: ignore the line.
STALE_OBSERVATION_MINUTES = 45

#: Three missed 15-minute protection passes. Kept separate from observation liveness:
#: one loop can be healthy while the safety-critical loop is stopped.
STALE_PROTECTION_MINUTES = 45

#: How many heartbeats the session page lists. A `PROJECT_DEFAULT` (#72): at the 15-minute
#: interval this is about eight hours, so one full session plus its edges fits on the page. More
#: would be a scroll nobody reads; fewer would hide the shape of a session that degraded and
#: recovered, which is the pattern worth seeing.
SESSION_OBSERVATION_ROWS = 32


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
    #: Whole days since the last operational cycle. `None` when none has ever run.
    #:
    #: Derived rather than left to the reader. The console showed `last_run_at` as a timestamp
    #: and nothing else, so a system that had stopped ten days earlier rendered as a date a
    #: person had to subtract from today to notice -- which is exactly what did not happen.
    days_since_last_run: int | None
    #: When the market-hours loop last looked at anything (#143). `None` when it never has.
    #:
    #: Distinct from `last_run_at`, and the distinction is the point. A stale cycle with a fresh
    #: observation is a daemon that is up and simply between sessions. A stale cycle with no
    #: observation at all is a daemon that is not running -- which is what happened for eleven
    #: days, and what `last_run_at` alone could not tell apart.
    last_observation_at: datetime | None
    #: Whole minutes since that pass. `None` when none has ever run.
    minutes_since_last_observation: int | None
    #: What that pass found: HEALTHY, DEGRADED, HALTED, or `None` when none has run.
    last_observation_outcome: str | None
    #: The most recent independent after-entry evaluation, or None when it never ran.
    last_protection_at: datetime | None
    minutes_since_last_protection: int | None
    last_protection_outcome: str | None
    #: None when no protection cycle exists; zero is a measured count.
    protected_positions: int | None
    unprotected_positions: int | None
    exit_required_positions: int | None
    #: Durable exit actions by state for the configured account. Empty means no action exists;
    #: it never means that an EXIT_REQUIRED evaluation was handled elsewhere.
    protection_action_counts: Mapping[str, int]
    #: Number of durable actions linked to evaluations in the latest protection cycle.
    latest_protection_action_count: int
    #: Absent rather than zero when the account has never been snapshotted.
    account_equity: str | None
    open_positions: int
    legacy_protection_positions: int | None = None
    legacy_protection_covered: int | None = None
    core_protection_unverified: int = 0
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


class DiscoveryPanel(FrozenModel):
    """What the anomaly and catalyst subsystems have actually produced (#56).

    The panel exists because both subsystems have failure modes that look like success from any
    aggregate. A run of `NO_PUBLIC_CATALYST_FOUND` reads as a stream of discoveries; a run of
    `CATALYST_UNKNOWN` reads as nothing happening. Neither is legible from a count of events, so
    the outcome mix is shown rather than a total.
    """

    anomalies: int
    #: Assessed, and how the assessments came out. Keys are the three outcomes.
    catalyst_outcomes: Mapping[str, int]
    #: Anomalies with no catalyst assessment at all -- enrichment was not configured, or failed.
    unassessed: int
    #: Symbols this source could not support a negative finding about, most recent first. Named
    #: rather than counted: which instruments are unenrichable is the actionable part.
    uncovered_symbols: tuple[str, ...] = ()

    @property
    def findings(self) -> int:
        """Negative findings, which are the *interesting* outcome and the easiest to manufacture."""
        return self.catalyst_outcomes.get("NO_PUBLIC_CATALYST_FOUND", 0)


class IncidentRow(FrozenModel):
    """One contained failure, so a research gap is visible rather than silent."""

    kind: Text
    severity: Text
    message: Text
    occurred_at: Text


class JobPlacement(FrozenModel):
    """Whether this host can carry one declared kind of work, and why not when it cannot."""

    job: Text
    can_run: bool
    #: The first requirement this host fails. `None` when it can run.
    unmet: str | None = None


class RuntimePanel(FrozenModel):
    """What this machine measured about itself, and what that permits (#38, #136).

    Reads a `HostProfile` rather than a config file, for the reason `placement.py` gives: a host
    that could *declare* its capacity would be checking a claim, and the host most likely to
    carry a wrong claim is the undersized one nobody wants running heavy work.

    Measured once when the console starts, not per request. Reading the GPU spawns a process,
    and a machine does not grow memory between two page loads -- so the measurement time is
    displayed rather than implied.
    """

    host: Text
    architecture: Text
    cpu_cores: int | None
    total_memory_mb: int | None
    gpu_vram_mb: int | None
    #: MEASURED or DECLARED. Only a measured profile may receive work.
    provenance: Text
    measured_at: datetime
    model_endpoint: str | None = None
    model_endpoint_reachable: bool | None = None
    placements: tuple[JobPlacement, ...] = ()


class BudgetRow(FrozenModel):
    """What one budget has spent of one resource (#14).

    `renewable` is the field that matters. Wall seconds and tokens come back tomorrow; trials
    against a dataset window do not, and a console that displayed them in the same table without
    saying so would let a reader treat a permanent cost as a recurring one.
    """

    budget_key: Text
    resource: Text
    spent: Text
    renewable: bool
    #: `None` when no cap is configured for this pair, which means nothing is limiting it.
    cap: str | None = None
    #: Spent as a share of the cap, 0-1. `None` when uncapped.
    fraction: float | None = None

    @property
    def exhausted(self) -> bool:
        return self.fraction is not None and self.fraction >= 1.0


class ReportRow(FrozenModel):
    """One report on disk. Listed rather than rendered."""

    name: Text
    written_at: datetime
    size_bytes: int


class ReportsPanel(FrozenModel):
    """What has been written to the reports directory (#82).

    A listing, not a viewer. Rendering the markdown would need a renderer this project does not
    have, for a file the operator can open -- and the question this panel answers is not "what
    did last week's report say" but "is anything still being produced", which is the question
    that went unanswered while the system was stopped for ten days.
    """

    directory: Text
    #: Newest first.
    reports: tuple[ReportRow, ...] = ()
    #: Whole days since the newest report. `None` when there are none.
    days_since_latest: int | None = None
    #: The directory does not exist. Distinct from existing and being empty.
    missing: bool = False


class ObservationRow(FrozenModel):
    """One market-hours heartbeat, as the console shows it."""

    observed_at: datetime
    market_open: bool
    #: HEALTHY, DEGRADED or HALTED. Halted is a deliberate stop, not a fault.
    outcome: Text
    reasons: tuple[str, ...] = ()


class SessionPanel(FrozenModel):
    """What the market-hours loop has actually been doing (#143).

    Counts and a recent list, and nothing that could be mistaken for research progress. A loop
    that observed 26 times today has not discovered anything -- it has confirmed that the parts
    of the system that can go wrong between one close and the next have not.

    Deliberately reports the session state **as of the last heartbeat** rather than calling the
    broker. The console reads authoritative stored state in one transaction; a live clock call
    here would add a network dependency that can fail, and would show a market state no recorded
    observation was taken under.
    """

    observations: tuple[ObservationRow, ...] = ()
    #: Every heartbeat ever recorded, not merely the ones listed above.
    total: int = 0
    healthy: int = 0
    degraded: int = 0
    halted: int = 0

    @property
    def market_open_at_last_heartbeat(self) -> bool | None:
        """`None` when nothing has ever been observed, which is not the same as a closed market."""
        return self.observations[0].market_open if self.observations else None

    @property
    def ever_observed(self) -> bool:
        return self.total > 0


class ResearchRuntimePanel(FrozenModel):
    """Whether the research pipeline could run at all, and what is missing if not.

    Every research issue on this project reports the same symptom -- no experiment has run, no
    hypothesis registered, no candidate rejected -- and the console showed none of the reasons.
    An operator reading empty research panels could reasonably conclude the system was working
    and had found nothing, which is the most expensive possible misreading.

    The narrowest cause is mechanical: generating an implementation from a registration needs a
    model, criticising it needs a *different* model, and neither is configured. That is three
    environment variables, not a research programme, and it is worth one line on a page rather
    than an afternoon of archaeology.

    **Names only, never values.** `missing` lists which variables are unset. A configured
    endpoint or model name is not rendered, for the same reason the registry reports the account
    id as `[configured]`: a console is a surface, and what it does not hold it cannot leak.
    """

    configured: bool
    #: Which required variables are unset. Empty when configured.
    missing: tuple[str, ...] = ()
    #: Why a configuration was refused when all the variables are present -- generator and critic
    #: naming the same model is the case that actually happens.
    refusal: str | None = None


class ConsoleState(FrozenModel):
    paper_costs: dict[str, Any] | None = None
    overview: OverviewPanel = OverviewPanel()
    """Everything the read-only console renders, assembled in one pass."""

    generated_at: datetime
    learner: LearnerPanel = LearnerPanel()
    market: MarketStatus = MarketStatus()
    health: HealthPanel
    strategies: tuple[StrategyRow, ...]
    risk: RiskPanel
    market_data: MarketDataPanel
    capacity: CapacityPanel
    discovery: DiscoveryPanel
    #: The market-hours loop's own record of itself (#143).
    session: SessionPanel = SessionPanel()
    #: Whether research could run at all. `None` when the console was built without asking.
    research_runtime: ResearchRuntimePanel | None = None
    #: Every budget with recorded spend, non-renewable first.
    budgets: tuple[BudgetRow, ...] = ()
    #: `None` when the console was built without a reports directory.
    reports: ReportsPanel | None = None
    #: `None` when the console was built without a measurement, which is the test default.
    runtime: RuntimePanel | None = None
    #: Unresolved only. A resolved incident is history; this panel is about what is still true.
    incidents: tuple[IncidentRow, ...]
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
        # Before everything except the kill switch: with no model configured, no experiment
        # can run at all, so every empty research panel below is explained by this one line
        # rather than by the absence of findings. Reading "nothing discovered" as a result when
        # the pipeline was never able to start is the most expensive misreading available here.
        if self.research_runtime is not None and not self.research_runtime.configured:
            if self.research_runtime.missing:
                items.append(
                    "no model is configured ("
                    + ", ".join(self.research_runtime.missing)
                    + " unset), so no experiment can run and no research panel below can fill"
                )
            else:
                items.append(f"research is configured but refused: {self.research_runtime.refusal}")
        # Before reconciliation and capacity, because a system that is not running makes every
        # other measurement on this page a statement about the past. The threshold is a
        # `PROJECT_DEFAULT`: a paper strategy on a daily cadence should run every trading day, so
        # anything past a long weekend is worth an operator seeing. It is deliberately not tied
        # to the market calendar -- a holiday producing one spurious notice costs a glance, and
        # calendar logic here would be a second place for #74's off-by-one to live.
        if self.health.days_since_last_run is None:
            items.append(
                "no operational cycle has ever run, so nothing on this page describes a "
                "system that has done anything"
            )
        elif self.health.days_since_last_run >= STALE_RUN_DAYS:
            items.append(
                f"the last operational cycle was {self.health.days_since_last_run} days ago; "
                "no forward evidence is accumulating while it is stopped"
            )
        market = self.market.current(self.generated_at)
        if market.warning:
            items.append(market.warning)
        if (
            market.expected_open
            and market.session_open is not None
            and self.health.last_observation_at is not None
            and self.health.last_observation_at >= market.session_open
            and self.session.market_open_at_last_heartbeat is False
        ):
            items.append(
                "the market-hours loop recorded the market closed during a scheduled session"
            )
        gap = self.health.minutes_since_last_observation
        if market.loop_overdue(
            self.health.last_observation_at, self.generated_at, STALE_OBSERVATION_MINUTES
        ):
            age = (
                f"last looked {gap} minutes ago"
                if gap is not None
                else "has never recorded a check"
            )
            items.append(
                f"the market-hours loop {age}; scheduled observations are overdue"
                if market.expected_open
                else f"the market-hours loop {age}; market status is unavailable, so check runtime "
                "service status"
            )
        protection_gap = self.health.minutes_since_last_protection
        protection_positions = (
            self.health.open_positions
            if self.health.legacy_protection_positions is None
            else self.health.legacy_protection_positions
        )
        legacy_protection_relevant = (
            self.health.legacy_protection_positions is None or protection_positions > 0
        )
        if self.health.core_protection_unverified:
            items.append(
                f"{self.health.core_protection_unverified} core-owned position(s) lack current "
                "verified Guardian protection"
            )
        if protection_positions and self.health.last_protection_at is None:
            items.append(
                f"{protection_positions} open position(s) have never received a durable "
                "protection evaluation"
            )
        elif protection_positions and market.loop_overdue(
            self.health.last_protection_at, self.generated_at, STALE_PROTECTION_MINUTES
        ):
            items.append(
                f"the position-protection loop last ran {protection_gap} minutes ago while "
                f"{protection_positions} position(s) remain open"
            )
        covered_positions = (self.health.protected_positions or 0) + (
            self.health.unprotected_positions or 0
        )
        if self.health.legacy_protection_covered is not None:
            covered_positions = self.health.legacy_protection_covered
        if (
            self.health.last_protection_at is not None
            and protection_positions > covered_positions
        ):
            items.append(
                f"{protection_positions - covered_positions} open position(s) are absent "
                "from the latest protection cycle"
            )
        if legacy_protection_relevant and self.health.unprotected_positions:
            items.append(
                f"{self.health.unprotected_positions} position(s) are explicitly unprotected"
            )
        action_counts = self.health.protection_action_counts
        if action_counts.get("UNCERTAIN", 0):
            items.append(
                f"{action_counts['UNCERTAIN']} protective exit action(s) have an uncertain "
                "broker outcome and will not be resubmitted blindly"
            )
        if action_counts.get("FAILED", 0):
            items.append(
                f"{action_counts['FAILED']} protective exit action(s) failed and require "
                "operator review"
            )
        if action_counts.get("FILLED_UNVERIFIED", 0):
            items.append(
                f"{action_counts['FILLED_UNVERIFIED']} filled protective exit(s) are waiting "
                "for a broker snapshot confirming the position is flat"
            )
        if action_counts.get("PENDING", 0):
            items.append(
                f"{action_counts['PENDING']} protective exit action(s) are durably pending"
            )
        required_actions = (
            (self.health.exit_required_positions or 0) if legacy_protection_relevant else 0
        )
        missing_actions = max(
            required_actions - self.health.latest_protection_action_count,
            0,
        )
        if missing_actions == required_actions and missing_actions:
            items.append(
                f"{required_actions} protective exit(s) require action but "
                "no durable exit action exists"
            )
        elif missing_actions:
            items.append(
                f"{missing_actions} of {required_actions} required protective exit(s) have no "
                "durable exit action"
            )
        if legacy_protection_relevant and self.health.last_protection_outcome == "INCIDENT":
            items.append("the latest position-protection cycle ended in an incident")
        # Before reconciliation: a control plane this host cannot carry is not a degraded
        # system, it is one that should not be running here at all (#38).
        if self.runtime is not None:
            for placement in self.runtime.placements:
                if placement.job == "control-plane" and not placement.can_run:
                    items.append(
                        f"this host cannot meet the control-plane requirement: {placement.unmet}"
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
        if self.reports is not None and self.reports.missing:
            items.append(
                f"the reports directory {self.reports.directory} does not exist, so nothing "
                "written there could be found"
            )
        exhausted = [row for row in self.budgets if row.exhausted and not row.renewable]
        if exhausted:
            items.append(
                f"{', '.join(sorted(row.budget_key for row in exhausted))} has spent its "
                "non-renewable budget; no further confirmatory work can be admitted against it"
            )
        if self.discovery.unassessed:
            items.append(
                f"{self.discovery.unassessed} anomaly event(s) have no catalyst assessment, so "
                "nothing established whether they had a public explanation"
            )
        if self.discovery.uncovered_symbols:
            items.append(
                f"the news source does not cover {', '.join(self.discovery.uncovered_symbols)}, "
                "so no negative catalyst finding is possible for them (#83)"
            )
        if self.incidents:
            items.append(
                f"{len(self.incidents)} unresolved incident(s), including {self.incidents[0].kind}"
            )
        if not self.market_data.supports_volume_triggers:
            items.append(
                f"the {self.market_data.feed} feed reports {self.market_data.volume_coverage} "
                "volume, so volume-derived anomaly detection is unavailable on it"
            )
        return tuple(items)


def _health(
    session: Session, settings_source: Mapping[str, ConfigSetting], *, now: datetime
) -> HealthPanel:
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
    observation = session.execute(
        select(
            session_observations.c.observed_at,
            session_observations.c.outcome,
        )
        .order_by(session_observations.c.observed_at.desc())
        .limit(1)
    ).one_or_none()
    observed_at = None if observation is None else decode_utc(observation.observed_at)
    protection = (
        None
        if expected is None
        else session.execute(
            select(
                position_protection_cycles.c.observed_at,
                position_protection_cycles.c.cycle_id,
                position_protection_cycles.c.outcome,
                position_protection_cycles.c.evaluated_count,
                position_protection_cycles.c.unprotected_count,
                position_protection_cycles.c.exit_required_count,
            )
            .where(position_protection_cycles.c.account_id == expected)
            .order_by(position_protection_cycles.c.observed_at.desc())
            .limit(1)
        ).one_or_none()
    )
    protected_at = None if protection is None else decode_utc(protection.observed_at)
    legacy_positions = None
    legacy_covered = None
    core_unverified = 0
    if account is not None:
        from quantbot.paper_core.protection_bridge import core_position_protection

        noncore = []
        reserved_count = 0
        for position in account.positions:
            try:
                reserved, verified, _ = core_position_protection(
                    session, account_id=account.account.account_id, position=position, now=now
                )
            except (ValueError, TypeError, KeyError, ArithmeticError):
                reserved, verified = False, False
            if reserved:
                reserved_count += 1
                core_unverified += int(not verified)
            else:
                noncore.append(position)
        if reserved_count:
            legacy_positions = len(noncore)
            # Aggregate historical core evaluations cannot stand in for current
            # non-core exposure. Match its exact symbol and quantity instead.
            evaluated = set()
            if protection is not None and noncore:
                for row in session.execute(
                    select(position_protection_evaluations.c.symbol,
                           position_protection_evaluations.c.quantity).where(
                        position_protection_evaluations.c.cycle_id == str(protection.cycle_id)
                    )
                ):
                    try:
                        evaluated.add((str(row.symbol), Decimal(row.quantity)))
                    except (ValueError, TypeError, ArithmeticError):
                        pass  # Malformed history never supplies current exposure coverage.
            legacy_covered = sum((p.symbol, p.quantity) in evaluated for p in noncore)
    action_counts = (
        {}
        if expected is None
        else {
            str(row.status): int(row.n)
            for row in session.execute(
                select(
                    position_protection_actions.c.status,
                    func.count().label("n"),
                )
                .where(position_protection_actions.c.account_id == expected)
                .group_by(position_protection_actions.c.status)
            )
        }
    )
    latest_action_count = (
        0
        if protection is None
        else int(
            session.scalar(
                select(func.count(func.distinct(position_protection_actions.c.action_id)))
                .select_from(position_protection_actions)
                .join(
                    position_protection_action_evaluations,
                    position_protection_action_evaluations.c.action_id
                    == position_protection_actions.c.action_id,
                )
                .join(
                    position_protection_evaluations,
                    position_protection_evaluations.c.evaluation_id
                    == position_protection_action_evaluations.c.evaluation_id,
                )
                .where(position_protection_evaluations.c.cycle_id == str(protection.cycle_id))
            )
            or 0
        )
    )
    return HealthPanel(
        kill_switch_engaged=True if kill is None else bool(kill.engaged),
        kill_switch_reason=(
            "no kill-switch state is recorded in this store" if kill is None else kill.reason
        ),
        kill_switch_updated_at=None if kill is None else kill.updated_at,
        reconciliation_clean=(None if reconciliation is None else not reconciliation.diffs),
        reconciliation_diffs=0 if reconciliation is None else len(reconciliation.diffs),
        last_run_at=runs[-1].started_at if runs else None,
        days_since_last_run=(None if not runs else max((now - runs[-1].started_at).days, 0)),
        last_observation_at=observed_at,
        minutes_since_last_observation=(
            None if observed_at is None else max(int((now - observed_at).total_seconds() // 60), 0)
        ),
        last_observation_outcome=(None if observation is None else str(observation.outcome)),
        last_protection_at=protected_at,
        minutes_since_last_protection=(
            None
            if protected_at is None
            else max(int((now - protected_at).total_seconds() // 60), 0)
        ),
        last_protection_outcome=(None if protection is None else str(protection.outcome)),
        protected_positions=(
            None
            if protection is None
            else int(protection.evaluated_count) - int(protection.unprotected_count)
        ),
        unprotected_positions=(None if protection is None else int(protection.unprotected_count)),
        exit_required_positions=(
            None if protection is None else int(protection.exit_required_count)
        ),
        protection_action_counts=action_counts,
        latest_protection_action_count=latest_action_count,
        account_equity=None if account is None else str(account.account.equity),
        open_positions=0 if account is None else len(account.positions),
        legacy_protection_positions=legacy_positions,
        legacy_protection_covered=legacy_covered,
        core_protection_unverified=core_unverified,
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


def _discovery(session: Session) -> DiscoveryPanel:
    """Read what discovery produced, without interpreting it.

    `unassessed` is derived by difference rather than stored. An anomaly with no assessment and
    an anomaly assessed as `CATALYST_UNKNOWN` are different facts -- the first means nobody
    looked, the second means somebody could not -- and a panel that showed only the assessments
    would make the first invisible.
    """
    repository = StorageRepository(session)
    events = repository.list_anomaly_events()
    assessments = repository.list_catalyst_assessments()

    outcomes: dict[str, int] = {}
    for row in assessments:
        outcome = str(row["outcome"])
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    assessed = {str(row["anomaly_event_id"]) for row in assessments}
    by_event = {str(row["event_id"]): str(row["symbol"]) for row in events}
    # A symbol is reported uncovered on the evidence of its own assessments: the source said so.
    uncovered = {
        by_event.get(str(row["anomaly_event_id"]), "")
        for row in assessments
        if "STALE" in str(row["unhealthy_sources"])
    }
    return DiscoveryPanel(
        anomalies=len(events),
        catalyst_outcomes=dict(sorted(outcomes.items())),
        unassessed=sum(1 for row in events if str(row["event_id"]) not in assessed),
        uncovered_symbols=tuple(sorted(item for item in uncovered if item)),
    )


def _incidents(session: Session) -> tuple[IncidentRow, ...]:
    """Unresolved incidents, newest first.

    Contained failures are recorded precisely so a research dataset does not acquire invisible
    holes. That only works if somebody can see them, which is this.
    """
    records = StorageRepository(session).list_incidents(unresolved_only=True)
    return tuple(
        IncidentRow(
            kind=record.kind,
            severity=record.severity,
            message=record.message,
            occurred_at=str(record.occurred_at),
        )
        for record in sorted(records, key=lambda item: str(item.occurred_at), reverse=True)
    )


def _budgets(session: Session) -> tuple[BudgetRow, ...]:
    """Every budget that has actually spent something, with its cap when one exists.

    Read from recorded spend rather than from the configured caps, so a budget nobody capped
    still appears once it has cost something. The reverse -- listing caps and showing zero
    against them -- would fill the page with rows describing nothing while hiding the one
    resource that was quietly being consumed without a limit.
    """
    from sqlalchemy import func, select

    from quantbot.research.budget import RENEWABLE, Resource
    from quantbot.storage.schema import budget_spend

    rows = session.execute(
        select(
            budget_spend.c.budget_key,
            budget_spend.c.category,
            func.coalesce(func.sum(budget_spend.c.amount), "0"),
        ).group_by(budget_spend.c.budget_key, budget_spend.c.category)
    ).all()

    reported = []
    for key, category, total in rows:
        try:
            resource = Resource(str(category))
        except ValueError:
            # A category this build does not know. Reported rather than dropped: a spend nobody
            # can classify is still a spend, and hiding it would understate the total.
            reported.append(
                BudgetRow(
                    budget_key=str(key),
                    resource=str(category),
                    spent=str(total),
                    renewable=False,
                )
            )
            continue
        reported.append(
            BudgetRow(
                budget_key=str(key),
                resource=resource.value,
                spent=str(total),
                renewable=resource in RENEWABLE,
            )
        )
    # Non-renewable first: a permanent cost deserves the reader's attention before a recurring
    # one, whatever the alphabet says.
    return tuple(sorted(reported, key=lambda row: (row.renewable, row.budget_key, row.resource)))


def build_reports_panel(directory: Path, *, now: datetime) -> ReportsPanel:
    """List what has been written, newest first.

    Reads the filesystem rather than a table because that is where reports live. A missing
    directory is reported as missing rather than as empty: "nobody has written a report" and
    "the place reports go does not exist" are different faults, and only the second is a
    configuration error.
    """
    if not directory.is_dir():
        return ReportsPanel(directory=str(directory), missing=True)

    rows = []
    for path in sorted(directory.glob("*.md")):
        try:
            stat = path.stat()
        except OSError:
            # A file that vanished between the glob and the stat. Skipped rather than reported
            # as size zero, which would read as an empty report somebody wrote.
            continue
        rows.append(
            ReportRow(
                name=path.name,
                written_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                size_bytes=stat.st_size,
            )
        )
    rows.sort(key=lambda row: row.written_at, reverse=True)
    return ReportsPanel(
        directory=str(directory),
        reports=tuple(rows),
        days_since_latest=(None if not rows else max((now - rows[0].written_at).days, 0)),
    )


def build_runtime_panel(host: object, *, measured_at: datetime) -> RuntimePanel:
    """Turn a measured `HostProfile` into what the console renders.

    Takes the profile rather than measuring one, so the caller decides when the machine is read
    and the console never spawns a GPU probe while rendering a page.
    """
    from quantbot.placement import (
        CONTROL_PLANE,
        KRONOS_INFERENCE,
        SEMANTIC_ANALYSIS,
        WATCHDOG,
        unmet_requirement,
    )

    declared = (CONTROL_PLANE, WATCHDOG, KRONOS_INFERENCE, SEMANTIC_ANALYSIS)
    placements = []
    for job in declared:
        reason = unmet_requirement(job, host, now=measured_at)  # type: ignore[arg-type]
        placements.append(JobPlacement(job=job.name, can_run=reason is None, unmet=reason))
    endpoint = getattr(host, "model_endpoint", None)
    return RuntimePanel(
        host=str(getattr(host, "name", "unknown")),
        architecture=str(getattr(host, "architecture", "unknown")),
        cpu_cores=getattr(host, "cpu_cores", None),
        total_memory_mb=getattr(host, "total_memory_mb", None),
        gpu_vram_mb=getattr(host, "gpu_vram_mb", None),
        provenance=str(getattr(getattr(host, "provenance", None), "value", "DECLARED")),
        measured_at=measured_at,
        model_endpoint=None if endpoint is None else str(getattr(endpoint, "url", "")),
        model_endpoint_reachable=(
            None if endpoint is None else bool(getattr(endpoint, "reachable", False))
        ),
        placements=tuple(placements),
    )


def _session(session: Session) -> SessionPanel:
    """Read the heartbeat log the market-hours loop writes (#143).

    Counts come from the whole table and the listed rows from its tail, so a page showing 32
    observations still reports honestly how many there have been. Deriving the count from the
    listed rows would silently cap it at the page size.
    """
    counts = {
        str(row.outcome): int(row.n)
        for row in session.execute(
            select(
                session_observations.c.outcome,
                func.count().label("n"),
            ).group_by(session_observations.c.outcome)
        )
    }
    rows = session.execute(
        select(
            session_observations.c.observed_at,
            session_observations.c.market_open,
            session_observations.c.outcome,
            session_observations.c.reasons,
        )
        .order_by(session_observations.c.observed_at.desc())
        .limit(SESSION_OBSERVATION_ROWS)
    ).all()
    return SessionPanel(
        observations=tuple(
            ObservationRow(
                observed_at=decode_utc(row.observed_at),
                market_open=bool(row.market_open),
                outcome=str(row.outcome),
                reasons=tuple(part for part in str(row.reasons).split(",") if part),
            )
            for row in rows
        ),
        total=sum(counts.values()),
        healthy=int(counts.get("HEALTHY", 0)),
        degraded=int(counts.get("DEGRADED", 0)),
        halted=int(counts.get("HALTED", 0)),
    )


def _research_runtime() -> ResearchRuntimePanel:
    """Ask whether a model runtime could be built, without building one or contacting anything.

    Reads the environment rather than probing the endpoint. A page load must not open a socket to
    a model server: the answer would be about that instant rather than about configuration, and a
    slow or hanging endpoint would take the whole console down with it.

    So this reports *configured or not*, which is the question an operator actually has when
    every research panel reads zero. Whether the configured endpoint is reachable is a different
    question and belongs to whatever tries to use it.
    """
    import os

    from quantbot.research.composition import (
        CRITIC,
        ENDPOINT,
        GENERATOR,
        ResearchNotConfigured,
        model_configuration,
    )

    try:
        model_configuration()
    except ResearchNotConfigured as refusal:
        missing = tuple(
            name for name in (ENDPOINT, GENERATOR, CRITIC) if not os.environ.get(name, "").strip()
        )
        return ResearchRuntimePanel(
            configured=False,
            missing=missing,
            # Present only when nothing is missing -- that is the generator-equals-critic case,
            # and it is the one an operator would otherwise stare at with every variable set.
            refusal=None if missing else str(refusal),
        )
    return ResearchRuntimePanel(configured=True)


def read_console_state(
    session: Session,
    *,
    now: datetime,
    runtime: RuntimePanel | None = None,
    reports: ReportsPanel | None = None,
    market: MarketStatus | None = None,
) -> ConsoleState:
    """Assemble everything the console renders, from authoritative state alone.

    One pass, one transaction. A page whose panels each opened their own read could show two
    different moments beside each other and give an operator a picture that never existed.
    """
    settings = {item.key: item for item in config_registry()}
    strategies = _strategies(session)
    return ConsoleState(
        overview=read_overview(session, account_id=Settings().EXPECTED_ACCOUNT_ID, now=now),
        generated_at=now,
        market=(market or MarketStatus()).current(now),
        health=_health(session, settings, now=now),
        strategies=strategies,
        risk=_risk(session),
        market_data=_market_data(session),
        capacity=_capacity(session, strategies),
        discovery=_discovery(session),
        session=_session(session),
        research_runtime=_research_runtime(),
        budgets=_budgets(session),
        reports=reports,
        runtime=runtime,
        incidents=_incidents(session),
        settings=tuple(settings.values()),
    )


__all__ = [
    "BudgetRow",
    "ReportRow",
    "ReportsPanel",
    "CapacityPanel",
    "ConsoleState",
    "JobPlacement",
    "RuntimePanel",
    "DiscoveryPanel",
    "IncidentRow",
    "HealthPanel",
    "MarketDataPanel",
    "RiskPanel",
    "StrategyRow",
    "read_console_state",
]
