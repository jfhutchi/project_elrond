"""What this machine can carry, made visible (#82, #38, #136).

`placement.py` has always refused to degrade a heavy job onto an undersized host, and nothing
showed an operator what it had measured or what that permitted. The panel exists so the placement
decision is inspectable before it refuses something, not only afterwards.

The property these tests protect hardest is the one `placement.py` was built around: capacity is
*measured*, and a profile that was merely declared may not receive work. A page that rendered a
declared profile as though it were measured would undo that in the one place an operator looks.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.state import build_runtime_panel, read_console_state
from quantbot.placement import HostProfile, HostProvenance, ModelEndpointStatus
from quantbot.storage import Database

MEASURED_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def profile(**overrides: object) -> HostProfile:
    """A *measured* profile. `HostProfile.measure` reads the real machine, so the token that
    marks a profile measurable is set directly here rather than by probing CI's hardware.
    """
    base = HostProfile.measure("test-host", now=MEASURED_AT)
    return base.__class__(
        name=str(overrides.get("name", "test-host")),
        architecture=str(overrides.get("architecture", "x86_64")),
        cpu_cores=overrides.get("cpu_cores", 8),  # type: ignore[arg-type]
        total_memory_mb=overrides.get("total_memory_mb", 16384),  # type: ignore[arg-type]
        gpu_vram_mb=overrides.get("gpu_vram_mb", 8192),  # type: ignore[arg-type]
        model_endpoint=overrides.get("model_endpoint"),  # type: ignore[arg-type]
        provenance=overrides.get("provenance", HostProvenance.MEASURED),  # type: ignore[arg-type]
        _token=base._token,  # noqa: SLF001
    )


def panel(**overrides: object):
    return build_runtime_panel(profile(**overrides), measured_at=MEASURED_AT)


@pytest.fixture
def client(tmp_path: Path):
    with Database.create(tmp_path / "runtime.db") as database, ExitStack() as clients:

        def build(runtime=None):
            return clients.enter_context(TestClient(create_app(database, runtime=runtime)))

        yield build


# --- what the panel reports ---------------------------------------------------------------------


def test_a_capable_host_can_run_every_declared_job() -> None:
    """8 cores, 16GB, a GPU. Nothing should be refused for capacity on a machine like this."""
    reported = panel()

    assert {item.job for item in reported.placements} == {
        "control-plane",
        "watchdog",
        "kronos-inference",
        "semantic-analysis",
    }
    # Semantic analysis needs a local model endpoint, which this host has not got.
    blocked = {item.job for item in reported.placements if not item.can_run}
    assert blocked == {"semantic-analysis"}


def test_an_undersized_host_is_refused_and_told_why() -> None:
    """#38's actual case: a Pi Zero W reports about 434MB once the VideoCore split is taken.

    The control plane floor excludes it deliberately, and the panel has to say which requirement
    failed rather than only that something did -- an operator cannot act on "no".
    """
    reported = panel(cpu_cores=1, total_memory_mb=434, gpu_vram_mb=None, architecture="armv6l")

    refused = {item.job: item.unmet for item in reported.placements if not item.can_run}
    assert "control-plane" in refused
    # The message names both figures, which is what an operator needs: the requirement and what
    # this machine actually has. "insufficient memory" would say nothing actionable.
    assert refused["control-plane"] == "needs 512MB, host reports 434"
    # The watchdog is what such a host keeps, and it must still be permitted.
    assert next(i for i in reported.placements if i.job == "watchdog").can_run


def test_an_unreadable_capacity_blocks_rather_than_passes() -> None:
    """Refusing because nobody could measure the memory is recoverable. The reverse is not."""
    reported = panel(total_memory_mb=None)

    refused = [item for item in reported.placements if not item.can_run]
    assert any("control-plane" == item.job for item in refused)


def test_a_configured_endpoint_that_did_not_answer_is_not_a_capability() -> None:
    """A URL in a config file is a claim. `ModelEndpointStatus` records whether it answered."""
    dead = ModelEndpointStatus(
        url="http://localhost:11434/v1", reachable=False, detail="connection refused"
    )
    reported = panel(model_endpoint=dead)

    assert reported.model_endpoint_reachable is False
    assert not next(i for i in reported.placements if i.job == "semantic-analysis").can_run


# --- what reaches the operator ------------------------------------------------------------------


def test_a_host_that_cannot_carry_the_control_plane_reaches_attention(tmp_path: Path) -> None:
    """Not a degraded system -- one that should not be running here at all (#38).

    Ranked above reconciliation, because every other measurement on the page is a statement about
    a machine that should not be making them.
    """
    database = Database.create(tmp_path / "small.db")
    try:
        with database.transaction() as session:
            state = read_console_state(
                session,
                now=MEASURED_AT,
                runtime=panel(cpu_cores=1, total_memory_mb=434),
            )
    finally:
        database.close()

    assert any("cannot meet the control-plane requirement" in item for item in state.attention)


def test_a_capable_host_raises_nothing(tmp_path: Path) -> None:
    """A notice that fires on a healthy machine is one an operator learns to scroll past."""
    database = Database.create(tmp_path / "big.db")
    try:
        with database.transaction() as session:
            state = read_console_state(session, now=MEASURED_AT, runtime=panel())
    finally:
        database.close()

    assert not [item for item in state.attention if "control-plane" in item]


# --- the page -------------------------------------------------------------------------------------


def test_the_page_says_when_the_reading_was_taken(client) -> None:
    """Measured once at start-up, so the age of the reading has to be visible.

    Reading the GPU spawns a process and a machine does not grow memory between two page loads,
    so measuring per request would be waste -- but a stale number presented as live is worse than
    waste, hence the timestamp.
    """
    body = client(panel()).get("/runtime").text

    assert "2026-08-30 12:00" in body
    assert "when this console started" in body


def test_the_page_admits_there_is_only_one_backend(client) -> None:
    """Every worker runs as a local subprocess and nothing chooses between backends.

    A runtime page that listed capabilities without saying where work actually runs would imply
    a dispatcher that does not exist. #136 tracks it; the page states it.
    """
    body = client(panel()).get("/runtime").text

    assert "local subprocess" in body
    assert "#136" in body


def test_an_unmeasured_host_renders_as_absent_rather_than_capable(client) -> None:
    """The console starts without the panel rather than refusing to start.

    One panel is not worth an operator's whole view of the system -- but the absence has to read
    as absence, not as a machine that can run everything.
    """
    body = client(None).get("/runtime").text

    assert "was not measured" in body
    assert "control-plane" not in body


def test_a_declared_profile_is_not_rendered_as_a_measured_one(client) -> None:
    """The property this file's docstring claims to protect hardest, and did not.

    `placement.py`'s whole argument is that capacity is measured rather than declared: a host
    that could announce its own size would be checked against a claim, and the host most likely
    to carry a wrong claim is the undersized one. A page that showed `DECLARED` as though it were
    `MEASURED` would undo that in the one place an operator looks.

    Caught by mutation: hard-coding the panel's provenance to "MEASURED" left every test green.
    """
    declared = HostProfile(
        name="claims-to-be-big",
        architecture="x86_64",
        cpu_cores=64,
        total_memory_mb=262144,
        gpu_vram_mb=81920,
    )
    reported = build_runtime_panel(declared, measured_at=MEASURED_AT)

    assert reported.provenance == "DECLARED"
    assert not declared.is_measured, "the fixture is genuinely unmeasured"

    body = client(reported).get("/runtime").text
    assert "DECLARED" in body
    assert "may not receive work" in body


def test_a_measured_profile_says_so(client) -> None:
    """The other half. A check that only ever fires one way is half a check."""
    reported = panel()

    assert reported.provenance == "MEASURED"

    body = client(reported).get("/runtime").text
    assert "MEASURED" in body
    assert "may not receive work" not in body
