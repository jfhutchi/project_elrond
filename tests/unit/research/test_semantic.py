"""Semantic research that proposes what could be wrong, not what to buy (#37).

The refusals are the substance. A worker that contributes confounders and falsification tests is
useful; the same worker returning a recommendation is a conclusion Elrond did not reach, arriving
with the authority of something that did.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from quantbot.research.hindsight import HindsightRefused
from quantbot.research.models import (
    CostClass,
    ModelCapabilities,
    ModelRole,
    ModelRuntime,
    ModelSpec,
    OpenAICompatibleTransport,
    RoleRouting,
)
from quantbot.research.semantic import (
    SemanticAnalysis,
    SemanticRefused,
    SemanticResearchWorker,
    SemanticWorker,
    analysis_record,
)
from quantbot.research.sources import Source, SourceKind

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
CUTOFF = date(2024, 6, 1)


def source(source_id: str, published: date) -> Source:
    moment = datetime(published.year, published.month, published.day, tzinfo=UTC)
    return Source(
        source_id=source_id,
        kind=SourceKind.NEWS,
        uri=f"https://example.invalid/{source_id}",
        title="Quarterly update",
        publisher="Example Wire",
        published_at=moment,
        retrieved_at=NOW,
        content_hash="c" * 64,
        parser_version="v1",
    )


def worker(answer: dict[str, object], *, cutoff: date | None = CUTOFF) -> SemanticWorker:
    def post(url: str, payload: object, *, timeout_seconds: float) -> str:
        return json.dumps(
            {
                "choices": [{"message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 120},
            }
        )

    spec = ModelSpec(
        provider="openai-compatible",
        model="critic-model",
        version="1",
        endpoint="http://localhost:11434/v1",
        capabilities=ModelCapabilities(
            context_tokens=32768, structured_output=True, local=True, cost_class=CostClass.LOCAL
        ),
    )
    runtime = ModelRuntime(
        RoleRouting(chains={ModelRole.CRITIC: (spec,)}), OpenAICompatibleTransport(post)
    )
    return SemanticWorker(runtime, model_cutoff=cutoff, cutoff_source="model card")


FORWARD = [source("post-1", date(2025, 4, 1)), source("post-2", date(2026, 2, 1))]

USEFUL = {
    "confounders": ["sector beta could produce the same excess return"],
    "falsification_tests": ["the effect should vanish after controlling for market beta"],
    "disagreements": ["bull: margin expansion is structural; bear: it is a one-off tax item"],
    "unsupported_claims": ["the hypothesis asserts a persistence the sources do not address"],
}


def test_it_returns_things_elrond_can_test() -> None:
    """The permitting case, and the reason this worker is worth having.

    The deterministic critic cannot ask "what else could explain this?". A proposed confounder is
    a hypothesis Elrond tests; that is a contribution rather than an opinion.
    """
    analysis, assessment, response = worker(USEFUL).analyse(
        "Margin expansion at these names persists for three quarters.", FORWARD, now=NOW
    )

    assert analysis.confounders and analysis.falsification_tests
    assert analysis.disagreements, "unresolved disagreement is a first-class output"
    assert assessment.usable_as_evidence is True
    assert response.total_tokens == 520


def test_a_directional_call_is_refused_even_hidden_in_another_field() -> None:
    """A schema without a recommendation field is not a schema a model cannot smuggle one into.

    It will write "this is a strong buy" in a confounder if nothing looks, and a reader skimming
    confounders would take it as one. The absent field is the design; the check is what makes the
    design hold against a model that ignores instructions.
    """
    smuggled = dict(USEFUL, confounders=["sector beta", "overall this is a strong buy for Q3"])

    with pytest.raises(SemanticRefused, match="directional call"):
        worker(smuggled).analyse("Margins persist.", FORWARD, now=NOW)


def test_ordinary_words_containing_a_directional_term_are_not_refused() -> None:
    """ "Buyer" and "buyback" appear in honest fundamental analysis.

    A check that fired on those would refuse most real analyses, and a refusal that fires on
    everything is one somebody removes -- which loses the check that matters.
    """
    honest = dict(
        USEFUL,
        confounders=["the buyback programme flatters per-share figures"],
        unsupported_claims=["no buyer concentration data is provided"],
    )

    analysis, _, _ = worker(honest).analyse("Margins persist.", FORWARD, now=NOW)

    assert analysis.confounders, "an honest analysis was refused"


def test_pre_cutoff_sources_are_refused_rather_than_marked() -> None:
    """A bear argument against a 2019 thesis, by a model that knows 2020, is not an argument.

    This is the confirmatory path. A caller wanting an exploratory read of older text uses
    `assess_hindsight` and labels the result itself.
    """
    contaminated = [source("pre-1", date(2019, 4, 1)), *FORWARD]

    with pytest.raises(HindsightRefused, match="published before"):
        worker(USEFUL).analyse("Margins persist.", contaminated, now=NOW)


def test_a_model_with_no_recorded_cutoff_cannot_do_confirmatory_semantic_work() -> None:
    """Absent is not exempt. Forgetting to record a cutoff must not be the cheapest way in."""
    with pytest.raises(HindsightRefused, match="no recorded training cutoff"):
        worker(USEFUL, cutoff=None).analyse("Margins persist.", FORWARD, now=NOW)


def test_an_analysis_with_nothing_to_add_is_refused_rather_than_stored() -> None:
    """A stored empty analysis reads later as "this hypothesis was reviewed".

    Having nothing to add is a legitimate outcome for a model. It is not a legitimate thing to
    record as research input.
    """
    empty = {"confounders": [], "falsification_tests": [], "disagreements": ["they differ"]}

    with pytest.raises(SemanticRefused, match="no confounders and no falsification tests"):
        worker(empty).analyse("Margins persist.", FORWARD, now=NOW)


def test_reviewing_nothing_is_refused() -> None:
    """Prose generated from no sources reads later as though somebody had looked."""
    with pytest.raises(SemanticRefused, match="needs sources"):
        worker(USEFUL).analyse("Margins persist.", [], now=NOW)


def test_the_record_carries_the_reason_the_analysis_was_allowed() -> None:
    """The analysis and the assessment that permitted it travel together.

    Storing one without the other is storing a claim without the reason it is allowed to be a
    claim -- and a later reader could not tell a checked analysis from an unchecked one.
    """
    analysis, assessment, response = worker(USEFUL).analyse("Margins persist.", FORWARD, now=NOW)

    record = analysis_record(analysis, assessment, response)

    assert json.loads(record["confounders"]) == USEFUL["confounders"]
    assert record["model"].startswith("critic-model")
    assert record["prompt_template_hash"]
    assert record["hindsight_cutoff"] == "2024-06-01"
    assert record["hindsight_cutoff_source"] == "model card"
    assert record["hindsight_usable_as_evidence"] == "true"
    assert "post-1" in record["sources"]


def test_the_schema_has_no_field_for_a_recommendation() -> None:
    """The policy "BUY/HOLD/SELL is never an execution instruction" is easy to erode.

    A schema with no such field cannot. This asserts the absence directly, so adding one back is
    a test failure rather than a design drift nobody notices.
    """
    fields = set(SemanticAnalysis.model_fields)

    assert fields == {
        "confounders",
        "falsification_tests",
        "disagreements",
        "unsupported_claims",
    }
    for forbidden in ("recommendation", "action", "signal", "direction", "rating", "conviction"):
        assert forbidden not in fields


# --- the generic worker boundary (#11, #37) ---------------------------------------------------


def worker_input(mandate: str = "Margin expansion at these names persists for three quarters."):
    from quantbot.research.workers import DataRole, WorkerInput  # noqa: PLC0415

    return WorkerInput(
        mandate=mandate,
        datasets=("news:example-wire",),
        data_roles=frozenset({DataRole.DISCOVERY}),
        trial_budget=50,
    )


def adapter(answer: dict[str, object], *, sources=FORWARD, cutoff: date | None = CUTOFF):
    return SemanticResearchWorker(
        worker(answer, cutoff=cutoff),
        lambda _request: sources,
        version="critic-model:1",
        configuration_hash="f" * 64,
    )


def test_the_semantic_worker_satisfies_the_generic_research_worker_contract() -> None:
    """#37: TradingAgents is invoked through #11, not through a path of its own.

    Not tidiness. Budget admission, search accounting and orchestration are written against this
    contract, so a worker reached any other way is a worker none of them can see.
    """
    from quantbot.research.workers import ResearchWorker, WorkerCapability  # noqa: PLC0415

    engine: ResearchWorker = adapter(USEFUL)

    result = engine.run(worker_input(), now=NOW)

    assert WorkerCapability.SEMANTIC_ANALYSIS in engine.spec.capabilities
    assert result.metrics["preserved_disagreements"] == "1"
    assert result.artifacts["analysis.json"]


def test_one_call_is_one_disclosed_candidate() -> None:
    """An agent whose search costs the budget nothing is the laundering path, reopened.

    Cardinality one per call is what makes a prompt sweep cost as many trials as it swept. It
    also makes the spec confirmatory-eligible, which is a statement about the burden being
    accountable -- not about the analysis being evidence.
    """
    from quantbot.research.workers import WorkerTrust  # noqa: PLC0415

    engine = adapter(USEFUL)

    assert engine.run(worker_input(), now=NOW).search_cardinality == 1
    assert engine.spec.trust is WorkerTrust.CONFIRMATORY_ELIGIBLE


def test_a_refused_analysis_does_not_become_a_worker_result() -> None:
    """A result recording a rejected analysis reads later as though the hypothesis was reviewed."""
    smuggled = dict(USEFUL, confounders=["sector beta", "overall this is a strong buy for Q3"])

    with pytest.raises(SemanticRefused):
        adapter(smuggled).run(worker_input(), now=NOW)


def test_an_unverifiable_cutoff_marks_the_result_unsupported_rather_than_clean() -> None:
    """`unsupported` is non-empty, which forces the result exploratory downstream.

    The analysis may be perfectly good. Nothing can establish that, and "nobody could check" is
    a different claim from "checked and fine".
    """
    from quantbot.research.hindsight import HindsightRefused  # noqa: PLC0415

    # The confirmatory path refuses outright, which is the stronger behaviour and already
    # tested above. This pins that the adapter would not have quietly reported it as clean.
    with pytest.raises(HindsightRefused):
        adapter(USEFUL, cutoff=None).run(worker_input(), now=NOW)


def test_the_worker_is_handed_its_sources_rather_than_fetching_them() -> None:
    """A worker that finds its own inputs decides its own point-in-time boundary.

    That boundary is the thing #45 exists to enforce, so the loader is the caller's.
    """
    seen: list[object] = []

    def loader(request):
        seen.append(request.mandate)
        return FORWARD

    engine = SemanticResearchWorker(
        worker(USEFUL), loader, version="critic-model:1", configuration_hash="f" * 64
    )
    engine.run(worker_input("a specific mandate"), now=NOW)

    assert seen == ["a specific mandate"], "the worker did not receive the caller's mandate"


def test_the_semantic_worker_cannot_reach_the_broker_risk_or_kill_switch() -> None:
    """#37: TradingAgents cannot call order paths, mutate trading state, or clear kill switches.

    Asserted against the transitive import closure rather than by reading the code, because the
    dangerous version of this failure is a helper three modules away that someone adds later
    without noticing what it drags in. A worker that can import the kill switch is one refactor
    from being able to clear it, and the policy saying it will not is not a mechanism.

    Run in a fresh interpreter, and that detail is the test. The first version cleared the
    trading modules out of `sys.modules` and re-imported the worker -- but the worker was itself
    already cached, so nothing re-executed and the check passed with `KillSwitchController`
    imported at the top of the module under test. It measured nothing for as long as it existed.

    The reverse direction -- the trading path not importing research -- is pinned separately in
    the budget suite. Both are needed: that one protects the account from research, this one
    protects the account from the worker.
    """
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    probe = (
        "import sys; import quantbot.research.semantic; "
        "bad=sorted(m for m in sys.modules if m.startswith(("
        "'quantbot.brokers','quantbot.execution','quantbot.risk','quantbot.operations'))); "
        "print(','.join(bad))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert completed.stdout.strip() == "", (
        "importing the semantic worker pulled in trading modules: " + completed.stdout.strip()
    )
