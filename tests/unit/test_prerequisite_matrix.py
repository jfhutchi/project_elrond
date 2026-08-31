"""Every safety check names the authoritative site that must invoke it (#162).

#162's central requirement, and the most important sentence written about this repository:

> A detector/helper existing in a module does **not** satisfy this issue unless every relevant
> authoritative call site actually invokes it.

That is this codebase's dominant defect class. Thirteen instances have shipped with green tests
and no caller: `configure_logging` (#128), the promotion ladder (#130), the counterfactual
evaluator (#127), catalyst enrichment (#124), the capacity ladder (#153), `compile_experiment`
(#154), the anomaly detector, four #82 console slices, the regime and dependence columns, and
`DatasetSnapshot`, which nothing in `src/` had ever constructed (#148).

**A taxonomy of failure codes would not have caught a single one of them.** Every one had a
correct check and no caller; a richer name for the refusal changes nothing. What catches them is
an assertion that names the check *and* the site that must call it, and fails when the call
disappears.

`docs/experiments/unreached_controls.py` is the weaker half: it finds definitions nothing
references, over-reports by design, and produces a list somebody has to read. This is the
inverse and is a gate -- a declared pairing that fails loudly.

**Why source inspection rather than a runtime spy.** A spy proves the call happened on the path
the test drove. This proves it exists in the code, which is the property that survives a future
edit taking a different branch. Both are worth having; this is the one that would have caught the
thirteen, because in every case the call site simply was not written.

Adding a row here is deliberate, and removing one should be argued in a PR rather than done to
make a test pass.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path("src/quantbot")

#: `(check, call site, why it matters)`.
#:
#: `check` is the name that must appear in the call site's source. `call site` is the module that
#: has to invoke it. The third column is why a reader should refuse to delete the row.
#:
#: Deliberately not exhaustive. Every row is a check whose absence has either already caused a
#: defect on this branch or would silently remove a safety property, which is a higher bar than
#: "everything that could be listed".
MATRIX: tuple[tuple[str, str, str], ...] = (
    (
        "unmet_requirement",
        "research/dispatch.py",
        "a backend that cannot carry a job must decline with a reason rather than accept it; "
        "without this the dispatcher routes work to a host that cannot run it",
    ),
    (
        "_require_capable_host",
        "research/worker_adapter.py",
        "a job that starts on an undersized host thrashes onto swap and takes the always-on "
        "control plane with it -- the broker, risk engine and reconciliation live there",
    ),
    (
        # Two rows, deliberately. This one is the *wiring*: the cycle must invoke the method.
        # The row below is the *persistence*: the method must actually write. Mutation testing
        # found that guarding only the second was vacuous -- removing the cycle's call left the
        # method body, whose own `record_capacity_assessment(...)` satisfied the assertion.
        "_record_capacity_assessment",
        "operations/runners.py",
        "#153: without the cycle calling it, the capacity ladder has no production caller at "
        "all, which is the state it shipped in for its whole life",
    ),
    (
        "record_capacity_assessment",
        "operations/runners.py",
        "#153: the capacity ladder had no production caller for its whole life, so the milestone "
        "gate read an empty table and reported the question unassessed forever",
    ),
    (
        "_evaluate_matured_counterfactuals",
        "operations/runners.py",
        "#127: the counterfactual evaluator was built, tested and never called, so no refusal "
        "would have been evaluated even once horizons elapsed",
    ),
    (
        "_record_catalysts",
        "operations/runners.py",
        "#124: catalyst enrichment ran nowhere, so every anomaly was recorded with no answer to "
        "whether it had a public cause",
    ),
    (
        "_detect_and_record_anomalies",
        "operations/runners.py",
        "the anomaly detector had no caller; nothing was ever detected",
    ),
    (
        "configure_logging",
        "cli.py",
        "#128: structlog installs no redactor by default, so the next log line added in good "
        "faith would have printed a key",
    ),
    (
        "active_policy_version",
        "runtime.py",
        "#82: constructing a fresh policy here instead meant every applied change produced a "
        "correct, fully audited version history that changed nothing about what the system does",
    ),
    (
        "_compare_against_metadata",
        "storage/database.py",
        "a store whose live schema differs from the compiled metadata must not open; this is "
        "what makes migration/schema drift impossible rather than merely unlikely",
    ),
    (
        "validate_trading_safety",
        "runtime.py",
        "the capital boundary check has to run where the runtime is assembled, not where it is "
        "convenient",
    ),
)


def _source(relative: str) -> str:
    path = SRC / relative
    assert path.exists(), f"{relative} does not exist; the matrix names a module that is gone"
    return path.read_text(encoding="utf-8")


def _call_name(expression: ast.expr) -> str | None:
    """Return the named check for a direct function or method call."""
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    return None


def _called_names(source: str) -> set[str]:
    """Names the module actually calls, not names it merely loads or passes around.

    The first version counted definitions and imports, so a same-module definition or an unused
    import kept the matrix green. Counting every expression-level name and attribute fixed those
    mutations but left the same failure one step later: assigning a safety check to a callback or
    testing its truthiness still references the name without executing the prerequisite.

    Only the function expression of an AST call establishes invocation. Bare calls and method
    calls are both supported; definitions, imports, annotations, assignments, comparisons, and
    callback references do not satisfy the matrix.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name is not None:
                names.add(name)
    return names


def test_a_reference_without_a_call_does_not_satisfy_the_matrix() -> None:
    """A callback or condition can mention a safety check while never executing it."""
    source = (
        "callback = safety_check\n"
        "bound = service.safety_check\n"
        "if safety_check:\n"
        "    pass\n"
    )

    assert "safety_check" not in _called_names(source)


def test_bare_and_attribute_calls_are_recognized() -> None:
    source = "bare_check()\nservice.attribute_check()\n"

    assert _called_names(source) == {"attribute_check", "bare_check"}


@pytest.mark.parametrize(
    ("check", "call_site", "why"),
    MATRIX,
    ids=[f"{check}->{site}" for check, site, _ in MATRIX],
)
def test_the_authoritative_call_site_still_invokes_the_check(
    check: str, call_site: str, why: str
) -> None:
    """Deleting the call makes this fail, which is #162's stated regression criterion.

    The failure message carries the reason rather than only the names, because the next person
    to see this red will be someone who just removed the call and needs to know what they took
    out.
    """
    assert check in _called_names(_source(call_site)), (
        f"{call_site} no longer calls {check}.\n\n{why}\n\n"
        "If this removal is deliberate, delete the matrix row in the same change and say why "
        "in the PR -- a safety check that quietly loses its caller is the thirteen-times defect "
        "this matrix exists to stop."
    )


def test_every_matrix_row_names_a_check_that_exists() -> None:
    """A row pointing at a deleted function would pass forever without protecting anything.

    This is the matrix's own failure mode: it is a list of names, and a list of names can rot
    into a list of names for things that are gone.
    """
    defined: set[str] = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                defined.add(node.name)

    missing = [check for check, _, _ in MATRIX if check not in defined]

    assert not missing, (
        f"{missing} are named in the prerequisite matrix and defined nowhere in src/; either "
        "the check was renamed and the row was not, or it was deleted and the row is now "
        "guarding nothing"
    )


def test_every_matrix_row_explains_itself() -> None:
    """A row with no reason is one a future reader deletes to make a test pass.

    The reason is the only thing standing between "this assertion is inconvenient" and "this
    assertion is load-bearing", and that judgement is made by someone who was not here.
    """
    for check, call_site, why in MATRIX:
        assert len(why) > 40, f"{check} -> {call_site} has no usable reason recorded"
