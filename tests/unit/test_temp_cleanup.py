"""Successful test sessions reclaim only their own synthetic stores."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"
SETUP = """
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
import pytest

@pytest.fixture
def stores(tmp_path, tmp_path_factory):
    for path in (tmp_path / 'synthetic.db', Path(os.environ['QUANTBOT_DB_PATH'])):
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE sample (value INTEGER)')
            db.execute('INSERT INTO sample VALUES (1)')
            db.commit()
    base = tmp_path_factory.getbasetemp()
    Path('paths.json').write_text(json.dumps({'base': str(base)}))
    return tmp_path, base
"""


def run_case(tmp_path, body, *, explicit=True, extra_conftest=""):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(CONFTEST, project / "conftest.py")
    with (project / "conftest.py").open("a") as config:
        config.write(textwrap.dedent(extra_conftest))
    (project / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n")
    (project / "test_case.py").write_text(SETUP + textwrap.dedent(body))
    outside = project / "outside"
    outside.mkdir()
    (outside / "keep.db").write_bytes(b"not a test-owned database")
    temp = project / "pytest-temp"
    temp.mkdir()
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_DEBUG_TEMPROOT=str(temp))
    env.pop("PYTEST_ADDOPTS", None)
    command = [sys.executable, "-m", "pytest", "-q", "--junitxml=report.xml"]
    if explicit:
        command.append("--basetemp=" + str(project / "scratch"))
    result = subprocess.run(
        command,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    base = Path(json.loads((project / "paths.json").read_text())["base"])
    assert (outside / "keep.db").read_bytes() == b"not a test-owned database"
    return result, base, project


@pytest.mark.parametrize("explicit", [False, True])
def test_success_removes_session_stores_and_preserves_report(tmp_path, explicit):
    result, base, project = run_case(
        tmp_path,
        """
        def test_example(stores):
            pass
    """,
        explicit=explicit,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not base.exists(), "Successful sessions must remove synthetic databases"
    assert (project / "report.xml").is_file()


@pytest.mark.parametrize("failure", ["call", "teardown", "interrupt"])
def test_unsuccessful_session_preserves_stores(tmp_path, failure):
    body = (
        """
        @pytest.fixture
        def broken():
            yield
            assert False, 'teardown failed'
        def test_example(stores, broken):
            pass
    """
        if failure == "teardown"
        else f"""
        def test_example(stores):
            {"raise KeyboardInterrupt" if failure == "interrupt" else "assert False"}
    """
    )
    result, base, _ = run_case(tmp_path, body)
    assert result.returncode != 0
    assert list(base.rglob("synthetic.db"))
    assert list(base.rglob("quantbot.db"))


LINK = """
def link_directory(target, link):
    if os.name == 'nt':
        from _winapi import CreateJunction
        CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
"""


def test_nested_link_cannot_delete_external_data(tmp_path):
    result, base, _ = run_case(
        tmp_path,
        LINK
        + """
def test_example(stores):
    path, base = stores
    link_directory(Path('outside').resolve(), path / 'linked')
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not base.exists()


@pytest.mark.parametrize("replacement", ["directory", "junction", "broken-junction"])
def test_replaced_root_is_refused_and_original_stores_survive(tmp_path, replacement):
    result, _, project = run_case(
        tmp_path,
        LINK
        + f"""
def test_example(stores):
    path, base = stores
    base.rename(Path('original-scratch').resolve())
    target = Path('outside').resolve()
    if {replacement == "broken-junction"}:
        target = target / 'empty'
        target.mkdir()
    {"base.mkdir()" if replacement == "directory" else "link_directory(target, base)"}
    if {replacement == "broken-junction"}:
        target.rmdir()
""",
    )
    assert result.returncode != 0
    assert "Test scratch cleanup failed" in result.stdout + result.stderr
    assert list((project / "original-scratch").rglob("synthetic.db"))


def test_cleanup_failure_is_not_reported_as_success(tmp_path):
    result, base, _ = run_case(
        tmp_path,
        """
        def test_example(stores):
            path, base = stores
            def deny_cleanup(event, args):
                if event == 'shutil.rmtree' and Path(args[0]) == base:
                    raise PermissionError('synthetic cleanup denial')
            sys.addaudithook(deny_cleanup)
    """,
    )
    assert result.returncode != 0
    assert "Test scratch cleanup failed" in result.stdout + result.stderr
    assert list(base.rglob("synthetic.db"))


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only unlink semantics")
def test_success_removes_readonly_git_object_only_inside_owned_scratch(tmp_path):
    result, base, project = run_case(
        tmp_path,
        """
        def test_example(stores):
            import stat
            path, base = stores
            obj = path / 'installer-repo' / '.git' / 'objects' / '01' / ('a' * 38)
            obj.parent.mkdir(parents=True)
            obj.write_bytes(b'synthetic git object')
            obj.chmod(stat.S_IREAD)
            assert obj.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not base.exists()
    assert (project / "report.xml").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and read-only attributes")
def test_nested_junction_never_clears_external_readonly_attribute(tmp_path):
    import stat

    result, base, project = run_case(
        tmp_path,
        LINK
        + """
def test_example(stores):
    import stat
    path, base = stores
    Path('outside/keep.db').chmod(stat.S_IREAD)
    link_directory(Path('outside').resolve(), path / 'linked')
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not base.exists()
    assert (
        project / "outside" / "keep.db"
    ).stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY


@pytest.mark.parametrize("failure", ["status", "wrapper_status", "wrapper_error", "shutdown_error"])
def test_late_session_failure_preserves_stores(tmp_path, failure):
    action = (
        'raise RuntimeError("late finish failure")'
        if failure == "wrapper_error"
        else "session.exitstatus = pytest.ExitCode.TESTS_FAILED"
    )
    late_hook = (
        """
        @pytest.hookimpl(specname='pytest_sessionfinish')
        def pytest_late_failure(session):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
    """
        if failure == "status"
        else f"""
        def pytest_configure(config):
            class LateFailure:
                @pytest.hookimpl(wrapper=True, tryfirst=True)
                def pytest_sessionfinish(self, session):
                    yield
                    {action}
            config.pluginmanager.register(LateFailure())
    """
    )
    if failure == "shutdown_error":
        late_hook = """
            def pytest_unconfigure(config):
                raise RuntimeError('late shutdown failure')
        """
    result, base, _ = run_case(
        tmp_path,
        """
        def test_example(stores):
            pass
    """,
        extra_conftest=late_hook,
    )
    assert result.returncode != 0
    assert list(base.rglob("synthetic.db"))
