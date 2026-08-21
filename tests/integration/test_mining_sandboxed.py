"""A real miner, inside the real OS-enforced sandbox (#13, #21).

`test_mining.py` proves the accounting against a fake sandbox. That proves nothing about the
isolation, and isolation is the entire reason `DiscoveryMode.DATA_ANOMALY` was refused until #21
existed. So this runs an actual mining script under the actual kernel-enforced backend.

The script here is honest about what it is: it evaluates a grid of trivial thresholds over data
copied into the workspace and proposes the ones that pass. That is a real search with a real
cardinality, which is what the accounting needs to be tested against -- not a script that prints
a number somebody chose.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from quantbot.research.discovery import DiscoveryMode
from quantbot.research.mining import MiningRefused, mine_for_anomalies
from quantbot.research.registry import DataRole, WindowConsumption
from quantbot.sandbox import SandboxPolicy, SandboxRunner
from quantbot.sandbox.wsl import WslSandbox, wsl_backend_available

pytestmark = pytest.mark.skipif(
    not wsl_backend_available(),
    reason="no WSL distribution with unprivileged namespaces; this suite must not pass without one",
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
DATASET = "fred-macro-daily"
OPEN_WINDOW = WindowConsumption(dataset=DATASET, status="UNTOUCHED", trials=0, consumers=())

#: A search over 60 thresholds that keeps the two best. Deliberately a real grid: the point of
#: the accounting is that 60 gets charged and 2 gets proposed, and a script that hardcoded the
#: numbers would test the JSON shape rather than the behaviour.
MINER = """
import json

series = [((i * 37) % 101) / 100.0 for i in range(400)]
evaluated = 0
scored = []
for lower in range(0, 60):
    evaluated += 1
    threshold = lower / 100.0
    hits = [value for value in series if value > threshold]
    if len(hits) > 5:
        scored.append((sum(hits) / len(hits), threshold))

scored.sort(reverse=True)
candidates = [
    {
        "question": f"Does a level above {threshold:.2f} precede a drawdown?",
        "overlooked": "thresholds in this range are rarely reported",
        "why_unpriced": "the aggregation is unusual",
        "expected_direction": "higher level precedes larger drawdown",
        "horizon_observations": 21,
        "universe": ["SPY"],
        "features": ["macro_level"],
        "confounders": ["level rises in volatile regimes"],
        "simplest_refutation": "regress drawdown on level; no relation refutes it",
    }
    for _, threshold in scored[:2]
]
print(json.dumps({"evaluated": evaluated, "candidates": candidates}))
"""


def _sandbox() -> WslSandbox:
    return WslSandbox(SandboxPolicy(wall_clock_seconds=90.0, memory_mb=256))


def test_a_real_miner_runs_isolated_and_is_charged_for_its_whole_search() -> None:
    """End to end: kernel-enforced isolation, and the search volume it cannot hide.

    Sixty thresholds evaluated, two proposed. The burden is sixty, on every candidate.
    """
    run = mine_for_anomalies(
        _sandbox(),
        MINER,
        dataset=DATASET,
        roles=frozenset({DataRole.DISCOVERY}),
        consumption=OPEN_WINDOW,
        family_id="macro-levels",
        proposed_by="grid-miner-v1",
        now=NOW,
    )

    assert run.evaluated == 60, "the whole grid, not the two that survived it"
    assert len(run.candidates) == 2
    assert all(item.search_cardinality == 60 for item in run.candidates)
    assert all(item.mode is DiscoveryMode.DATA_ANOMALY for item in run.candidates)
    assert run.discard_ratio == 30.0


def test_the_miner_cannot_read_the_ledger_it_is_mining_on_behalf_of() -> None:
    """The property that had to exist before this mode could be enabled at all.

    A miner that can read `quantbot.db` can read the protected windows it is supposed to be
    kept away from, and can read the results of every experiment already run. Under the Windows
    backend an absolute path resolves. Here the file is not on any visible filesystem.
    """
    peeking = """
import json
try:
    with open("/mnt/e/Documents/GitHub/project_elrond/CLAUDE.md", "rb") as handle:
        leaked = len(handle.read())
except OSError:
    leaked = None
print(json.dumps({"evaluated": 1, "candidates": [], "leaked": leaked}))
"""

    # It reports no candidates, which is refused -- but the assertion that matters is what the
    # stdout says about the read, so the refusal is caught and inspected rather than asserted on.
    with pytest.raises(MiningRefused, match="no candidates"):
        mine_for_anomalies(
            _sandbox(),
            peeking,
            dataset=DATASET,
            roles=frozenset({DataRole.DISCOVERY}),
            consumption=OPEN_WINDOW,
            family_id="macro-levels",
            proposed_by="peeking-miner",
            now=NOW,
        )

    result = _sandbox().run(peeking)
    assert '"leaked": null' in result.stdout, (
        f"the miner read the repository from inside the sandbox: {result.stdout}"
    )


def test_the_windows_backend_is_refused_for_mining_even_though_it_would_work() -> None:
    """It runs scripts perfectly well. That is exactly why the refusal has to be explicit.

    A miner under `SandboxRunner` would produce correct-looking candidates and correct-looking
    accounting, having had read access to the dotenv and the ledger the whole time. Nothing in
    the output would show it.
    """
    with pytest.raises(MiningRefused, match="OS-enforced"):
        mine_for_anomalies(
            SandboxRunner(SandboxPolicy(wall_clock_seconds=30.0)),  # type: ignore[arg-type]
            MINER,
            dataset=DATASET,
            roles=frozenset({DataRole.DISCOVERY}),
            consumption=OPEN_WINDOW,
            family_id="macro-levels",
            proposed_by="grid-miner-v1",
            now=NOW,
        )
