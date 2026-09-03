"""What is actually running, stated by the runtime instead of reconstructed later (#190).

The #190 inventory of the coordinator answered "which commit is checked out", "is anything
running", "which database is authoritative" and "could a second host wake up as a competing
writer" by reading the filesystem, the process table, the registry, the ARP cache and the git
reflog. Those answers were unambiguous only because nothing was running. With a live process on
an older checkout, the same archaeology would not have settled any of them.

So the runtime records what it is, and a reader turns those observations into the four warnings
that matter operationally. Nothing here has authority: it reports, it does not gate. A stale
checkout is a fact an operator needs, not a reason for software to refuse -- and a
deployed-state check that could halt trading would be a new way for the system to stop itself
over a bookkeeping disagreement.

**Every field is non-secret by construction.** The database is identified by a random UUID
persisted in its singleton schema row. That durable store-lineage identity survives a move or
byte-for-byte backup without publishing a filesystem layout, and no credential, key or account
identifier is read.
"""

from __future__ import annotations

import json
import os
import platform as platform_module
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from quantbot.domain import StrategyIdentity
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.schema import SCHEMA_VERSION, runs, runtime_deployments

#: A runtime observed less recently than this is not treated as active.
#:
#: **Project default (#190).** The coordinator records one observation per cycle and the daemon
#: schedules one cycle per session, so anything inside a long weekend plus a margin is still
#: "the thing that is running". 100 hours is the value `scripts/supervisor.py::STALE_RUN_HOURS`
#: already uses for the same question about the same cadence -- CLAUDE.md's first tiebreak takes
#: an existing deployed value -- and it was chosen there because consecutive cycles are 96h apart
#: across a Monday holiday.
ACTIVE_WITHIN = timedelta(hours=100)

#: Recorded when the process cannot name its own commit. Reportable in its own right: a stale
#: checkout cannot be detected on a runtime that will not say what it is running.
UNKNOWN_COMMIT = "UNKNOWN"


class RuntimeRole(StrEnum):
    """What a process is doing, so worker availability is separable from coordinator authority.

    #190 asks for these to be reported separately, and the reason is concrete: a Kronos worker
    being up says nothing about whether anything is trading, and reading one as the other is how
    "the system is running" becomes true and useless at the same time.
    """

    COORDINATOR = "COORDINATOR"
    WORKER = "WORKER"
    CONSOLE = "CONSOLE"
    TOOL = "TOOL"


class DeploymentWarning(StrEnum):
    NO_RUNTIME_RECORDED = "NO_RUNTIME_RECORDED"
    NO_ACTIVE_COORDINATOR = "NO_ACTIVE_COORDINATOR"
    MULTIPLE_ACTIVE_COORDINATORS = "MULTIPLE_ACTIVE_COORDINATORS"
    STALE_CHECKOUT = "STALE_CHECKOUT"
    UNIDENTIFIED_CHECKOUT = "UNIDENTIFIED_CHECKOUT"
    SCHEMA_VERSION_MISMATCH = "SCHEMA_VERSION_MISMATCH"
    LIVE_TRADING_RECORDED = "LIVE_TRADING_RECORDED"


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """One process, at one moment, saying what it is."""

    observation_id: str
    host_id: str
    instance_id: str
    role: RuntimeRole
    git_commit: str
    package_version: str
    python_version: str
    host_platform: str
    trading_mode: str
    store_identity: str
    schema_version: int
    strategy_id: str | None
    strategy_version: str | None
    configuration_hash: str | None
    process_started_at: datetime
    observed_at: datetime

    @property
    def identifies_its_checkout(self) -> bool:
        return self.git_commit != UNKNOWN_COMMIT


@dataclass(frozen=True, slots=True)
class DeploymentStatus:
    """The answer to "what is actually running?", with what is wrong about it."""

    as_of: datetime
    active: tuple[RuntimeObservation, ...]
    latest_coordinator: RuntimeObservation | None
    last_successful_cycle_at: datetime | None
    warnings: tuple[DeploymentWarning, ...]
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.warnings

    @property
    def active_coordinators(self) -> tuple[RuntimeObservation, ...]:
        return tuple(item for item in self.active if item.role is RuntimeRole.COORDINATOR)

    @property
    def active_workers(self) -> tuple[RuntimeObservation, ...]:
        """Worker availability, reported separately from coordinator authority (#190)."""
        return tuple(item for item in self.active if item.role is RuntimeRole.WORKER)


def instance_id(*, pid: int | None = None, started_at: datetime | None = None) -> str:
    """Identify this process. Two coordinators on one host must be two ids, not one."""
    process = os.getpid() if pid is None else pid
    stamp = "" if started_at is None else f":{encode_utc(started_at)}"
    return f"pid-{process}{stamp}"


def describe_runtime(
    *,
    role: RuntimeRole,
    store_identity: str,
    trading_mode: str,
    now: datetime,
    process_started_at: datetime,
    identity: StrategyIdentity | None = None,
    host_id: str | None = None,
    process_id: str | None = None,
    git_commit: str | None = None,
) -> RuntimeObservation:
    """Build one observation from the process's own view of itself.

    `git_commit` comes from `QUANTBOT_GIT_COMMIT` when the caller does not supply it. Reading
    it from the environment rather than shelling out to git is deliberate: the runtime may be
    installed rather than checked out, and a `git` subprocess in the trading path is a new
    dependency on a tool that does not have to be there.
    """
    from quantbot import __version__ as package_version

    observed = now.astimezone(UTC)
    # Clamped rather than refused, and this is a deliberate choice against raising.
    #
    # `PROCESS_STARTED_AT` is real wall clock; the observation time comes from the caller's
    # clock. A system clock that steps backwards between the two -- an NTP correction, a VM
    # resume -- would otherwise make this raise, and it is called while the coordinator holds
    # the writer lock at the top of a cycle. Aborting a trading cycle over a bookkeeping
    # timestamp is a worse outcome than recording "up since no later than now", which is what
    # the clamp says. It is visible when it happens: `process_started_at == observed_at`
    # exactly is the tell.
    started = min(process_started_at.astimezone(UTC), observed)

    commit = git_commit if git_commit is not None else os.environ.get("QUANTBOT_GIT_COMMIT", "")
    commit = commit.strip() or UNKNOWN_COMMIT
    host = (host_id or platform_module.node() or "unknown-host").strip()
    process = process_id or instance_id(started_at=started)
    return RuntimeObservation(
        observation_id=f"{host}:{process}:{role.value}:{encode_utc(observed)}",
        host_id=host,
        instance_id=process,
        role=role,
        git_commit=commit,
        package_version=package_version,
        python_version=platform_module.python_version(),
        host_platform=f"{platform_module.platform()} {sys.platform}"[:160],
        trading_mode=trading_mode.strip().upper(),
        store_identity=store_identity,
        schema_version=SCHEMA_VERSION,
        strategy_id=None if identity is None else identity.strategy_id,
        strategy_version=None if identity is None else identity.version,
        configuration_hash=None if identity is None else identity.configuration_hash,
        process_started_at=started,
        observed_at=observed,
    )


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """The parts of a runtime's identity that do not change while the process lives.

    Built once by whoever wired the process -- that layer already holds the settings and the
    persisted store identity -- so the cycle stamps only the time. Passing the whole thing rather
    than four loose arguments keeps a caller from wiring up a runtime that describes the wrong
    database.
    """

    role: RuntimeRole
    store_identity: str
    trading_mode: str
    process_started_at: datetime
    host_id: str | None = None
    process_id: str | None = None
    git_commit: str | None = None

    def observe(
        self, *, now: datetime, identity: StrategyIdentity | None = None
    ) -> RuntimeObservation:
        # Derive the default id from the context's original captured start, not from the start
        # value `describe_runtime` may clamp after a backwards clock step. Otherwise one real
        # process changes identity precisely when NTP correction makes its timestamps unusual.
        stable_process_id = self.process_id or instance_id(started_at=self.process_started_at)
        return describe_runtime(
            role=self.role,
            store_identity=self.store_identity,
            trading_mode=self.trading_mode,
            now=now,
            process_started_at=self.process_started_at,
            identity=identity,
            host_id=self.host_id,
            process_id=stable_process_id,
            git_commit=self.git_commit,
        )


class DeploymentRegistry:
    """Append-only runtime observations, inside the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self, observation: RuntimeObservation, *, detail: dict[str, object] | None = None
    ) -> None:
        """Write one observation. Idempotent for one process at one recorded instant.

        Repeated recording at the same instant is a re-run, not a second runtime, and inserting
        twice would fabricate an extra process for the multiple-coordinator check to find.
        """
        existing = self._session.execute(
            select(runtime_deployments.c.observation_id).where(
                runtime_deployments.c.observation_id == observation.observation_id
            )
        ).one_or_none()
        if existing is not None:
            return
        self._session.execute(
            runtime_deployments.insert().values(
                observation_id=observation.observation_id,
                host_id=observation.host_id,
                instance_id=observation.instance_id,
                role=observation.role.value,
                git_commit=observation.git_commit,
                package_version=observation.package_version,
                python_version=observation.python_version,
                host_platform=observation.host_platform,
                trading_mode=observation.trading_mode,
                store_identity=observation.store_identity,
                schema_version=observation.schema_version,
                strategy_id=observation.strategy_id,
                strategy_version=observation.strategy_version,
                configuration_hash=observation.configuration_hash,
                process_started_at=encode_utc(observation.process_started_at),
                observed_at=encode_utc(observation.observed_at),
                detail_json=json.dumps(
                    detail or {}, ensure_ascii=True, separators=(",", ":"), sort_keys=True
                ),
            )
        )

    def observations(self, *, limit: int = 200) -> list[RuntimeObservation]:
        rows = (
            self._session.execute(
                select(runtime_deployments)
                .order_by(
                    runtime_deployments.c.observed_at.desc(),
                    runtime_deployments.c.observation_id.desc(),
                )
                .limit(limit)
            )
            .mappings()
            .all()
        )
        return [_observation_from_row(row) for row in rows]

    def status(
        self, *, now: datetime, active_within: timedelta = ACTIVE_WITHIN
    ) -> DeploymentStatus:
        """Latest observation per process, and what is wrong with the picture."""
        moment = now.astimezone(UTC)
        history = self.observations(limit=1000)
        latest_per_process: dict[tuple[str, str, RuntimeRole], RuntimeObservation] = {}
        for item in history:
            key = (item.host_id, item.instance_id, item.role)
            if key not in latest_per_process:
                latest_per_process[key] = item

        cutoff = moment - active_within
        active = tuple(
            sorted(
                (item for item in latest_per_process.values() if item.observed_at >= cutoff),
                key=lambda item: item.observed_at,
                reverse=True,
            )
        )
        coordinators = [item for item in history if item.role is RuntimeRole.COORDINATOR]
        latest_coordinator = coordinators[0] if coordinators else None

        warnings: list[DeploymentWarning] = []
        reasons: list[str] = []

        if not history:
            warnings.append(DeploymentWarning.NO_RUNTIME_RECORDED)
            reasons.append(
                "no process has ever recorded what it is running; nothing here can say what "
                "is deployed, which is the state #190 found the coordinator in"
            )

        # Freshness identifies candidates for the current state, not liveness. The warning below
        # is intentionally narrower: only recorded temporal overlap establishes concurrency.
        recent_coordinator_process_keys = {
            (item.host_id, item.instance_id)
            for item in active
            if item.role is RuntimeRole.COORDINATOR
        }
        if history and not recent_coordinator_process_keys:
            warnings.append(DeploymentWarning.NO_ACTIVE_COORDINATOR)
            last_seen = (
                "never"
                if latest_coordinator is None
                else latest_coordinator.observed_at.isoformat()
            )
            reasons.append(f"no coordinator observed within {active_within}; last seen {last_seen}")

        coordinator_history: dict[tuple[str, str], list[RuntimeObservation]] = {}
        for item in coordinators:
            process_key = (item.host_id, item.instance_id)
            coordinator_history.setdefault(process_key, []).append(item)

        recent_coordinator_processes = sorted(recent_coordinator_process_keys)
        same_host_overlap_pairs: list[
            tuple[tuple[str, str], tuple[str, str]]
        ] = []
        cross_host_overlap_pairs: list[
            tuple[tuple[str, str], tuple[str, str]]
        ] = []
        for index, first_key in enumerate(recent_coordinator_processes):
            for second_key in recent_coordinator_processes[index + 1 :]:
                if _processes_overlap(
                    coordinator_history[first_key], coordinator_history[second_key]
                ):
                    pairs = (
                        same_host_overlap_pairs
                        if first_key[0] == second_key[0]
                        else cross_host_overlap_pairs
                    )
                    pairs.append((first_key, second_key))

        if same_host_overlap_pairs or cross_host_overlap_pairs:
            warnings.append(DeploymentWarning.MULTIPLE_ACTIVE_COORDINATORS)
            overlap_evidence: list[str] = []
            if same_host_overlap_pairs:
                overlap_evidence.append(
                    "same-host recorded intervals overlap: "
                    + ", ".join(
                        _format_process_pair(first, second)
                        for first, second in same_host_overlap_pairs
                    )
                )
            if cross_host_overlap_pairs:
                overlap_evidence.append(
                    "cross-host wall-clock intervals are consistent with overlap: "
                    + ", ".join(
                        _format_process_pair(first, second)
                        for first, second in cross_host_overlap_pairs
                    )
                    + "; clock skew or NTP corrections mean those timestamps do not prove "
                    "concurrency"
                )
            reasons.append(
                "; ".join(overlap_evidence)
                + "; two writers against one account is the condition #190 Phase 4 exists "
                "to prevent"
            )

        if latest_coordinator is not None and not latest_coordinator.identifies_its_checkout:
            warnings.append(DeploymentWarning.UNIDENTIFIED_CHECKOUT)
            reasons.append(
                "the running coordinator did not record a commit, so a stale checkout cannot "
                "be detected; set QUANTBOT_GIT_COMMIT"
            )
        elif latest_coordinator is not None:
            # A reappearance after a different known commit proves a reversion without
            # pretending that commit names reveal Git ancestry. A normal upgrade (A -> B) has
            # no such reappearance, so it is not stale. Unknown commits are excluded because
            # they cannot prove that a different checkout replaced the current one.
            current_commit = latest_coordinator.git_commit
            saw_current = False
            saw_different_after_current = False
            reverted = False
            for item in reversed(coordinators):
                if not item.identifies_its_checkout:
                    continue
                if item.git_commit == current_commit:
                    if saw_current and saw_different_after_current:
                        reverted = True
                        break
                    saw_current = True
                    saw_different_after_current = False
                elif saw_current:
                    saw_different_after_current = True

            if reverted:
                warnings.append(DeploymentWarning.STALE_CHECKOUT)
                reasons.append(
                    f"the running coordinator is on {current_commit}, which reappeared after "
                    "a different known commit in coordinator history; a checkout reversion "
                    "was observed"
                )

        for item in active:
            if item.schema_version != SCHEMA_VERSION:
                warnings.append(DeploymentWarning.SCHEMA_VERSION_MISMATCH)
                reasons.append(
                    f"{item.host_id}/{item.instance_id} recorded schema {item.schema_version} "
                    f"against this build's {SCHEMA_VERSION}"
                )
                break

        live = [item for item in active if item.trading_mode == "LIVE"]
        if live:
            warnings.append(DeploymentWarning.LIVE_TRADING_RECORDED)
            reasons.append(
                "a runtime recorded LIVE trading mode. Reported, never acted on: this module "
                "has no authority to stop anything, and a deployed-state check that could halt "
                "trading would be a new way for the system to stop itself over bookkeeping"
            )

        return DeploymentStatus(
            as_of=moment,
            active=active,
            latest_coordinator=latest_coordinator,
            last_successful_cycle_at=self._last_successful_cycle(),
            warnings=tuple(dict.fromkeys(warnings)),
            reasons=tuple(reasons),
        )

    def _last_successful_cycle(self) -> datetime | None:
        """Read from `runs` rather than stored again.

        A second copy of this timestamp is a second thing that can be wrong, and the run ledger
        is the authority on whether a cycle finished.
        """
        row = self._session.execute(
            select(runs.c.finished_at)
            .where(runs.c.status == "SUCCEEDED", runs.c.finished_at.is_not(None))
            .order_by(runs.c.finished_at.desc())
            .limit(1)
        ).one_or_none()
        return None if row is None or row[0] is None else decode_utc(str(row[0]))


def _observation_from_row(row: RowMapping) -> RuntimeObservation:
    return RuntimeObservation(
        observation_id=str(row["observation_id"]),
        host_id=str(row["host_id"]),
        instance_id=str(row["instance_id"]),
        role=RuntimeRole(str(row["role"])),
        git_commit=str(row["git_commit"]),
        package_version=str(row["package_version"]),
        python_version=str(row["python_version"]),
        host_platform=str(row["host_platform"]),
        trading_mode=str(row["trading_mode"]),
        store_identity=str(row["store_identity"]),
        schema_version=int(row["schema_version"]),
        strategy_id=None if row["strategy_id"] is None else str(row["strategy_id"]),
        strategy_version=None if row["strategy_version"] is None else str(row["strategy_version"]),
        configuration_hash=(
            None if row["configuration_hash"] is None else str(row["configuration_hash"])
        ),
        process_started_at=decode_utc(str(row["process_started_at"])),
        observed_at=decode_utc(str(row["observed_at"])),
    )


def summarize(status: DeploymentStatus) -> dict[str, object]:
    """The operator-readable answer to "what is actually running?"."""
    return {
        "ok": status.ok,
        "as_of": status.as_of.isoformat(),
        "warnings": [warning.value for warning in status.warnings],
        "reasons": list(status.reasons),
        "last_successful_cycle_at": (
            None
            if status.last_successful_cycle_at is None
            else status.last_successful_cycle_at.isoformat()
        ),
        "coordinator": (
            None
            if status.latest_coordinator is None
            else {
                "host_id": status.latest_coordinator.host_id,
                "instance_id": status.latest_coordinator.instance_id,
                "git_commit": status.latest_coordinator.git_commit,
                "package_version": status.latest_coordinator.package_version,
                "python_version": status.latest_coordinator.python_version,
                "platform": status.latest_coordinator.host_platform,
                "trading_mode": status.latest_coordinator.trading_mode,
                "store_identity": status.latest_coordinator.store_identity,
                "schema_version": status.latest_coordinator.schema_version,
                "strategy_id": status.latest_coordinator.strategy_id,
                "strategy_version": status.latest_coordinator.strategy_version,
                "configuration_hash": status.latest_coordinator.configuration_hash,
                "process_started_at": status.latest_coordinator.process_started_at.isoformat(),
                "observed_at": status.latest_coordinator.observed_at.isoformat(),
                "active": bool(status.active_coordinators),
            }
        ),
        # Reported separately, because a worker being up says nothing about whether anything
        # is trading, and reading one as the other makes "the system is running" true and
        # useless at the same time (#190).
        "active_workers": [
            {
                "host_id": item.host_id,
                "instance_id": item.instance_id,
                "git_commit": item.git_commit,
                "observed_at": item.observed_at.isoformat(),
            }
            for item in status.active_workers
        ],
        "note": (
            "reported, never enforced: this record has no authority over the broker, the risk "
            "engine or the kill switch"
        ),
    }


def _processes_overlap(
    first: list[RuntimeObservation], second: list[RuntimeObservation]
) -> bool:
    """Whether two persisted wall-clock intervals overlap rather than merely touch.

    On one host the observations share a clock domain, subject to that clock being corrected.
    Across hosts this establishes only that the recorded intervals are *consistent* with overlap:
    wall clocks can be skewed or stepped independently. The caller preserves that distinction in
    its operator-facing reason and never claims OS-verified liveness.
    """
    return any(
        left.observed_at > right.process_started_at
        and right.observed_at > left.process_started_at
        for left in first
        for right in second
    )


def _format_process_pair(
    first: tuple[str, str], second: tuple[str, str]
) -> str:
    return f"{first[0]}/{first[1]} + {second[0]}/{second[1]}"


__all__ = [
    "ACTIVE_WITHIN",
    "UNKNOWN_COMMIT",
    "DeploymentRegistry",
    "DeploymentStatus",
    "RuntimeContext",
    "DeploymentWarning",
    "RuntimeObservation",
    "RuntimeRole",
    "describe_runtime",
    "instance_id",
    "summarize",
]
