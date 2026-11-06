"""Suite-wide isolation from the operator's real deployment.

**This exists because the test suite migrated a live trading database.**

`Database.__init__` calls `command.upgrade(config, "head")`, so merely *opening* a store applies
every migration the current checkout carries. `quantbot.cli.main` builds one for every command
from `QUANTBOT_DB_PATH`, read out of the ambient environment. On a machine where the operator has
exported that variable -- which is the machine this is developed on -- a unit test that invoked
the CLI opened the deployment's database and upgraded it to a schema version that existed only on
an unmerged branch. Nothing was lost, because the migration was additive, but every build at the
older version then refused to open the store at all.

CI never saw any of it. CI's environment is clean, which is exactly why this could not be left to
be noticed: the suite was green while reaching outside the repository and writing to it.

So every test gets its own paths, and a test that wants specific ones still sets them itself --
`monkeypatch.setenv` inside a test runs after this fixture and wins.

Only the variables that name somewhere the process will **write** are redirected. Credentials and
endpoint configuration are deliberately left alone: a test that opts into a live endpoint should
keep being able to find it, and scrubbing those here would silently disable coverage rather than
protect anything.
"""

from __future__ import annotations

import gc
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

#: Environment variables naming somewhere the process will write. `QUANTBOT_DB_PATH` is the one
#: that caused the incident; the others are here because the reasoning is identical -- a lock
#: file or a report written into the operator's deployment is still a test reaching outside the
#: repository.
REDIRECTED = ("QUANTBOT_DB_PATH", "QUANTBOT_LOCK_PATH", "QUANTBOT_REPORTS_DIR")
_TEMP_ROOT = pytest.StashKey[tuple[Path, tuple[int, int]]]()


def _remove_owned_readonly_file(root, identity, function, path, exc_info):
    """Retry only a Windows read-only regular file, using its verified open handle.

    Handle-based attributes/deletion avoid following a replaced filename or junction.
    ACL errors, locks, reparse points and multiply linked files remain failures.
    """
    error = exc_info[1]
    if (
        os.name != "nt"
        or function not in (os.unlink, os.remove)
        or not isinstance(error, PermissionError)
    ):
        raise error
    target = Path(path)
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise OSError("cleanup target outside owned scratch") from error
    if not relative.parts:
        raise error
    ancestor = root
    for part in (None, *relative.parts[:-1]):
        if part is not None:
            ancestor /= part
        info = ancestor.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (ancestor == root and (info.st_dev, info.st_ino) != identity)
        ):
            raise OSError("cleanup ancestor identity changed") from error
    before = target.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or not before.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    ):
        raise error

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class BasicInfo(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_longlong) for name in ("creation", "access", "write", "change")
        ] + [("attributes", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    # DELETE | READ_ATTRIBUTES | WRITE_ATTRIBUTES; shared access; OPEN_EXISTING;
    # OPEN_REPARSE_POINT deliberately opens the link itself, never its target.
    handle = kernel.CreateFileW(
        ctypes.c_wchar_p(str(target)), 0x10180, 7, None, 3, 0x00200000, None
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        opened = os.fstat(descriptor)
        basic = BasicInfo()
        if not kernel.GetFileInformationByHandleEx(
            wintypes.HANDLE(handle), 0, ctypes.byref(basic), ctypes.sizeof(basic)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        actual = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(wintypes.HANDLE(handle), actual, len(actual), 0)
        root_now = root.lstat()
        if (
            not 0 < length < len(actual)
            or Path(actual.value.removeprefix("\\\\?\\")) != target
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_nlink != 1
            or (root_now.st_dev, root_now.st_ino) != identity
            or root_now.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or basic.attributes
            & (stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DIRECTORY)
            or not basic.attributes & stat.FILE_ATTRIBUTE_READONLY
        ):
            raise OSError("opened cleanup file is not the owned regular file")
        basic.attributes &= ~stat.FILE_ATTRIBUTE_READONLY
        if not basic.attributes:
            basic.attributes = stat.FILE_ATTRIBUTE_NORMAL
        if not kernel.SetFileInformationByHandle(
            wintypes.HANDLE(handle), 0, ctypes.byref(basic), ctypes.sizeof(basic)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        delete = ctypes.c_byte(1)
        if not kernel.SetFileInformationByHandle(
            wintypes.HANDLE(handle), 4, ctypes.byref(delete), ctypes.sizeof(delete)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if descriptor is not None:
            os.close(descriptor)
        else:
            kernel.CloseHandle(wintypes.HANDLE(handle))


@pytest.fixture(autouse=True)
def isolate_deployment_paths(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Point every write-bearing path at this test's own directory.

    Autouse and unconditional. A fixture that only acted when the variable was already set would
    leave the default relative `quantbot.db` being created in whatever directory the suite ran
    from, which is the same class of problem one level quieter.
    """
    sandbox: Path = tmp_path_factory.mktemp("deployment")
    if _TEMP_ROOT not in request.config.stash:
        root = tmp_path_factory.getbasetemp()
        identity = root.lstat()
        request.config.stash[_TEMP_ROOT] = (root, (identity.st_dev, identity.st_ino))
        request.config.add_cleanup(lambda session=request.session: _cleanup_test_session(session))
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(sandbox / "quantbot.db"))
    monkeypatch.setenv("QUANTBOT_LOCK_PATH", str(sandbox / "quantbot.lock"))
    monkeypatch.setenv("QUANTBOT_REPORTS_DIR", str(sandbox / "reports"))


def _cleanup_test_session(session: pytest.Session) -> None:
    """Remove this session's scratch only after every test and finish hook succeeds.

    Includes explicit --basetemp and factory-created deployment directories.
    Reports belong outside scratch; failures retain their diagnostic databases.
    """
    owned = session.config.stash.get(_TEMP_ROOT, None)
    if sys.exc_info()[0] is not None or session.exitstatus != 0 or owned is None:
        return
    root, identity = owned
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    try:
        # Release unreachable SQLite connections before Windows file deletion.
        gc.collect()
        try:
            current = root.lstat()
        except FileNotFoundError:
            return
        reparse = getattr(current, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        if (
            not root.is_absolute()
            or root.resolve() != root
            or reparse
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != identity
        ):
            raise OSError("temporary directory identity changed; refusing cleanup")
        # rmtree unlinks nested symlinks/Windows junctions without following them.
        shutil.rmtree(
            root,
            onerror=lambda function, path, error: _remove_owned_readonly_file(
                root, identity, function, path, error
            ),
        )
    except OSError as error:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        message = f"Test scratch cleanup failed: {root}: {error}"
        if reporter is None:
            print(message, file=sys.stderr)
        else:
            reporter.write_line(message, red=True)
    else:
        if reporter is not None:
            reporter.write_line(f"Removed synthetic test data: {root}")
