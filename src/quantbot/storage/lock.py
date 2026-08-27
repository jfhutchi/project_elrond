"""Cross-process single-writer lock with same-process protection."""

from __future__ import annotations

import importlib
import os
import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class AlreadyLockedError(RuntimeError):
    """Raised when another writer already owns the configured lock."""


class SingleWriterLock:
    """Own an advisory one-byte file lock for the lifetime of a writer."""

    _registry_guard = threading.Lock()
    _process_registry: set[Path] = set()

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._handle: BinaryIO | None = None

    @property
    def is_acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        with self._registry_guard:
            if self.path in self._process_registry:
                raise AlreadyLockedError(f"Writer lock is already held: {self.path}")
            self._process_registry.add(self.path)

        handle: BinaryIO | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
        except OSError:
            if handle is not None:
                handle.close()
            self._discard_registration()
            raise

        try:
            self._lock_handle(handle)
        except OSError as exc:
            handle.close()
            self._discard_registration()
            raise AlreadyLockedError(f"Writer lock is already held: {self.path}") from exc

        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            self._unlock_handle(handle)
        finally:
            handle.close()
            self._handle = None
            self._discard_registration()

    def _discard_registration(self) -> None:
        with self._registry_guard:
            self._process_registry.discard(self.path)

    @staticmethod
    def _lock_handle(handle: BinaryIO) -> None:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: BinaryIO) -> None:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> SingleWriterLock:
        self.acquire()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()
