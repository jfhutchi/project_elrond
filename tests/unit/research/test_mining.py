"""The discovery mode that can destroy the dataset, and the refusals that stop it (#13).

A miner is the component best placed to burn the project's remaining statistical capacity while
looking productive: it generates volume, volume looks like progress, and every candidate it
discards still raises the luck bar for everything registered afterwards.

So these tests are mostly about what it refuses. The sandbox here is a fake with an honest
`os_enforced` flag; the real backend is exercised end to end in
`tests/integration/test_mining_sandboxed.py`, because a miner tested only against a fake sandbox
would prove the accounting and nothing about the isolation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from quantbot.research.discovery import ContaminatedGenerator, DiscoveryMode
from quantbot.research.mining import MiningRefused, mine_for_anomalies
from quantbot.research.registry import DataRole, WindowConsumption
from quantbot.research.sources import EpistemicStatus
from quantbot.sandbox.runner import SandboxResult

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
DATASET = "fred-macro-daily"
OPEN_WINDOW = WindowConsumption(dataset=DATASET, status="UNTOUCHED", trials=0, consumers=())
SPENT_WINDOW = WindowConsumption(
    dataset=DATASET, status="EXHAUSTED", trials=40, consumers=("H-old",)
)


def candidate_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "question": "Does revision dispersion precede index drawdowns?",
        "overlooked": "studied per-name, rarely aggregated",
        "why_unpriced": "aggregation needs data most index traders lack",
        "expected_direction": "higher dispersion precedes larger drawdowns",
        "horizon_observations": 21,
        "universe": ["SPY"],
        "features": ["revision_dispersion"],
        "confounders": ["dispersion rises in volatile regimes"],
        "simplest_refutation": "regress drawdown on dispersion; no relation refutes it",
    }
    payload.update(overrides)
    return payload


class FakeSandbox:
    """A sandbox that reports its isolation honestly and returns a scripted result."""

    def __init__(self, stdout: str, *, os_enforced: bool = True, **result: object) -> None:
        self.os_enforced = os_enforced
        self.ran: list[str] = []
        self._result = SandboxResult(
            ok=result.get("ok", True),  # type: ignore[arg-type]
            exit_code=result.get("exit_code", 0),  # type: ignore[arg-type]
            stdout=stdout,
            stderr="",
            duration_seconds=1.0,
            terminated_reason=result.get("terminated_reason"),  # type: ignore[arg-type]
        )

    def run(self, source: str) -> SandboxResult:
        self.ran.append(source)
        return self._result


def report(evaluated: int, candidates: list[dict[str, object]]) -> str:
    return "searching...\n" + json.dumps({"evaluated": evaluated, "candidates": candidates})


def mine(sandbox, **overrides):
    kwargs: dict[str, object] = {
        "dataset": DATASET,
        "roles": frozenset({DataRole.DISCOVERY}),
        "consumption": OPEN_WINDOW,
        "family_id": "revisions",
        "proposed_by": "miner-v1",
        "now": NOW,
    }
    kwargs.update(overrides)
    return mine_for_anomalies(sandbox, "print('mining')", **kwargs)  # type: ignore[arg-type]


def test_the_whole_search_is_charged_not_the_candidates_returned() -> None:
    """#13 and #23: a miner that evaluates 900 and proposes 3 spent 900 trials.

    The count is stamped identically onto every candidate rather than divided across them.
    Dividing would make a miner look *cheaper* the more it discarded, which inverts the
    incentive the multiple-testing budget exists to create.
    """
    sandbox = FakeSandbox(report(900, [candidate_payload(), candidate_payload()]))

    run = mine(sandbox)

    assert run.evaluated == 900
    assert len(run.candidates) == 2
    assert all(item.search_cardinality == 900 for item in run.candidates)
    assert run.discard_ratio == 450.0


def test_a_run_that_will_not_say_what_it_evaluated_is_refused_not_guessed() -> None:
    """An invented figure in the luck bar is worse than a known gap in it.

    Same rule as `record_worker_search`: a search whose size nobody can state cannot be charged,
    and a miner that will not state one does not get its candidates counted as if free.
    """
    sandbox = FakeSandbox(json.dumps({"candidates": [candidate_payload()]}))

    with pytest.raises(MiningRefused, match="did not report how many"):
        mine(sandbox)


def test_a_report_that_returns_more_than_it_examined_is_refused() -> None:
    """The report contradicting itself is the cheapest possible sign it cannot be trusted."""
    sandbox = FakeSandbox(report(1, [candidate_payload(), candidate_payload()]))

    with pytest.raises(MiningRefused, match="cannot return more than it examined"):
        mine(sandbox)


def test_a_miner_is_refused_a_sandbox_that_only_conceals() -> None:
    """The reason this module could not be written before #21.

    Under the Windows backend a miner handed an absolute path still reads the dotenv and the
    durable ledger. The flag is a property of the backend, so this cannot be configured away.
    """
    sandbox = FakeSandbox(report(10, [candidate_payload()]), os_enforced=False)

    with pytest.raises(MiningRefused, match="OS-enforced sandbox"):
        mine(sandbox)

    assert sandbox.ran == [], "nothing should have been executed"


def test_a_miner_is_never_handed_protected_evaluation_data() -> None:
    """A generator that saw the holdout has already spent it.

    `Candidate` refuses such a candidate at construction, but by then the compute is gone and so
    is the window. Refusing before the run makes it cheap as well as impossible.
    """
    sandbox = FakeSandbox(report(10, [candidate_payload()]))

    with pytest.raises(ContaminatedGenerator, match="PROTECTED_EVALUATION"):
        mine(sandbox, roles=frozenset({DataRole.PROTECTED_EVALUATION}))

    assert sandbox.ran == []


def test_an_exhausted_window_is_not_mined_at_all() -> None:
    """Activity that cannot change what anybody believes is the definition of brute-forcing.

    `REFUTED.md` records that cycles 2-10 consumed every out-of-sample window on 2016-2026 US
    equities. Mining it again would spend real compute producing candidates that can never be
    confirmatory, and would raise the luck bar for the ones that could.
    """
    sandbox = FakeSandbox(report(900, [candidate_payload()]))

    with pytest.raises(MiningRefused, match="exhausted"):
        mine(sandbox, consumption=SPENT_WINDOW)

    assert sandbox.ran == [], "not one second of compute on a window that cannot answer"


def test_a_stopped_run_has_an_unknown_cardinality_and_is_refused() -> None:
    """A search that did not finish cannot be charged, and must not be read as a small one."""
    sandbox = FakeSandbox(
        report(900, [candidate_payload()]), ok=False, terminated_reason="wall-clock"
    )

    with pytest.raises(MiningRefused, match="unknown cardinality"):
        mine(sandbox)


def test_a_miner_cannot_claim_point_in_time_availability_or_a_flattering_basis() -> None:
    """Two claims the miner has no way to check, so it is not allowed to make either.

    Point-in-time availability is a fact about the snapshot, decided upstream by #17. And a
    pattern found by searching is `DATA_DRIVEN_NO_EXTERNAL_SOURCE` -- `LITERATURE_SUPPORTED` on
    a mined result is the most flattering lie available and it is unreachable from here.
    """
    sandbox = FakeSandbox(
        report(
            50,
            [candidate_payload(point_in_time_available=True, basis="LITERATURE_SUPPORTED")],
        )
    )

    run = mine(sandbox)

    assert run.candidates[0].point_in_time_available is False
    assert run.candidates[0].basis.status is EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE
    assert run.candidates[0].mode is DiscoveryMode.DATA_ANOMALY


def test_a_candidate_that_cannot_say_how_it_would_be_refuted_is_not_a_candidate() -> None:
    payload = candidate_payload()
    del payload["simplest_refutation"]
    sandbox = FakeSandbox(report(50, [payload]))

    with pytest.raises(MiningRefused, match="simplest_refutation"):
        mine(sandbox)


def test_a_run_that_printed_no_report_tells_us_nothing() -> None:
    sandbox = FakeSandbox("mining complete, found some interesting things")

    with pytest.raises(MiningRefused, match="no JSON report"):
        mine(sandbox)
