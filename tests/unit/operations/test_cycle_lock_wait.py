import asyncio

import pytest

from quantbot.operations import cycle
from quantbot.storage import SingleWriterLock
from quantbot.storage.lock import AlreadyLockedError


@pytest.mark.asyncio
async def test_transient_writer_owner_releases_before_cycle_acquires(tmp_path):
    wait = getattr(cycle, "_bounded_writer_lock", None)
    assert callable(wait), "cycle must wait boundedly for the actual writer owner"
    path = tmp_path / "writer.lock"
    owner = SingleWriterLock(path)
    owner.acquire()

    async def release():
        await asyncio.sleep(0.01)
        owner.release()

    release_task = asyncio.create_task(release())
    async with wait(path, timeout_seconds=0.2):
        with pytest.raises(AlreadyLockedError):
            SingleWriterLock(path).acquire()
    await release_task
    with SingleWriterLock(path):
        pass


@pytest.mark.asyncio
async def test_persistent_writer_is_never_displaced_and_wait_is_bounded(tmp_path):
    wait = getattr(cycle, "_bounded_writer_lock", None)
    assert callable(wait)
    path = tmp_path / "writer.lock"
    with SingleWriterLock(path) as owner:
        with pytest.raises(AlreadyLockedError):
            async with wait(path, timeout_seconds=0.01):
                pytest.fail("must never enter while another writer owns the lock")
        assert owner.is_acquired


@pytest.mark.asyncio
async def test_exception_inside_owned_cycle_releases_writer(tmp_path):
    wait = getattr(cycle, "_bounded_writer_lock", None)
    assert callable(wait)
    path = tmp_path / "writer.lock"
    with pytest.raises(ValueError, match="body failed"):
        async with wait(path):
            raise ValueError("body failed")
    with SingleWriterLock(path):
        pass


@pytest.mark.asyncio
async def test_cancellation_while_waiting_never_takes_the_owner_lock(tmp_path):
    path = tmp_path / "writer.lock"
    owner = SingleWriterLock(path)
    owner.acquire()
    async def attempt():
        async with cycle._bounded_writer_lock(path):
            pytest.fail("owner has not released the lock")
    task = asyncio.create_task(attempt())
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.is_acquired
    owner.release()
    with SingleWriterLock(path):
        pass
