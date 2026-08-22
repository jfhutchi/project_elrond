"""The project-level economic objective, and why it has to be frozen before anything is compared.

The repository's own record is the argument for this file existing. SPY buy-and-hold wins on
terminal wealth, SPY_SMA200 wins on Sharpe and drawdown, vol targeting trades CAGR for drawdown,
and leverage trades drawdown for CAGR. With four metrics and four candidates something always
wins on something, so a research system with no frozen objective can search not only strategies
but definitions of success -- and it will find one.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest
import yaml

from quantbot.research.mandate import (
    DEFAULT_MANDATE_PATH,
    EconomicObjective,
    MandateError,
    MandateStatus,
    Objective,
    load_economic_objective,
)

REPOSITORY = Path(__file__).resolve().parents[3]


def test_the_projects_objective_loads_and_says_what_it_is_optimising() -> None:
    """The shipped mandate is real, not an example."""
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)

    assert frozen.objective is Objective.BENCHMARK_RELATIVE_GROWTH
    assert frozen.benchmark_symbol == "SPY"
    # The 15% entry halt, not the 20% liquidation tier. A strategy that reaches the entry halt
    # cannot take positions, so it cannot earn the drawdown back -- STATUS.md records that the
    # halt has no exit, which is what makes it the binding constraint.
    assert frozen.max_drawdown_bps == 1500
    assert frozen.minimum_meaningful_improvement_bps > 0


def test_the_shipped_objective_is_not_yet_the_operators() -> None:
    """`PROVISIONAL` is the honest state and it has consequences.

    An earlier version of this test said every value in the file was transcribed from STATUS.md
    and CLAUDE.md, so freezing them was bookkeeping. #54's review disagreed and was right: the
    capital range, the improvement threshold, the turnover cap, the horizon and the choice of
    objective are proposals an agent wrote. Only the operator decides what this project is
    optimising, and until they do, nothing here may claim to have met it.
    """
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)

    assert frozen.status is MandateStatus.PROVISIONAL
    assert not frozen.ratified()
    objections = frozen.ratification_objections()
    assert objections, "an unratified objective must not back a human live-review claim"
    assert "PROVISIONAL" in objections[0]


def test_a_ratified_objective_clears_live_review_and_a_superseded_one_does_not() -> None:
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)

    ratified = frozen.model_copy(
        update={
            "status": MandateStatus.RATIFIED,
            "ratified_by": "hutch",
            "ratified_at": date(2026, 8, 22),
        }
    )
    assert ratified.ratified()
    assert ratified.ratification_objections() == ()

    superseded = frozen.model_copy(update={"status": MandateStatus.SUPERSEDED})
    assert superseded.ratification_objections()
    assert "superseded" in superseded.ratification_objections()[0]


def test_claimed_ratification_without_a_ratifier_is_refused() -> None:
    """Half-filled ratification is the state that would quietly authorise something."""
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)

    with pytest.raises(ValueError, match="records who ratified it"):
        frozen.model_copy(update={"status": MandateStatus.RATIFIED}).model_validate(
            frozen.model_copy(update={"status": MandateStatus.RATIFIED}).model_dump()
        )

    with pytest.raises(ValueError, match="ratification details on an unratified mandate"):
        EconomicObjective.model_validate(frozen.model_dump() | {"ratified_by": "hutch"})


def test_a_missing_objective_raises_rather_than_defaulting(tmp_path: Path) -> None:
    """A missing mandate must not become a permissive one.

    Returning a built-in default here would be an agent choosing the project's goal at the one
    moment nobody is looking, and it would do it silently.
    """
    with pytest.raises(MandateError, match="nothing may be compared on return"):
        load_economic_objective(tmp_path / "absent.yaml")


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    """A key nobody consumes reads like a constraint in review and constrains nothing."""
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)
    payload = yaml.safe_load((REPOSITORY / DEFAULT_MANDATE_PATH).read_text(encoding="utf-8"))
    payload["max_sector_concentration_bps"] = 3000
    location = tmp_path / "aspirational.yaml"
    location.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(MandateError, match="not a usable economic objective"):
        load_economic_objective(location)

    assert frozen.content_hash  # the real one still loads


def test_editing_the_objective_changes_its_identity() -> None:
    """A candidate names the mandate it satisfied, so the name has to move when the rules do."""
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)
    looser = frozen.model_copy(update={"max_drawdown_bps": 3000})

    assert looser.content_hash != frozen.content_hash
    assert looser.identity != frozen.identity
    assert frozen.identity.startswith("economic-objective-v1-")


def test_only_the_ladder_reads_a_mandate_from_anywhere_but_disk() -> None:
    """`objective_path` exists for tests; nothing in production may pass it.

    Same arrangement as `issue_measured_readiness`: the guarantee is not that the parameter is
    hard to reach, it is that exactly one call site reaches it and that is checked rather than
    assumed. A caller holding the mandate holds the definition of success.
    """
    source_root = REPOSITORY / "src" / "quantbot"
    callers: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "load_economic_objective" and node.args:
                callers.append(path.relative_to(source_root).as_posix())

    # By file, not by line: pinning line numbers makes an unrelated edit look like a boundary
    # violation, and a test that cries wolf gets its expectation updated without being read.
    assert set(callers) == {"research/promotion.py"}, (
        "a mandate loaded from a caller-chosen path is a definition of success a caller chose: "
        f"{callers}"
    )


PROPOSED_BY_AN_AGENT = (
    "intended_capital_range_usd",
    "minimum_meaningful_improvement_bps",
    "max_annual_turnover",
    "evaluation_horizon_years",
    "objective",
)


def test_every_value_an_agent_chose_says_so_in_the_file() -> None:
    """The five values #54's review could not trace to an operator decision are marked.

    A mandate whose comments claim more provenance than it has is worse than one with no
    comments: an operator skimming it for what still needs deciding would find nothing, ratify
    it, and have ratified an agent's guesses at what the project is for. The marking is the
    deliverable, so it is what gets pinned.
    """
    lines = (REPOSITORY / DEFAULT_MANDATE_PATH).read_text(encoding="utf-8").splitlines()
    for key in PROPOSED_BY_AN_AGENT:
        index = next(i for i, line in enumerate(lines) if line.startswith(f"{key}:"))
        preamble: list[str] = []
        while index and lines[index - 1].startswith("#"):
            index -= 1
            preamble.append(lines[index])
        assert any("[PROPOSED]" in line for line in preamble), (
            f"{key} is a policy value an agent chose and the file does not say so"
        )


def test_ratifying_does_not_change_which_objective_it_is() -> None:
    """The lifecycle #54 asked to be explicit rather than accidental.

    If ratification changed the content hash, the operator's signature would invalidate every
    assessment made against the values they were signing -- at the exact moment those values
    became usable. Policy edits must change the identity; the signature must not.
    """
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)
    ratified = frozen.model_copy(
        update={
            "status": MandateStatus.RATIFIED,
            "ratified_by": "hutch",
            "ratified_at": date(2026, 8, 22),
        }
    )

    assert ratified.identity == frozen.identity
    assert ratified.content_hash == frozen.content_hash
    # And the half that has to keep working: a policy edit is a different objective.
    assert frozen.model_copy(update={"max_annual_turnover": 24}).identity != frozen.identity
    assert (
        frozen.model_copy(update={"minimum_meaningful_improvement_bps": 1}).identity
        != frozen.identity
    )


def test_an_agent_cannot_ratify_the_projects_objective() -> None:
    """`ratified_by` naming an automated identity is refused (#54).

    This cannot stop an agent typing the operator's name, and nothing in software can. It stops
    the realistic version: a coding agent that has just written a mandate flipping `status` to
    RATIFIED as part of tidying it up, honestly signing its own work.
    """
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)
    for ratifier in ("claude-opus-5", "GPT-5.6 Sol", "research-agent", "elrond-bot"):
        with pytest.raises(ValueError, match="automated ratifier"):
            EconomicObjective.model_validate(
                frozen.model_dump()
                | {
                    "status": MandateStatus.RATIFIED,
                    "ratified_by": ratifier,
                    "ratified_at": date(2026, 8, 22),
                }
            )

    # A person still can, or the check would be a ban rather than a boundary.
    assert EconomicObjective.model_validate(
        frozen.model_dump()
        | {
            "status": MandateStatus.RATIFIED,
            "ratified_by": "hutch",
            "ratified_at": date(2026, 8, 22),
        }
    ).ratified()
