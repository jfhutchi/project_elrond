"""Jobs reach hosts that can run them, or reach nothing (#38).

The failure being prevented is named in the issue: *no silent fallback from GPU/large-memory
workflows to the Pi Zero W*. A 512MB single-core host will accept a Kronos job and then thrash
onto a microSD, taking down the always-on control plane -- which is where the broker, the risk
engine and reconciliation live. A research task that was never urgent must not be able to do
that.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from quantbot.placement import (
    CONTROL_PLANE,
    KRONOS_INFERENCE,
    SEMANTIC_ANALYSIS,
    WATCHDOG,
    HostProfile,
    HostProvenance,
    JobRequirements,
    NoCapableHost,
    place,
    unmet_requirement,
)

#: The real hardware, from `HostProfile.measure()` on each machine. The Pi's figures are the
#: Zero W's published specification; it is the one host here that cannot run the measurement.
OMEN = {"architecture": "amd64", "cpu_cores": 32, "total_memory_mb": 32486, "gpu_vram_mb": 16376}
WSL = {"architecture": "x86_64", "cpu_cores": 32, "total_memory_mb": 15847, "gpu_vram_mb": 16376}
NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)

PI_ZERO = {"architecture": "armv6l", "cpu_cores": 1, "total_memory_mb": 434, "gpu_vram_mb": 0}


def measured(name: str, specs: dict[str, object], **overrides: object) -> HostProfile:
    """A profile carrying measured provenance, built the way `measure()` builds one.

    Reaches for the module-private token deliberately: the production route reads the machine
    under the test, which would make every assertion a description of the CI runner rather than
    of the rule.
    """
    from quantbot import placement  # noqa: PLC0415

    fields: dict[str, object] = {"name": name, **specs, **overrides}
    return HostProfile(
        provenance=HostProvenance.MEASURED,
        _token=placement._MEASURED,
        **fields,  # type: ignore[arg-type]
    )


def test_kronos_never_lands_on_the_pi_even_when_it_is_the_only_host() -> None:
    """The whole reason this module exists.

    With one host and a job it cannot run, the tempting behaviour is to run it anyway. That is
    how a research task takes down the trading control plane.
    """
    with pytest.raises(NoCapableHost) as raised:
        place(KRONOS_INFERENCE, [measured("pi-zero-w", PI_ZERO)])

    assert "architecture armv6l" in raised.value.unmet["pi-zero-w"]

    # And the memory ceiling holds independently, on a board the architecture rule admits. Two
    # gates that both happen to catch the Pi would leave either one free to rot unnoticed.
    small_arm64 = measured("small-board", PI_ZERO, architecture="aarch64", cpu_cores=4)
    assert "2048MB" in (unmet_requirement(KRONOS_INFERENCE, small_arm64) or "")


def test_a_capable_host_present_alongside_the_pi_gets_the_job() -> None:
    """Refusing is only correct when nothing can run it."""
    chosen = place(KRONOS_INFERENCE, [measured("pi-zero-w", PI_ZERO), measured("omen-wsl2", WSL)])

    assert chosen.name == "omen-wsl2"


def test_the_pi_keeps_the_watchdog_role_and_loses_the_control_plane() -> None:
    """This issue's conclusion, encoded as a rule rather than as prose.

    The Pi is not banned by name -- a rule naming a host would be wrong the moment the hardware
    changed. It is sized out: a nominally 512MB Zero W reports about 434MB once the VideoCore
    split is taken, so the control plane does not fit and the watchdog does.
    """
    pi = measured("pi-zero-w", PI_ZERO)

    assert unmet_requirement(WATCHDOG, pi) is None
    assert unmet_requirement(CONTROL_PLANE, pi) == "needs 512MB, host reports 434"


def test_a_host_cannot_talk_its_way_into_a_job() -> None:
    """A declared profile is a claim, and a gate that reads a claim checks nothing.

    This is the defect class that keeps recurring here: the gate reading its input from the
    thing it constrains. An undersized host with a copied config is exactly the case.
    """
    generous = HostProfile(
        name="pi-zero-w",
        architecture="x86_64",
        cpu_cores=32,
        total_memory_mb=65536,
        gpu_vram_mb=24576,
    )

    assert generous.is_measured is False
    with pytest.raises(NoCapableHost, match="not measured"):
        place(KRONOS_INFERENCE, [generous])


def test_a_profile_claiming_measured_provenance_without_the_token_is_refused() -> None:
    """The enum value is not the evidence; the token is.

    Otherwise the gate is one keyword argument away from being bypassed by the caller it exists
    to constrain, which is not a gate.
    """
    forged = HostProfile(
        name="pi-zero-w",
        architecture="x86_64",
        cpu_cores=32,
        total_memory_mb=65536,
        gpu_vram_mb=24576,
        provenance=HostProvenance.MEASURED,
    )

    assert forged.is_measured is False
    with pytest.raises(NoCapableHost):
        place(KRONOS_INFERENCE, [forged])


@pytest.mark.parametrize("field", ["cpu_cores", "total_memory_mb"])
def test_a_capacity_nobody_could_read_blocks_rather_than_passes(field: str) -> None:
    """Unknown is not sufficient.

    A job refused because the RAM could not be measured is recoverable. One placed because the
    reading failed open is how the control plane goes down.
    """
    unreadable = measured("mystery-host", WSL, **{field: None})

    assert unmet_requirement(KRONOS_INFERENCE, unreadable) is not None


def test_an_unreadable_gpu_is_not_treated_as_a_working_one() -> None:
    """`None` and `0` are different claims, and neither is "adequate"."""
    training = JobRequirements(name="fine-tune", minimum_gpu_vram_mb=8192)

    assert unmet_requirement(training, measured("unprobed", WSL, gpu_vram_mb=None)) is not None
    assert unmet_requirement(training, measured("no-gpu", WSL, gpu_vram_mb=0)) is not None
    assert unmet_requirement(training, measured("omen", OMEN)) is None


def test_semantic_work_needs_a_reachable_endpoint_not_a_configured_one() -> None:
    """Capacity is not capability, and a URL is not an endpoint.

    This was `bool(model_endpoint)`: any non-empty string counted as a measured capability, so a
    dead or misconfigured runtime read as available -- inside the module whose whole argument is
    that capacity is measured rather than declared. Sol caught it in review.
    """
    from quantbot.placement import ModelEndpointStatus  # noqa: PLC0415

    live = ModelEndpointStatus(
        url="http://localhost:11434/v1", reachable=True, verified_at=NOW, detail="HTTP 200"
    )
    dead = ModelEndpointStatus(url="http://localhost:11434/v1", reachable=False, detail="URLError")

    none_configured = measured("omen-wsl2", WSL)
    unreachable = measured("omen-wsl2", WSL, model_endpoint=dead)
    reachable = measured("omen-wsl2", WSL, model_endpoint=live)

    assert "none configured" in (
        unmet_requirement(SEMANTIC_ANALYSIS, none_configured, now=NOW) or ""
    )
    assert "did not answer" in (unmet_requirement(SEMANTIC_ANALYSIS, unreachable, now=NOW) or "")
    assert unmet_requirement(SEMANTIC_ANALYSIS, reachable, now=NOW) is None


def test_a_stale_endpoint_check_blocks_rather_than_carrying_forward() -> None:
    """A profile is measured once and can be held for the life of a worker.

    So a verification from an hour ago is a fact about an hour ago. Stale blocks for the same
    reason unknown blocks: the alternative is dispatching work to a runtime that answered once.
    """
    from datetime import timedelta  # noqa: PLC0415

    from quantbot.placement import ENDPOINT_FRESHNESS, ModelEndpointStatus  # noqa: PLC0415

    verified = ModelEndpointStatus(
        url="http://localhost:11434/v1", reachable=True, verified_at=NOW, detail="HTTP 200"
    )
    host = measured("omen-wsl2", WSL, model_endpoint=verified)

    assert unmet_requirement(SEMANTIC_ANALYSIS, host, now=NOW) is None
    later = NOW + ENDPOINT_FRESHNESS + timedelta(seconds=1)
    assert "stale" in (unmet_requirement(SEMANTIC_ANALYSIS, host, now=later) or "")


def test_a_probe_of_an_unreachable_endpoint_records_the_failure_rather_than_raising() -> None:
    """A host without a model endpoint is a normal host, not an error.

    The refusal belongs at placement, where it names the job that cannot run, rather than as an
    exception from measuring a machine.
    """
    from quantbot.placement import probe_model_endpoint  # noqa: PLC0415

    # Port 1 is reserved and never listening; no network is reached beyond loopback.
    status = probe_model_endpoint("http://127.0.0.1:1/v1", now=NOW, timeout_seconds=2.0)

    assert status.reachable is False
    assert status.verified_at is None
    assert status.detail


def test_the_smallest_sufficient_host_wins() -> None:
    """A large machine should stay free for work that needs it.

    Otherwise the first job to ask takes the GPU box and everything after it queues behind a
    task that would have run anywhere.
    """
    chosen = place(
        WATCHDOG,
        [measured("omen-windows", OMEN), measured("pi-zero-w", PI_ZERO)],
    )

    assert chosen.name == "pi-zero-w"


def test_the_refusal_says_why_each_host_was_rejected() -> None:
    """An operator debugging a stuck queue needs the reasons, not the count."""
    with pytest.raises(NoCapableHost) as raised:
        place(
            JobRequirements(
                name="fine-tune",
                architectures=frozenset({"x86_64"}),
                minimum_memory_mb=64000,
                minimum_gpu_vram_mb=24576,
            ),
            [measured("pi-zero-w", PI_ZERO), measured("omen-wsl2", WSL)],
        )

    assert "architecture armv6l" in raised.value.unmet["pi-zero-w"]
    assert "64000MB" in raised.value.unmet["omen-wsl2"]


def test_measuring_this_machine_produces_a_usable_profile() -> None:
    """The production route has to work, not only the constructor the tests use.

    Values are not asserted -- they belong to whatever runs this -- but a profile that cannot
    satisfy the control plane on any machine capable of running the test suite would mean the
    measurement is broken rather than the host inadequate.
    """
    profile = HostProfile.measure("host-under-test")

    assert profile.is_measured is True
    assert profile.architecture
    assert unmet_requirement(CONTROL_PLANE, profile) is None
