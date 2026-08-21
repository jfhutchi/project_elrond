"""Anomaly mining, which is the discovery mode that can destroy the dataset (#13, #21, #14).

`LiteratureGenerator` refuses `DiscoveryMode.DATA_ANOMALY` and says why: mining reads data, and
until #21 there was no way to run reading code that could not also read the ledger. That is now
false, so this module exists -- and it is deliberately the most constrained thing in the package,
because a miner is the component best placed to burn through the project's remaining statistical
capacity while looking productive.

Four refusals, all before any compute is spent:

1. **The sandbox must be OS-enforced.** A miner under the Windows backend is a miner that could
   read `.env` and the durable ledger given an absolute path. `SandboxRunner` reports
   `os_enforced = False` and is refused here rather than trusted to behave.

2. **Protected roles are never handed over.** A generator that saw the holdout has already spent
   it -- `Candidate` refuses such a candidate at construction, but by then the compute is gone and
   so is the window. Checking the roles first makes it cheap and makes it impossible.

3. **An exhausted window is not mined.** `REFUTED.md` records that cycles 2-10 consumed every
   out-of-sample window on 2016-2026 US equities. Mining it again would spend real compute
   producing candidates that can never be confirmatory, which is the definition of an automated
   brute-forcer: activity that cannot change what anybody believes.

4. **The search must report what it evaluated.** A miner that returns three candidates from nine
   hundred spent nine hundred trials, and `search_cardinality` is that number. A run that does not
   report one is refused rather than charged a guess, exactly as `record_worker_search` refuses:
   an invented figure in the multiple-testing correction is worse than a known gap in it.

The count is taken from the run's own report and stamped onto every candidate it produced. A
caller cannot supply it, because the caller is the party that benefits from it being small.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from quantbot.research.discovery import (
    DISCOVERY_ROLES,
    Candidate,
    ContaminatedGenerator,
    DiscoveryMode,
)
from quantbot.research.registry import DataRole, WindowConsumption
from quantbot.research.sources import EpistemicStatus, EvidenceBasis
from quantbot.sandbox.runner import SandboxResult


class MiningRefused(RuntimeError):
    """Raised when a mining run must not happen, or its report cannot be trusted."""


class IsolatedSandbox(Protocol):
    """The capability this module requires, named rather than assumed.

    `os_enforced` is a property of the backend, not a setting: the Windows runner cannot become
    OS-enforced by configuration, and a miner asking politely is not isolation.
    """

    @property
    def os_enforced(self) -> bool: ...

    def run(self, source: str) -> SandboxResult: ...


@dataclass(frozen=True, slots=True)
class MiningRun:
    """What one mining run looked at, and what it proposes.

    `evaluated` is the whole search, including everything discarded. `candidates` is the handful
    that survived. Keeping both means the burden and the output can never drift apart, which is
    the drift #23 was opened about.
    """

    candidates: tuple[Candidate, ...]
    evaluated: int
    dataset: str
    roles: frozenset[DataRole]

    @property
    def discard_ratio(self) -> float:
        """How much was thrown away per proposal. High is not a virtue; it is a cost."""
        return self.evaluated / max(len(self.candidates), 1)


def mine_for_anomalies(
    sandbox: IsolatedSandbox,
    script: str,
    *,
    dataset: str,
    roles: frozenset[DataRole],
    consumption: WindowConsumption,
    family_id: str,
    proposed_by: str,
    now: datetime,
) -> MiningRun:
    """Run a miner under OS-enforced isolation and charge it for everything it looked at.

    Raises `MiningRefused` for every reason the run should not happen or its report cannot be
    believed. It never returns a partial result: a mining run that half worked has produced
    candidates whose provenance nobody can state, and those are worse than none.
    """
    if not sandbox.os_enforced:
        raise MiningRefused(
            "anomaly mining requires an OS-enforced sandbox; under the Windows backend a miner "
            "given an absolute path can still read the dotenv and the durable ledger"
        )
    forbidden = roles - DISCOVERY_ROLES
    if forbidden:
        raise ContaminatedGenerator(
            "a miner may not be handed "
            + ", ".join(sorted(role.value for role in forbidden))
            + "; a candidate cannot be tested on the data that produced it"
        )
    if not roles:
        raise MiningRefused(
            "declare the roles this miner reads; mining inspects data by definition"
        )
    if consumption.status == "EXHAUSTED":
        raise MiningRefused(
            f"{dataset} is exhausted, so mining it would spend compute producing candidates that "
            "can never be confirmatory; new data is the only thing that changes this"
        )

    result = sandbox.run(script)
    if result.terminated_reason is not None:
        raise MiningRefused(
            f"the mining run was stopped ({result.terminated_reason}); a search that did not "
            "finish has an unknown cardinality, and an unknown cardinality cannot be charged"
        )
    if not result.ok:
        raise MiningRefused(f"the mining run failed with exit code {result.exit_code}")

    report = _report(result.stdout)
    evaluated = report.get("evaluated")
    if not isinstance(evaluated, int) or evaluated < 1:
        raise MiningRefused(
            "the mining run did not report how many candidates it evaluated; charging a guess "
            "would put an invented figure into the luck bar every later hypothesis is judged "
            "against"
        )

    raw = report.get("candidates")
    if not isinstance(raw, list) or not raw:
        raise MiningRefused("the mining run reported no candidates and no reason for none")
    if len(raw) > evaluated:
        raise MiningRefused(
            f"the mining run proposes {len(raw)} candidates from a search of {evaluated}; a "
            "search cannot return more than it examined, so the report contradicts itself"
        )

    candidates = tuple(
        _candidate(
            item,
            index=index,
            dataset=dataset,
            roles=roles,
            evaluated=evaluated,
            family_id=family_id,
            proposed_by=proposed_by,
            now=now,
        )
        for index, item in enumerate(raw)
    )
    return MiningRun(
        candidates=candidates, evaluated=evaluated, dataset=dataset, roles=frozenset(roles)
    )


def _report(stdout: str) -> dict[str, object]:
    """Read the run's own JSON report, refusing anything else.

    The miner writes one JSON object on its last line. Scanning for the last parseable line
    rather than the whole stream is deliberate: a miner is free to print progress, and requiring
    silence would push callers into swallowing output that might explain a failure.
    """
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise MiningRefused("the mining run printed no JSON report, so nothing about it is known")


def _candidate(
    item: object,
    *,
    index: int,
    dataset: str,
    roles: frozenset[DataRole],
    evaluated: int,
    family_id: str,
    proposed_by: str,
    now: datetime,
) -> Candidate:
    """Build one candidate from the report, supplying provenance the miner may not assert."""
    if not isinstance(item, dict):
        raise MiningRefused("a mining candidate must be a JSON object")
    missing = {
        "question",
        "overlooked",
        "why_unpriced",
        "expected_direction",
        "horizon_observations",
        "universe",
        "features",
        "confounders",
        "simplest_refutation",
    } - set(item)
    if missing:
        raise MiningRefused(
            f"mining candidate {index} is missing {', '.join(sorted(missing))}; a proposal that "
            "cannot say how it would be refuted is not a candidate"
        )
    return Candidate(
        candidate_id=f"MINE-{now:%Y%m%d}-{index}",
        mode=DiscoveryMode.DATA_ANOMALY,
        question=str(item["question"]),
        family_id=family_id,
        overlooked=str(item["overlooked"]),
        why_unpriced=str(item["why_unpriced"]),
        expected_direction=str(item["expected_direction"]),
        horizon_observations=int(item["horizon_observations"]),
        universe=tuple(str(name) for name in _sequence(item["universe"])),
        features=tuple(str(name) for name in _sequence(item["features"])),
        required_datasets=(dataset,),
        # Not the miner's to claim. It read what it was given; whether those inputs were
        # point-in-time available is a fact about the snapshot, decided by #17 upstream, and a
        # miner asserting it would be asserting something it has no way to check.
        point_in_time_available=False,
        inspected_roles=roles,
        # The measured whole-search count, stamped identically onto every candidate. Dividing it
        # across proposals would make a miner look cheaper the more it discarded.
        search_cardinality=evaluated,
        confounders=tuple(str(name) for name in _sequence(item["confounders"])),
        simplest_refutation=str(item["simplest_refutation"]),
        basis=_basis(),
        estimated_trials=1,
        proposed_by=proposed_by,
        proposed_at=now,
    )


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, list) or not value:
        raise MiningRefused("a mining candidate field expected a non-empty list")
    return value


def _basis() -> EvidenceBasis:
    """Data-driven with no external source, which is exactly what mining produces.

    Not the miner's to choose. A run that could name its own epistemic status would eventually
    name a flattering one, and `LITERATURE_SUPPORTED` on a pattern found by searching is the
    most flattering lie available.
    """
    return EvidenceBasis(citations=(), status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE)


__all__ = [
    "IsolatedSandbox",
    "MiningRefused",
    "MiningRun",
    "mine_for_anomalies",
]
