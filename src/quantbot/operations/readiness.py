"""Measuring whether it is safe to clear the kill switch, rather than being told (#38).

`ReadinessEvidence` has six conditions and `KillSwitchController.clear` correctly refuses while
any of them is unmet. The defect was one layer up: three of the six were read straight out of
the environment.

    broker_healthy=active.settings.BROKER_HEALTHY,
    data_healthy=active.settings.MARKET_DATA_HEALTHY,
    risk_healthy=active.settings.RISK_ENGINE_HEALTHY,

`BROKER_HEALTHY=true` in a dotenv is not evidence the broker is healthy. It is a claim by
whoever last edited the file, and the gate that is supposed to establish safety was reading its
input from a place any operator or process could set to true. That is the same shape as
`available_observations` (#19) and hand-built `ForwardObservation` (#16): a gate consulting the
thing it is meant to constrain. `AlpacaPaperBrokerAdapter.health_check()` existed the whole
time and clearance never called it.

This matters more than the other two instances because of what it guards. Clearing the kill
switch while reconciliation is stale or the broker is unreachable lets the daemon trade against
a ledger it has no reason to trust, and the paper account is the only uncontaminated forward
evidence this project will ever get.

**What each probe establishes, and what it does not.**

* `broker_healthy` -- an actual round trip to Alpaca that returns an account with order entry
  allowed. Proves the broker answered *just now*; proves nothing about the next minute.
* `reconciliation_successful` -- the newest durable reconciliation is RECONCILED, carries no
  diffs, **and is not stale**. Freshness is new: a clean reconciliation from three weeks ago was
  previously sufficient, and a ledger that matched three weeks ago is not a ledger that matches.
* `data_healthy` -- the newest stored bar is within `max_bar_age`. Generous by design, because
  weekends and holidays make anything tight produce false alarms every Monday.
* `risk_healthy` -- the sizing caps in the active configuration parse and validate. Proves the
  risk configuration is loadable and internally consistent; it does not prove the risk engine
  would reject any particular order.
* `paper_mode` and `account_verified` -- genuinely configuration questions, and the second is
  now cross-checked: the broker must report the account id the configuration expects, so a
  dotenv pointing at one account and a key pointing at another is caught.

**Unprobed is not ready.** Anything that could not be measured lands in `unprobed`, and any
entry there blocks a clear. Failing closed is the only safe direction: an unmeasured condition
reported as satisfied is exactly the defect this module exists to remove.

`MeasuredReadiness` carries a module-private token, so it cannot be constructed anywhere else.
The same device as `LedgerObservations` and `ProtectedAccess`, for the same reason: a rule that
clearance must be measured is a rule, and a type only the measuring function can produce is a
mechanism. It is load-bearing here because the next thing to call this is an HTTP endpoint, and
an endpoint that could assemble its own evidence would be a clear button with a verification
step drawn on it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from quantbot.brokers.base import BrokerHealth
from quantbot.domain import ReconciliationStatus
from quantbot.operations.kill_switch import (
    MeasuredReadiness,
    ReadinessEvidence,
    issue_measured_readiness,
)
from quantbot.storage import Database, StorageRepository

#: A reconciliation older than this is not evidence about the ledger as it stands now. One
#: trading day plus slack: the daemon reconciles each cycle, so anything beyond this means a
#: cycle has been missed, which is itself a reason not to resume.
DEFAULT_MAX_RECONCILIATION_AGE = timedelta(hours=36)

#: Deliberately loose. A Friday close is four days old by Tuesday morning after a Monday holiday,
#: and a freshness check that cries wolf every long weekend is one an operator learns to ignore.
DEFAULT_MAX_BAR_AGE = timedelta(days=5)

class HealthProbe(Protocol):
    """The part of a broker adapter this module needs. Injected so tests need no network."""

    async def health_check(self) -> BrokerHealth: ...


async def measure_readiness(
    database: Database,
    broker: HealthProbe | None,
    *,
    paper_mode: bool,
    expected_account_id: str | None,
    risk_caps_valid: bool,
    now: datetime,
    max_reconciliation_age: timedelta = DEFAULT_MAX_RECONCILIATION_AGE,
    max_bar_age: timedelta = DEFAULT_MAX_BAR_AGE,
) -> MeasuredReadiness:
    """Probe every clearance condition that can be probed, and name the ones that could not be.

    `broker` may be `None` when no adapter is configured. That produces an *unprobed* broker
    condition rather than an unhealthy one, and both block a clear -- but they are different
    facts and an operator reading the refusal deserves to know which.
    """
    probes: list[str] = []
    unprobed: list[str] = []
    detail: list[str] = []

    broker_healthy = False
    account_verified = False
    if broker is None:
        unprobed.extend(["BROKER", "ACCOUNT"])
        detail.append("no broker adapter configured, so neither health nor account was checked")
    else:
        health = await broker.health_check()
        probes.append("BROKER")
        broker_healthy = health.healthy
        if not health.healthy:
            detail.append(f"broker reported: {', '.join(health.reasons) or 'unhealthy'}")
        # Cross-checked rather than merely present. A configuration naming one account and a key
        # opening another is exactly the mix-up that "is a value set?" cannot catch.
        probes.append("ACCOUNT")
        if expected_account_id is None:
            account_verified = False
            detail.append("no expected account id is configured to check the broker against")
        elif health.account_id != expected_account_id:
            account_verified = False
            detail.append("the broker reports a different account than the configured one")
        else:
            account_verified = True

    with database.transaction() as session:
        repository = StorageRepository(session)
        latest = repository.get_latest_reconciliation()
        newest_bar = repository.latest_bar_timestamp()

    probes.append("RECONCILIATION")
    reconciled = False
    if latest is None:
        detail.append("no reconciliation has ever completed")
    elif latest.status is not ReconciliationStatus.RECONCILED:
        detail.append(f"the newest reconciliation is {latest.status.value}")
    elif latest.diffs:
        detail.append(f"the newest reconciliation carries {len(latest.diffs)} diffs")
    elif now - latest.completed_at > max_reconciliation_age:
        # New. A clean reconciliation from three weeks ago used to satisfy this, and a ledger
        # that matched three weeks ago is not a ledger that matches.
        detail.append(
            f"the newest reconciliation is {(now - latest.completed_at).days}d old, beyond the "
            f"{max_reconciliation_age} freshness window"
        )
    else:
        reconciled = True

    probes.append("MARKET_DATA")
    data_healthy = False
    if newest_bar is None:
        detail.append("no market data has been stored")
    elif now - newest_bar > max_bar_age:
        detail.append(f"the newest bar is {(now - newest_bar).days}d old")
    else:
        data_healthy = True

    probes.append("RISK")
    if not risk_caps_valid:
        detail.append("the configured sizing caps did not validate")

    evidence = ReadinessEvidence(
        paper_mode=paper_mode,
        account_verified=account_verified,
        broker_healthy=broker_healthy,
        data_healthy=data_healthy,
        risk_healthy=risk_caps_valid,
        reconciliation_successful=reconciled,
    )
    return issue_measured_readiness(
        evidence=evidence,
        measured_at=now,
        probes=probes,
        unprobed=unprobed,
        detail=detail,
    )


__all__ = [
    "DEFAULT_MAX_BAR_AGE",
    "DEFAULT_MAX_RECONCILIATION_AGE",
    "HealthProbe",
    "MeasuredReadiness",
    "measure_readiness",
]
