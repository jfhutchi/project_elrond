"""Every safety check names the exact authoritative site that must invoke it (#162).

A helper existing somewhere in the repository is not evidence that the runtime uses it. Each row
below names four separate facts: the exact definition, the exact call target, the authoritative
module that must call it, and why removing that wiring matters.

The distinction between definition, inner persistence, and outer wiring is deliberate. This
project has repeatedly shipped correct controls with green unit tests and no production caller.
A matrix that accepts an unrelated same-named method, a callback reference, or an inner helper
call after the outer call was deleted would repeat that defect inside the test meant to catch it.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

import pytest

SRC = pathlib.Path("src/quantbot")


@dataclass(frozen=True, slots=True)
class Prerequisite:
    """One exact definition and the authoritative call that must reach it."""

    definition: str
    definition_site: str
    call_target: str
    call_site: str
    why: str


MATRIX: tuple[Prerequisite, ...] = (
    Prerequisite(
        definition="unmet_requirement",
        definition_site="placement.py",
        call_target="unmet_requirement",
        call_site="research/dispatch.py",
        why=(
            "a backend must apply the host-capability rule rather than accept a job on declared "
            "capacity alone"
        ),
    ),
    Prerequisite(
        definition="WorkerBackend.can_run",
        definition_site="research/dispatch.py",
        call_target="backend.can_run",
        call_site="research/dispatch.py",
        why=(
            "the dispatcher must consult each backend before routing; an inner capability helper "
            "does not protect dispatch when this outer call disappears"
        ),
    ),
    Prerequisite(
        definition="SubprocessWorker._require_capable_host",
        definition_site="research/worker_adapter.py",
        call_target="self._require_capable_host",
        call_site="research/worker_adapter.py",
        why=(
            "a directly constructed worker still needs a final pre-spawn host check so it cannot "
            "thrash the always-on control plane onto swap"
        ),
    ),
    Prerequisite(
        definition="AdaptiveMomentumRunner._record_capacity_assessment",
        definition_site="operations/runners.py",
        call_target="self._record_capacity_assessment",
        call_site="operations/runners.py",
        why=(
            "#153: the cycle must invoke the capacity assessment; its repository write can remain "
            "perfectly intact after the outer production call is deleted"
        ),
    ),
    Prerequisite(
        definition="StorageRepository.record_capacity_assessment",
        definition_site="storage/repositories.py",
        call_target="StorageRepository().record_capacity_assessment",
        call_site="operations/runners.py",
        why=(
            "#153: the assessment must become durable or the milestone gate reads an empty table "
            "and reports the capacity question unassessed forever"
        ),
    ),
    Prerequisite(
        definition="AdaptiveMomentumRunner._evaluate_matured_counterfactuals",
        definition_site="operations/runners.py",
        call_target="self._evaluate_matured_counterfactuals",
        call_site="operations/runners.py",
        why=(
            "#127: matured refusals must be evaluated by the cycle or no counterfactual outcome "
            "will ever be measured after its horizon elapses"
        ),
    ),
    Prerequisite(
        definition="AdaptiveMomentumRunner._record_catalysts",
        definition_site="operations/runners.py",
        call_target="self._record_catalysts",
        call_site="operations/runners.py",
        why=(
            "#124: production must enrich the anomalies that fired or every event remains without "
            "an answer to whether it had a public cause"
        ),
    ),
    Prerequisite(
        definition="AdaptiveMomentumRunner._record_anomalies",
        definition_site="operations/runners.py",
        call_target="self._record_anomalies",
        call_site="operations/runners.py",
        why=(
            "the cycle must reach anomaly recording; guarding only its inner detector call stays "
            "green when the entire outer production call is removed"
        ),
    ),
    Prerequisite(
        definition="AdaptiveMomentumRunner._detect_and_record_anomalies",
        definition_site="operations/runners.py",
        call_target="self._detect_and_record_anomalies",
        call_site="operations/runners.py",
        why=(
            "the contained anomaly recorder must actually invoke the detector and persistence body "
            "rather than return an empty observation set"
        ),
    ),
    Prerequisite(
        definition="configure_logging",
        definition_site="logging.py",
        call_target="configure_logging",
        call_site="cli.py",
        why=(
            "#128: the CLI logging helper must install structlog secret redaction before anything "
            "is allowed to emit a structured log"
        ),
    ),
    Prerequisite(
        definition="_configure_logging",
        definition_site="cli.py",
        call_target="_configure_logging",
        call_site="cli.py",
        why=(
            "#128: the authoritative CLI entrypoint must call its logging helper; guarding only "
            "the helper's inner configure_logging call misses the original no-caller defect"
        ),
    ),
    Prerequisite(
        definition="active_policy_version",
        definition_site="risk/policy_versions.py",
        call_target="active_policy_version",
        call_site="runtime.py",
        why=(
            "#82: runtime composition must load the recorded policy or an applied successor changes "
            "history while leaving the rule the process enforces unchanged"
        ),
    ),
    Prerequisite(
        definition="Database._compare_against_metadata",
        definition_site="storage/database.py",
        call_target="self._compare_against_metadata",
        call_site="storage/database.py",
        why=(
            "opening or migrating a store must compare its live schema to compiled metadata so "
            "schema drift cannot masquerade as a supported database"
        ),
    ),
    Prerequisite(
        definition="validate_trading_safety",
        definition_site="config.py",
        call_target="validate_trading_safety",
        call_site="risk/gates.py",
        why=(
            "the authoritative order gate must enforce account and environment safety; a doctor-only "
            "call in runtime.py does not protect broker submission"
        ),
    ),
    Prerequisite(
        definition="evaluate_order_gate",
        definition_site="risk/gates.py",
        call_target="evaluate_order_gate",
        call_site="execution/coordinator.py",
        why=(
            "the execution coordinator must invoke the authoritative order gate before durable "
            "intent creation and broker mutation"
        ),
    ),
)


def _source(relative: str) -> str:
    path = SRC / relative
    assert path.exists(), f"{relative} does not exist; the matrix names a module that is gone"
    return path.read_text(encoding="utf-8")


def _expression_name(expression: ast.expr) -> str | None:
    """Return a receiver-qualified expression name without evaluating it."""
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        owner = _expression_name(expression.value)
        return expression.attr if owner is None else f"{owner}.{expression.attr}"
    if isinstance(expression, ast.Call):
        target = _expression_name(expression.func)
        return None if target is None else f"{target}()"
    return None


def _called_targets(source: str) -> set[str]:
    """Receiver-qualified targets that are actually called in source."""
    targets: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            target = _expression_name(node.func)
            if target is not None:
                targets.add(target)
    return targets


class _DefinitionNames(ast.NodeVisitor):
    """Collect definitions with their lexical owner, e.g. StorageRepository.record_signal."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self._owners: list[str] = []

    def _visit_definition(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        qualified = ".".join((*self._owners, node.name))
        self.names.add(qualified)
        self._owners.append(node.name)
        for child in node.body:
            self.visit(child)
        self._owners.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast visitor API
        self._visit_definition(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast visitor API
        self._visit_definition(node)

    def visit_AsyncFunctionDef(  # noqa: N802 - ast visitor API
        self, node: ast.AsyncFunctionDef
    ) -> None:
        self._visit_definition(node)


def _defined_qualnames(source: str) -> set[str]:
    visitor = _DefinitionNames()
    visitor.visit(ast.parse(source))
    return visitor.names


def test_a_reference_without_a_call_does_not_satisfy_the_matrix() -> None:
    source = (
        "callback = service.safety_check\n"
        "if service.safety_check:\n"
        "    pass\n"
    )

    assert "service.safety_check" not in _called_targets(source)


def test_call_targets_keep_the_receiver_and_constructor() -> None:
    source = (
        "required.check()\n"
        "unrelated.check()\n"
        "Repository(session).write(row)\n"
    )

    assert _called_targets(source) == {
        "required.check",
        "unrelated.check",
        "Repository().write",
        "Repository",
    }


def test_definition_names_keep_the_lexical_owner() -> None:
    source = (
        "class Required:\n"
        "    def check(self):\n"
        "        pass\n"
        "class Unrelated:\n"
        "    def check(self):\n"
        "        pass\n"
    )

    assert _defined_qualnames(source) == {
        "Required",
        "Required.check",
        "Unrelated",
        "Unrelated.check",
    }


@pytest.mark.parametrize(
    "prerequisite",
    MATRIX,
    ids=[f"{item.call_target}->{item.call_site}" for item in MATRIX],
)
def test_the_authoritative_call_site_still_invokes_the_check(
    prerequisite: Prerequisite,
) -> None:
    assert prerequisite.call_target in _called_targets(_source(prerequisite.call_site)), (
        f"{prerequisite.call_site} no longer calls {prerequisite.call_target}.\n\n"
        f"{prerequisite.why}\n\n"
        "If this removal is deliberate, delete the matrix row in the same change and explain "
        "why. A safety check that quietly loses its caller is the defect this matrix exists to stop."
    )


@pytest.mark.parametrize(
    "prerequisite",
    MATRIX,
    ids=[f"{item.definition_site}:{item.definition}" for item in MATRIX],
)
def test_every_matrix_row_names_the_exact_definition(
    prerequisite: Prerequisite,
) -> None:
    assert prerequisite.definition in _defined_qualnames(
        _source(prerequisite.definition_site)
    ), (
        f"{prerequisite.definition_site} no longer defines {prerequisite.definition}; an "
        "unrelated same-named helper elsewhere must not keep a stale matrix row green"
    )


def test_every_matrix_row_explains_itself() -> None:
    for item in MATRIX:
        assert len(item.why) > 40, (
            f"{item.call_target} -> {item.call_site} has no usable reason recorded"
        )
