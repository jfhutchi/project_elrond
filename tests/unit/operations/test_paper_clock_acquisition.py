"""Full async acquisition with synthetic providers; no sockets or Windows queries."""

import asyncio
import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.domain import MarketClock
from quantbot.operations import paper_clock

NOW = datetime(2026, 10, 13, 17, 20, tzinfo=UTC)


class Health:
    async def read(self):
        return paper_clock.SynchronizationHealth(
            NOW, NOW - timedelta(seconds=60), 0, 5, 0, 1024, "time.windows.com,0x9"
        )


class Broker:
    environment = "PAPER"

    def __init__(self):
        self.calls = 0

    async def get_market_clock(self):
        self.calls += 1
        return MarketClock(
            timestamp=NOW + timedelta(seconds=0.1 * self.calls + 0.175),
            is_open=True,
            next_open=NOW + timedelta(days=1),
            next_close=NOW.replace(hour=20),
        )


@pytest.mark.asyncio
async def test_async_probe_collects_two_advancing_raw_samples():
    broker = Broker()
    wall = iter(NOW + timedelta(seconds=v) for v in (0, 0.1, 0.1, 0.2, 0.2))
    mono = iter((0.0, 0.0, 0.1, 0.1, 0.2, 0.2))
    result = await paper_clock.PaperClockProbe(
        broker,
        clock=lambda: next(wall),
        monotonic=lambda: next(mono),
        synchronization=Health(),
    ).verify()
    assert result.reasons == () and result.evidence is not None
    assert broker.calls == 2
    assert result.evidence.market.timestamp == NOW + timedelta(seconds=0.375)


@pytest.mark.asyncio
async def test_async_probe_times_out_health_before_broker_io(monkeypatch):
    class StalledHealth:
        async def read(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(paper_clock, "ACQUISITION_BUDGET_SECONDS", 0.01)
    broker = Broker()
    result = await paper_clock.PaperClockProbe(broker, synchronization=StalledHealth()).verify()
    assert result.reasons == ("PAPER_CLOCK_ACQUISITION_TIMEOUT",)
    assert broker.calls == 0


@pytest.mark.asyncio
async def test_async_probe_provider_exception_is_unverified():
    class FailedBroker(Broker):
        async def get_market_clock(self):
            raise OSError("synthetic provider unavailable")

    result = await paper_clock.PaperClockProbe(FailedBroker(), synchronization=Health()).verify()
    assert result.evidence is None and result.reasons == ("PAPER_CLOCK_PROVIDER_UNAVAILABLE",)


@pytest.mark.asyncio
async def test_async_probe_cancellation_is_not_converted_to_healthy():
    entered = asyncio.Event()

    class StalledHealth:
        async def read(self):
            entered.set()
            await asyncio.Event().wait()

    broker = Broker()
    task = asyncio.create_task(
        paper_clock.PaperClockProbe(broker, synchronization=StalledHealth()).verify()
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.calls == 0


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows synchronization reader")
async def test_windows_reader_thread_denial_is_unknown_without_retry(monkeypatch):
    calls = []

    def denied(args, **options):
        calls.append(args)
        raise PermissionError("synthetic denied query")

    monkeypatch.setattr(paper_clock.subprocess, "run", denied)
    result = await paper_clock.WindowsSynchronizationReader(clock=lambda: NOW).read()
    assert result is None
    assert calls == [["w32tm.exe", "/query", "/status", "/verbose"]]


@pytest.mark.skipif(os.name != "nt", reason="Windows synchronization reader")
def test_windows_reader_subprocess_timeout_is_unknown(monkeypatch):
    calls = []

    def timeout(args, **options):
        calls.append((args, options["timeout"]))
        raise subprocess.TimeoutExpired(args, options["timeout"])

    monkeypatch.setattr(paper_clock.subprocess, "run", timeout)
    assert paper_clock.WindowsSynchronizationReader(clock=lambda: NOW)._read() is None
    assert calls == [(["w32tm.exe", "/query", "/status", "/verbose"], 2)]
