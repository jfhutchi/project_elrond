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

import pytest

from quantbot.sandbox import SandboxError, SandboxPolicy, SandboxResult, SandboxRunner
from quantbot.sandbox import runner as runner_module


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


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
        py_compile.compile(str(package_source), doraise=True)

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

        class LocationBearingDistribution:
            files = (Path(metadata_name),)

            @staticmethod
            def locate_file(entry: object) -> Path:
                return source_root / Path(str(entry))

        destination = source_root / "staged"
        destination.mkdir()
        runner_module._copy_distribution(  # type: ignore[arg-type]
            LocationBearingDistribution(), destination
        )

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
