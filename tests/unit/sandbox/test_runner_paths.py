"""Regression tests for repository paths disclosed to sandboxed code."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import py_compile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

from quantbot.sandbox import SandboxError, SandboxPolicy, SandboxResult, SandboxRunner
from quantbot.sandbox import runner as runner_module


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


class _FakeDistribution:
    def __init__(
        self,
        source_root: Path,
        name: str,
        *,
        requires: tuple[str, ...] = (),
        files: tuple[Path, ...] = (),
    ) -> None:
        self._source_root = source_root
        self.metadata = {"Name": name}
        self.requires = requires
        self.files = files

    def locate_file(self, entry: object) -> Path:
        return self._source_root / Path(str(entry))


@contextmanager
def _repository_local_package(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Expose one importable package from inside the repository to the parent resolver."""
    repository = _repository_root()
    with tempfile.TemporaryDirectory(prefix="sandbox-package-", dir=repository) as directory:
        package_name = "sandbox_probe_package"
        package = Path(directory) / package_name
        package.mkdir()
        package_source = package / "__init__.py"
        package_source.write_text(
            "def source_path():\n    return __file__\n",
            encoding="utf-8",
        )
        py_compile.compile(
            str(package_source),
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )

        original_find_spec = importlib.util.find_spec

        def find_spec(name: str, package_name_arg: str | None = None) -> object:
            if name != package_name:
                return original_find_spec(name, package_name_arg)
            spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
            spec.submodule_search_locations = [str(package)]
            return spec

        monkeypatch.setattr(runner_module.importlib.util, "find_spec", find_spec)
        yield package_name


def _run_with_repository_local_package(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> SandboxResult:
    with _repository_local_package(monkeypatch) as package_name:
        policy = SandboxPolicy(
            allowed_third_party=(package_name,),
            wall_clock_seconds=30.0,
            memory_mb=512,
        )
        return SandboxRunner(policy).run(source.replace("PACKAGE_NAME", package_name))


def test_cached_repository_bytecode_is_not_staged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _repository_local_package(monkeypatch) as package_name:
        destination = tmp_path / "_packages"
        copied = runner_module._copy_import_root(package_name, destination)

    assert copied, "the approved package was not staged for the bytecode check"
    cached_bytecode = list(destination.rglob("*.pyc"))
    assert cached_bytecode == [], (
        f"cached bytecode from the repository package was staged: {cached_bytecode}"
    )


def test_child_sys_path_entries_and_approved_package_are_outside_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """
import json, sys
import PACKAGE_NAME as package
print(json.dumps({
    "sys_path": sys.path,
    "package_file": package.__file__,
    "code_file": package.source_path.__code__.co_filename,
}))
"""
    result = _run_with_repository_local_package(monkeypatch, source)

    assert result.ok, result.stderr
    observed = json.loads(result.stdout)
    repository = _repository_root().resolve()
    disclosed = [
        path
        for path in [*observed["sys_path"], observed["package_file"], observed["code_file"]]
        if Path(path).resolve().is_relative_to(repository)
    ]
    assert disclosed == [], f"the child received repository paths: {disclosed}"


def test_runtime_constructed_names_cannot_locate_repository_from_child_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """
import json, pathlib, sys
import PACKAGE_NAME as package
marker = "py" + "project" + chr(46) + "toml"
credential_file = chr(46) + "en" + "v"
ledger_file = "quant" + "bot" + chr(46) + "db"
raw_clues = [
    *sys.path,
    sys.executable,
    sys.prefix,
    sys.base_prefix,
    package.__file__,
    package.source_path.__code__.co_filename,
]
found = []
for raw in raw_clues:
    clue = pathlib.Path(raw).resolve()
    start = clue if clue.is_dir() else clue.parent
    for candidate in (start, *start.parents):
        if (candidate / marker).is_file():
            found.append({
                "root": str(candidate),
                "credential_file": (candidate / credential_file).is_file(),
                "ledger_file": (candidate / ledger_file).is_file(),
            })
print(json.dumps({"found": found}))
"""
    result = _run_with_repository_local_package(monkeypatch, source)

    assert result.ok, result.stderr
    observed = json.loads(result.stdout)
    assert observed["found"] == [], (
        "runtime-built filenames located the repository from child path metadata: "
        f"{observed['found']}"
    )


def test_shared_dependency_namespace_fails_closed_before_unapproved_sibling_is_staged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository_root().resolve()
    with tempfile.TemporaryDirectory(prefix="sandbox-namespace-", dir=repository) as directory:
        source_root = Path(directory)
        approved_name = "sandbox_approved_package"
        namespace_name = "sandbox_shared_namespace"
        approved = source_root / approved_name
        namespace = source_root / namespace_name
        approved.mkdir()
        namespace.mkdir()
        (approved / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        (namespace / "selected").mkdir()
        (namespace / "unapproved").mkdir()
        (namespace / "selected" / "__init__.py").write_text("", encoding="utf-8")
        (namespace / "unapproved" / "__init__.py").write_text("", encoding="utf-8")

        distributions = {
            "sandbox-approved-distribution": _FakeDistribution(
                source_root,
                "sandbox-approved-distribution",
                requires=("sandbox-selected-distribution>=1",),
            ),
            "sandbox-selected-distribution": _FakeDistribution(
                source_root,
                "sandbox-selected-distribution",
            ),
        }
        package_locations = {
            approved_name: approved,
            namespace_name: namespace,
        }
        monkeypatch.setattr(
            runner_module.importlib.metadata,
            "packages_distributions",
            lambda: {
                approved_name: ["sandbox-approved-distribution"],
                namespace_name: [
                    "sandbox-selected-distribution",
                    "sandbox-unapproved-distribution",
                ],
            },
        )
        monkeypatch.setattr(
            runner_module.importlib.metadata,
            "distribution",
            lambda name: distributions[name],
        )
        original_find_spec = importlib.util.find_spec

        def find_spec(name: str, package_name_arg: str | None = None) -> object:
            location = package_locations.get(name)
            if location is None:
                return original_find_spec(name, package_name_arg)
            spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
            spec.submodule_search_locations = [str(location)]
            return spec

        monkeypatch.setattr(runner_module.importlib.util, "find_spec", find_spec)
        policy = SandboxPolicy(allowed_third_party=(approved_name,))
        sandbox = SandboxRunner(policy)

        with pytest.raises(SandboxError, match="shared.*outside the approved dependency closure"):
            sandbox._prepare(tmp_path, "print('must not execute')", {})

    unapproved = tmp_path / "_packages" / namespace_name / "unapproved"
    assert not unapproved.exists(), (
        f"an unapproved sibling from a shared namespace was staged: {unapproved}"
    )


def test_editable_runtime_dependency_is_staged_from_its_import_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository_root().resolve()
    with tempfile.TemporaryDirectory(prefix="sandbox-dependency-", dir=repository) as directory:
        source_root = Path(directory)
        approved_name = "sandbox_approved_package"
        dependency_name = "sandbox_editable_dependency"
        approved = source_root / approved_name
        dependency = source_root / dependency_name
        approved.mkdir()
        dependency.mkdir()
        (approved / "__init__.py").write_text(
            "import sandbox_editable_dependency as dependency\n"
            "VALUE = dependency.VALUE\n"
            "DEPENDENCY_FILE = dependency.__file__\n",
            encoding="utf-8",
        )
        (dependency / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")

        finder_name = "__editable___sandbox_editable_dependency_0_0_0_finder.py"
        (source_root / finder_name).write_text(
            f"MAPPING = {{'sandbox_editable_dependency': {str(repository)!r}}}\n",
            encoding="utf-8",
        )

        distributions = {
            "sandbox-approved-distribution": _FakeDistribution(
                source_root,
                "sandbox-approved-distribution",
                requires=("sandbox-editable-dependency>=1",),
            ),
            "sandbox-editable-dependency": _FakeDistribution(
                source_root,
                "sandbox-editable-dependency",
                files=(Path(finder_name),),
            ),
        }
        package_locations = {
            approved_name: approved,
            dependency_name: dependency,
        }
        monkeypatch.setattr(
            runner_module.importlib.metadata,
            "packages_distributions",
            lambda: {
                approved_name: ["sandbox-approved-distribution"],
                dependency_name: ["sandbox-editable-dependency"],
            },
        )
        monkeypatch.setattr(
            runner_module.importlib.metadata,
            "distribution",
            lambda name: distributions[name],
        )
        original_find_spec = importlib.util.find_spec

        def find_spec(name: str, package_name_arg: str | None = None) -> object:
            location = package_locations.get(name)
            if location is None:
                return original_find_spec(name, package_name_arg)
            spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
            spec.submodule_search_locations = [str(location)]
            return spec

        monkeypatch.setattr(runner_module.importlib.util, "find_spec", find_spec)
        source = """
import json, sandbox_approved_package as package
print(json.dumps({
    "value": package.VALUE,
    "dependency_file": package.DEPENDENCY_FILE,
}))
"""
        policy = SandboxPolicy(
            allowed_third_party=(approved_name,),
            wall_clock_seconds=30.0,
            memory_mb=512,
        )
        result = SandboxRunner(policy).run(source)

    assert result.ok, result.stderr
    observed = json.loads(result.stdout)
    assert observed["value"] == 7, "the editable runtime dependency was not importable"
    assert not Path(observed["dependency_file"]).resolve().is_relative_to(repository), (
        "the editable runtime dependency loaded from its repository source root: "
        f"{observed['dependency_file']}"
    )


def test_approved_distribution_and_runtime_dependencies_are_staged() -> None:
    source = """
import json, pydantic
class Payload(pydantic.BaseModel):
    value: int
print(json.dumps({"module_file": pydantic.__file__, "payload": Payload(value=7).model_dump()}))
"""
    policy = SandboxPolicy(
        allowed_third_party=("pydantic",),
        wall_clock_seconds=30.0,
        memory_mb=512,
    )
    result = SandboxRunner(policy).run(source)

    assert result.ok, result.stderr
    observed = json.loads(result.stdout)
    assert observed["payload"] == {"value": 7}, "a staged dependency failed at runtime"
    assert not Path(observed["module_file"]).resolve().is_relative_to(
        _repository_root().resolve()
    ), f"the approved distribution still loaded from the repository: {observed}"


@pytest.mark.parametrize(
    "metadata_name",
    (
        "__editable___sandbox_probe_package_0_0_0_finder.py",
        "sandbox_probe_package.egg-link",
    ),
)
def test_parent_install_location_metadata_is_not_staged(metadata_name: str) -> None:
    repository = _repository_root().resolve()
    with tempfile.TemporaryDirectory(prefix="sandbox-metadata-", dir=repository) as directory:
        source_root = Path(directory)
        metadata = source_root / metadata_name
        metadata.write_text(
            f"MAPPING = {{'sandbox_probe_package': {str(repository)!r}}}\n",
            encoding="utf-8",
        )

        distribution = _FakeDistribution(
            source_root,
            "sandbox-probe-package",
            files=(Path(metadata_name),),
        )
        destination = source_root / "staged"
        destination.mkdir()
        runner_module._copy_distribution(distribution, destination)  # type: ignore[arg-type]

        staged_metadata = destination / metadata_name
        assert not staged_metadata.exists(), (
            "parent-install location metadata containing the repository path was staged: "
            f"{staged_metadata}"
        )


def test_prepare_fails_closed_if_a_child_sys_path_is_inside_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository_root().resolve()
    monkeypatch.setattr(runner_module, "_stdlib_paths", lambda: [str(repository)])
    runner = SandboxRunner(SandboxPolicy(allowed_third_party=()))

    with pytest.raises(SandboxError, match="sys.path.*repository"):
        runner._prepare(tmp_path, "print('must not execute')", {})


def test_the_memory_probe_actually_reads_a_number_on_this_platform() -> None:
    """The probe has to work, not merely exist.

    `_memory_mb` returns `None` when it cannot measure, and the monitor is written to decline to
    act on `None` rather than kill a healthy run. That is the right default and it has one nasty
    consequence: a probe that never works is indistinguishable from a run that never breached.

    It happened. `_memory_mb` shelled out to PowerShell, so on Linux every poll returned `None`
    and `SandboxPolicy.memory_mb` enforced nothing at all while still reading like a control.
    Nothing failed, because the only assertion anywhere was that a breaching run gets killed --
    and on Linux the runaway simply ran until the kernel OOM killer took the host with it.

    So this asserts the probe against a process that certainly exists and certainly occupies
    memory: the interpreter running the test.
    """
    import os
    import subprocess
    import sys

    from quantbot.sandbox.runner import _memory_mb

    class _Self:
        pid = os.getpid()

    measured = _memory_mb(cast("subprocess.Popen[bytes]", _Self()))

    assert measured is not None, (
        f"the memory probe cannot measure anything on {sys.platform}, so the sandbox's memory "
        "ceiling is inert here and every poll will decline to act"
    )
    assert measured > 1.0, f"{measured}MB is not a plausible reading for a Python interpreter"
