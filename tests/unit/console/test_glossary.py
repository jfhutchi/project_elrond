"""The jargon, explained once (#82).

Added because an operator reading the risk policy asked what correlation meant. The field
descriptions explain each *setting*; this explains the words those explanations are made of, and
the settings link to it.

The tests are mostly about the glossary staying honest as the console grows: a term that appears
on a page and not here is jargon nobody defined, and a definition that has drifted from what the
system actually does is worse than none.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.glossary import GLOSSARY, anchor, terms_in
from quantbot.storage import Database


@pytest.fixture
def client(tmp_path: Path):
    with Database.create(tmp_path / "glossary.db") as database:
        with TestClient(create_app(database)) as client:
            yield client


def test_the_page_defines_the_term_that_prompted_it(client: TestClient) -> None:
    """Correlation, which is meaningless to anybody who has not met the idea."""
    body = client.get("/glossary").text

    assert "move at the same time" in body
    assert "one bet with five names" in body


def test_every_entry_is_defined_twice(client: TestClient) -> None:
    """A one-line gist for a reader passing through, and a proper explanation for one deciding.

    Collapsing them would serve one reader and fail the other: the short form is useless to
    somebody about to change a threshold, and the long form is a wall to somebody who just met
    the word.
    """
    for entry in GLOSSARY:
        assert entry.short.endswith("."), entry.term
        assert len(entry.short) < 120, f"{entry.term} short form is not short"
        assert len(entry.long) > 150, f"{entry.term} long form is not an explanation"
        assert entry.long != entry.short


def test_a_setting_links_to_the_terms_its_description_uses(client: TestClient) -> None:
    """So a reader who does not know a word can find out without leaving the page.

    The link is offered beneath the description rather than woven into it: substituting links
    into prose means rewriting the prose, and a bad match would then corrupt the explanation
    rather than merely adding a redundant link.
    """
    body = client.get("/config/policy").text

    assert 'href="/glossary#correlation"' in body
    assert 'href="/glossary#basis-points"' in body


def test_term_matching_prefers_the_longer_phrase() -> None:
    """`effective sample size` must not be reported as `position` because both contain letters.

    Longest-first ordering is what stops a compound term being shadowed by a fragment of itself.
    """
    found = terms_in("the effective sample size of a position")

    assert found[0] == "effective sample size"
    assert "position" in found


def test_matching_never_rewrites_the_text() -> None:
    """`terms_in` decides which links to offer, and nothing else.

    A false positive costs a redundant link. If this function edited the description instead, a
    false positive would corrupt an explanation an operator is relying on.
    """
    original = "Correlation measures whether two investments move together."

    assert terms_in(original)
    assert original == "Correlation measures whether two investments move together."


def test_anchors_are_stable_and_unique() -> None:
    """A link that stops resolving is worse than no link: it reads as a broken promise."""
    anchors = [anchor(entry.term) for entry in GLOSSARY]

    assert len(anchors) == len(set(anchors))
    assert all(" " not in item and item.islower() for item in anchors)


def test_every_entry_is_reachable_from_the_contents(client: TestClient) -> None:
    """The page is long. A list nobody can navigate is a list nobody reads."""
    body = client.get("/glossary").text

    for entry in GLOSSARY:
        assert f'href="#{anchor(entry.term)}"' in body, f"{entry.term} is not in the contents"
        assert f'id="{anchor(entry.term)}"' in body, f"{entry.term} has no anchor"


def test_the_glossary_does_not_give_advice(client: TestClient) -> None:
    """It says what words mean, not what to do about them.

    Whether a threshold is set correctly is a question about evidence, and this system does not
    yet have enough forward evidence to answer it. A glossary that implied otherwise would be
    the most useful-looking wrong thing on the site.
    """
    # Whitespace-collapsed: the template wraps prose across lines, so asserting on a phrase
    # that spans a line break tests the line wrapping rather than the text.
    body = " ".join(client.get("/glossary").text.split())

    assert "does not yet have enough forward evidence" in body
    for entry in GLOSSARY:
        combined = f"{entry.short} {entry.long}".lower()
        assert "you should set" not in combined, entry.term
        assert "we recommend" not in combined, entry.term


def test_the_definitions_describe_this_system_rather_than_a_textbook() -> None:
    """ "Paper trading" has a general meaning and a particular one here.

    The particular one -- orders reaching a real broker's simulated account, with real prices
    and real rejections -- is what an operator needs when reading this system's output.
    """
    paper = next(entry for entry in GLOSSARY if entry.term == "paper trading")
    kill = next(entry for entry in GLOSSARY if entry.term == "kill switch")

    assert "real broker" in f"{paper.short} {paper.long}"
    assert "not a setting this system can change" in paper.long
    assert "nothing to do with real capital" in kill.long
