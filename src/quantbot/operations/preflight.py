"""Can this host actually run Elrond, and if not, exactly what is missing (#150, #82).

`quantbot config` inventories every setting with its source and why it is immutable. It does not
answer the question somebody standing up a host actually has: **is this configured enough to
work.** Today that answer is spread across forty JSON rows and a reader has to already know which
ones matter.

#150's Stage 1 is "run the authoritative application on a small cloud VM", and the difference
between that being provisioning and being porting is whether a fresh host can be told what it is
missing rather than discovered to be broken at the first cycle.

**Capabilities, not one verdict.** "Can it trade on paper" and "can it do research" are separate
questions with separate requirements, and a host can honestly answer them differently. This
machine does: it has broker credentials and no model configuration, so it can trade and cannot
research — which is exactly why every research table is empty while the paper account has fills.
A single ready/not-ready flag would have to pick one of those to be wrong about.

**Absence is reported, values never are.** Every check reports which variable names are unset. A
credential's presence is reportable and its value is not, and the endpoint is treated as a
credential for the reason recorded in the registry: it is a URL that can embed a token.

**It reads configuration; it does not probe.** Whether the broker answers, whether the model
endpoint is listening, whether the database has the right schema — those are questions about a
moment rather than about a host, and they belong to `status`, `readiness` and `migrate-database`
respectively. Mixing them in would make a preflight that fails when the network hiccups, which is
a preflight nobody trusts.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    """One thing a host can be configured to do."""

    #: Submit and reconcile paper orders. The authoritative loop.
    PAPER_TRADING = "PAPER_TRADING"
    #: Fetch and cache market data. Separate from trading because the same credentials are not
    #: guaranteed to carry both entitlements, and a host can legitimately do one.
    MARKET_DATA = "MARKET_DATA"
    #: Generate and criticise hypotheses. Needs two distinct models.
    RESEARCH = "RESEARCH"
    #: Serve the operator console. Reads the store and needs nothing else.
    CONSOLE = "CONSOLE"


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """Whether one capability is configured, and what is missing if not."""

    capability: Capability
    ready: bool
    #: Variable names that are unset. Names only -- never a value.
    missing: tuple[str, ...] = ()
    #: A configuration that is complete and still refused, which is a different fact from
    #: something being absent and would otherwise send someone hunting for a set variable.
    refusal: str | None = None
    #: What stops working while this is unconfigured, in terms an operator can act on.
    consequence: str = ""

    def explain(self) -> str:
        if self.ready:
            return f"{self.capability.value}: ready"
        if self.refusal is not None:
            return f"{self.capability.value}: refused -- {self.refusal}"
        return (
            f"{self.capability.value}: {', '.join(self.missing)} unset -- {self.consequence}"
        )


def _paper_trading(environ: Mapping[str, str]) -> CapabilityCheck:
    missing = tuple(
        name
        for name in ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET")
        if not environ.get(name, "").strip()
    )
    return CapabilityCheck(
        capability=Capability.PAPER_TRADING,
        ready=not missing,
        missing=missing,
        consequence=(
            "no cycle can reach the broker, so nothing reconciles and no forward evidence "
            "accumulates"
        ),
    )


def _market_data(environ: Mapping[str, str]) -> CapabilityCheck:
    # The same Alpaca credentials carry both entitlements today, which is why this reports the
    # same variables. It is a separate capability because that is a fact about the current
    # provider rather than about Elrond, and a second data provider would not change the broker.
    missing = tuple(
        name
        for name in ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET")
        if not environ.get(name, "").strip()
    )
    return CapabilityCheck(
        capability=Capability.MARKET_DATA,
        ready=not missing,
        missing=missing,
        consequence="no bars can be fetched, so every cycle halts on MARKET_DATA_STALE",
    )


def _research(environ: Mapping[str, str]) -> CapabilityCheck:
    from quantbot.research.composition import CRITIC, ENDPOINT, GENERATOR

    missing = tuple(
        name for name in (ENDPOINT, GENERATOR, CRITIC) if not environ.get(name, "").strip()
    )
    if missing:
        return CapabilityCheck(
            capability=Capability.RESEARCH,
            ready=False,
            missing=missing,
            consequence=(
                "no hypothesis can be implemented and no experiment can run, so every research "
                "table stays empty for a reason that is not 'nothing was found'"
            ),
        )
    generator = environ.get(GENERATOR, "").strip()
    critic = environ.get(CRITIC, "").strip()
    if generator == critic:
        return CapabilityCheck(
            capability=Capability.RESEARCH,
            ready=False,
            refusal=(
                f"{GENERATOR} and {CRITIC} name the same model; a model asked to review its own "
                "proposal tends to find it sound"
            ),
        )
    return CapabilityCheck(capability=Capability.RESEARCH, ready=True)


def _console(environ: Mapping[str, str]) -> CapabilityCheck:
    # Deliberately always ready. The console reads the store and needs no credential of its own;
    # reporting it as blocked on something would suggest a dependency it does not have, and the
    # store's existence is `migrate-database`'s question rather than this one's.
    return CapabilityCheck(capability=Capability.CONSOLE, ready=True)


def preflight(environ: Mapping[str, str] | None = None) -> tuple[CapabilityCheck, ...]:
    """Every capability, whether it is configured, and what is missing.

    Takes the environment explicitly so a caller can ask about a host other than this one --
    which is what makes it usable for checking a deployment before shipping to it, rather than
    only for explaining the machine it happens to run on.
    """
    active = os.environ if environ is None else environ
    return (
        _paper_trading(active),
        _market_data(active),
        _research(active),
        _console(active),
    )


def unready(checks: Sequence[CapabilityCheck]) -> tuple[CapabilityCheck, ...]:
    """The ones that would stop something working. Empty means the host is fully configured."""
    return tuple(check for check in checks if not check.ready)


__all__ = [
    "Capability",
    "CapabilityCheck",
    "preflight",
    "unready",
]
