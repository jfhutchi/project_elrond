"""Policy context that cannot become a trade (#59).

The issue names the flow this must never support -- `bill advanced committee -> BUY SYMBOL` --
and lists nine failure modes, most of which are ways an interpretation sneaks in beside a record.
So the tests below are almost entirely about what this refuses to conclude.

The two that matter most: a record may not carry a direction, and a record may not carry symbols
without the sector reasoning that produced them. Those are the two fields that turn a policy
artifact into a trading signal, and both are refused at construction rather than left to review.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quantbot.research.policy_context import (
    SOURCE_NAME,
    FederalRegisterSource,
    PolicyDirection,
    PolicyStage,
    PolicyThemeEvent,
    events_from_payload,
    fresh_events,
)

SOURCE = Path("src/quantbot/research/policy_context.py")
RETRIEVED = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
AS_OF = date(2026, 8, 30)


class Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()


class Transport:
    def __init__(self, response: object) -> None:
        self._response = response
        self.params: dict[str, str] = {}

    async def request(self, method, url, *, headers, params, timeout_seconds) -> Any:
        self.params = dict(params)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def document(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "document_number": "2026-18821",
        "title": "Miscellaneous Amendments to Accident Reporting Regulations",
        "type": "Rule",
        "publication_date": "2026-08-28",
        "effective_on": "2026-10-01",
        "html_url": "https://example.invalid/2026-18821",
        "agencies": [{"name": "Transportation Department"}],
    }
    payload.update(overrides)
    return payload


def event(**overrides: object) -> PolicyThemeEvent:
    fields: dict[str, object] = {
        "source": SOURCE_NAME,
        "source_document_id": "2026-18821",
        "source_uri": "https://example.invalid/2026-18821",
        "title": "A rule",
        "agencies": ("Transportation Department",),
        "stage": PolicyStage.FINAL_RULE,
        "published_at": date(2026, 8, 28),
        "effective_on": date(2026, 10, 1),
        "retrieved_at": RETRIEVED,
    }
    fields.update(overrides)
    return PolicyThemeEvent(**fields)  # type: ignore[arg-type]


# --- what it refuses to conclude ------------------------------------------------------------


def test_a_record_may_not_carry_a_direction() -> None:
    """The field where a policy record becomes a trading opinion.

    The issue specifies `inferred_direction`, so it exists -- but nothing in this module is
    entitled to populate it, and a value with no declared method behind it is refused rather
    than stored and later believed.
    """
    with pytest.raises(ValueError, match="trading opinion"):
        event(inferred_direction=PolicyDirection.POSITIVE)

    assert event().inferred_direction is PolicyDirection.UNKNOWN


def test_symbols_without_the_reasoning_that_produced_them_are_refused() -> None:
    """Over-assignment is the failure this field is most prone to.

    A mapping that tagged every technology name would make every anomaly look policy-adjacent,
    and the tag would then be indistinguishable from a real association. Requiring the sector
    reasoning alongside is what makes the assignment auditable at all.
    """
    with pytest.raises(ValueError, match="over-assignment"):
        event(potentially_affected_symbols=("NVDA", "AMD"))


def test_nothing_is_assigned_a_sector_by_default() -> None:
    """The honest state: an agency is not a sector, and pretending otherwise is the noise."""
    parsed = events_from_payload({"results": [document()]}, retrieved_at=RETRIEVED)

    assert parsed[0].potentially_affected_sectors == ()
    assert parsed[0].potentially_affected_symbols == ()


def test_the_module_cannot_reach_an_execution_path() -> None:
    """`bill advanced committee -> BUY SYMBOL`, prevented structurally rather than by intent.

    Asserted the same way `regime.py` and `counterfactual.py` are: if the module cannot import a
    broker, a coordinator or an order, no later edit can quietly let a rule become a trade.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden = {"broker", "execution", "coordinator", "OrderIntent", "OrderSide", "risk"}
    offenders = [name for name in imported if any(item in name for item in forbidden)]
    assert not offenders, f"policy context must not reach execution: {offenders}"


def test_no_model_is_involved() -> None:
    """#45: a model asked about a 2020 rule already knows how it turned out.

    Deterministic parsing means the contamination cannot arise here. A later component that does
    infer from these records has to declare itself, and this assertion is what makes adding one
    a deliberate act rather than a drift.
    """
    body = SOURCE.read_text(encoding="utf-8")

    assert "ModelRuntime" not in body
    assert "call_structured" not in body


# --- the three timestamps ---------------------------------------------------------------------


def test_publication_and_retrieval_are_kept_apart() -> None:
    """Collapsing them is how a point-in-time system stops being one."""
    parsed = events_from_payload({"results": [document()]}, retrieved_at=RETRIEVED)[0]

    assert parsed.published_at == date(2026, 8, 28)
    assert parsed.retrieved_at == RETRIEVED
    assert parsed.effective_on == date(2026, 10, 1)


def test_a_stale_action_is_identified_by_when_it_was_published() -> None:
    """The issue's first named failure mode: a recycled article about an old action.

    Judged on publication rather than retrieval, because the fetch date is a fact about Elrond
    and the publication date is a fact about the world -- and it is the world's date that decides
    whether something was news.
    """
    old = event(published_at=date(2023, 1, 10), effective_on=None)
    recent = event(published_at=date(2026, 8, 28))

    assert old.is_stale(as_of=AS_OF, freshness_days=30)
    assert not recent.is_stale(as_of=AS_OF, freshness_days=30)
    assert fresh_events([old, recent], as_of=AS_OF, freshness_days=30) == (recent,)


def test_a_rule_cannot_take_effect_before_it_was_published() -> None:
    """Reading one date as the other is how a stale action reads as fresh."""
    with pytest.raises(ValueError, match="cannot take effect before"):
        event(published_at=date(2026, 8, 28), effective_on=date(2026, 1, 1))


def test_an_undated_item_is_dropped_rather_than_given_elronds_clock() -> None:
    """A record that cannot be ordered against a decision cannot inform one.

    Dating it to the retrieval would make it look contemporaneous with whatever asked, which is
    the same substitution the catalyst path refuses for news items.
    """
    parsed = events_from_payload(
        {"results": [document(publication_date=None), document()]}, retrieved_at=RETRIEVED
    )

    assert len(parsed) == 1


# --- stage ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Rule", PolicyStage.FINAL_RULE),
        ("Proposed Rule", PolicyStage.PROPOSED_RULE),
        ("Notice", PolicyStage.NOTICE),
        ("Presidential Document", PolicyStage.PRESIDENTIAL),
    ],
)
def test_stage_distinguishes_a_proposal_from_law(raw: str, expected: PolicyStage) -> None:
    """Treating them alike is how legislative noise becomes apparent signal.

    A proposed rule is a request for comment that may never become anything; a final rule is law
    with a date.
    """
    parsed = events_from_payload({"results": [document(type=raw)]}, retrieved_at=RETRIEVED)

    assert parsed[0].stage is expected


def test_an_unrecognised_type_is_named_rather_than_guessed() -> None:
    """A stage this parser does not know is not a notice, and calling it one would be an
    invented fact in the field the issue singles out as a failure mode.
    """
    parsed = events_from_payload(
        {"results": [document(type="Some New Category")]}, retrieved_at=RETRIEVED
    )

    assert parsed[0].stage is PolicyStage.UNRECOGNISED


# --- fetching ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_query_is_bounded_at_the_decision_date() -> None:
    """Look-ahead prevented at the request, not only in a later filter."""
    transport = Transport(Response(200, {"results": []}))
    source = FederalRegisterSource(transport, timeout_seconds=Decimal("5"))

    await source.fetch(since=date(2026, 8, 1), as_of=AS_OF, retrieved_at=RETRIEVED)

    assert transport.params["conditions[publication_date][lte]"] == "2026-08-30"
    assert transport.params["conditions[publication_date][gte]"] == "2026-08-01"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [Response(503, {}), ConnectionError("down"), Response(200, b"<html>")]
)
async def test_a_failure_yields_nothing_and_claims_nothing(failure: object) -> None:
    """Unlike catalyst enrichment, silence here is not load-bearing.

    Nothing concludes "no policy activity" from an empty result, because nothing concludes
    anything from this artifact at all -- it is context for a hypothesis somebody will write.
    So a failure returns nothing rather than a health state, and that asymmetry is deliberate
    rather than an oversight copied from the other source.
    """
    source = FederalRegisterSource(Transport(failure))

    assert await source.fetch(since=date(2026, 8, 1), as_of=AS_OF, retrieved_at=RETRIEVED) == ()


def test_the_identity_changes_when_the_parser_does() -> None:
    """A re-parse under a new version is a new record, not a silent revision of an old one."""
    first = event()
    second = event(parser_version=2)

    assert first.identity != second.identity
    assert first.identity.endswith("2026-18821")
