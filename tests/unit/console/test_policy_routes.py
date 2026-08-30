"""Changing a threshold from a browser, and every way that surface could lie (#82 slice 6).

The unit tests for `control.py` prove the state machine is right. These prove the *routes* honour
it, which is a different claim: a form can drop a field, a redirect can hide a refusal, and a
diff can be rendered from something other than what would actually be applied. Each of those
would leave the suite green while the console misled the person using it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.risk.policy_versions import active_policy_version
from quantbot.storage import Database, StorageRepository


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "console.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(database: Database) -> TestClient:
    return TestClient(create_app(database))


def versions(database: Database) -> list[dict[str, object]]:
    with database.transaction() as session:
        return [dict(row) for row in StorageRepository(session).list_policy_versions()]


def head_identity(database: Database) -> str:
    with database.transaction() as session:
        version, _ = active_policy_version(
            StorageRepository(session), now=datetime.now(UTC)
        )
    return version.identity


# --- preview writes nothing ---------------------------------------------------------------


def test_a_preview_shows_the_diff_and_changes_nothing(
    client: TestClient, database: Database
) -> None:
    """Validate-without-applying is what separates a control plane from a form that does things.

    If preview wrote, the operator would have already changed the rule by the time they read
    what the change was.
    """
    before = versions(database)
    response = client.post(
        "/config/policy/preview",
        data={"correlation_threshold": "0.85", "reason": "wider clusters"},
    )

    assert response.status_code == 200
    assert "Proposed change" in response.text
    assert "0.85" in response.text and "0.90" in response.text
    assert versions(database) == before, "a preview wrote to the history"


def test_a_preview_of_an_invalid_value_refuses_and_says_why(client: TestClient) -> None:
    """The policy model is the validator, so the browser cannot submit past it."""
    response = client.post(
        "/config/policy/preview",
        data={"correlation_threshold": "5.0", "reason": "out of range"},
    )

    assert response.status_code == 200
    assert "Refused" in response.text
    assert "Proposed change" not in response.text


def test_a_blank_field_is_left_alone_rather_than_set_to_empty(client: TestClient) -> None:
    """Every field is rendered in the form, so a partial edit posts mostly-empty inputs.

    Treating those as proposed values would refuse every real edit with a complaint about the
    fields nobody touched -- and, worse, a validator that accepted them would blank a threshold.
    """
    response = client.post(
        "/config/policy/preview",
        data={
            "correlation_threshold": "0.85",
            "max_daily_loss_bps": "",
            "reason": "one field only",
        },
    )

    assert "Proposed change" in response.text
    assert "max_daily_loss_bps" not in response.text.split("Proposed change")[1].split(
        "Propose a change"
    )[0]


# --- apply --------------------------------------------------------------------------------


def test_applying_mints_a_successor_and_leaves_the_predecessor_intact(
    client: TestClient, database: Database
) -> None:
    """The core guarantee, through the route rather than the function."""
    first = head_identity(database)
    response = client.post(
        "/config/policy/apply",
        data={
            "base_identity": first,
            "correlation_threshold": "0.85",
            "reason": "wider clusters after re-measuring",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    history = versions(database)
    assert [row["version"] for row in history] == [1, 2]
    assert history[1]["supersedes"] == first
    assert '"correlation_threshold":"0.90"' in str(history[0]["body"]).replace(", ", ",").replace(
        '": "', '":"'
    ), "version 1 still holds the value it was applied under"


def test_an_apply_is_recorded_as_a_project_default_not_an_operator_choice(
    client: TestClient, database: Database
) -> None:
    """The console cannot tell a person from a script posting to the same local port.

    Recording OPERATOR would put a human's name on a threshold no human may have chosen, which
    is the provenance laundering #72 forbids. Weigh the two errors: a false PROJECT_DEFAULT only
    understates authority Hutch can assert separately, while a false OPERATOR is unrecoverable
    -- nothing downstream can tell it from the real thing.
    """
    client.post(
        "/config/policy/apply",
        data={
            "base_identity": head_identity(database),
            "correlation_threshold": "0.85",
            "reason": "wider clusters",
        },
        follow_redirects=False,
    )

    assert versions(database)[1]["authority"] == "PROJECT_DEFAULT"
    assert "OPERATOR" not in {row["authority"] for row in versions(database)}


def test_applying_against_a_stale_version_is_refused_rather_than_merged(
    client: TestClient, database: Database
) -> None:
    """Two open tabs are the normal case. The second must not apply on top of a change its
    author never saw.
    """
    stale = head_identity(database)
    client.post(
        "/config/policy/apply",
        data={"base_identity": stale, "correlation_threshold": "0.85", "reason": "first"},
        follow_redirects=False,
    )

    response = client.post(
        "/config/policy/apply",
        data={"base_identity": stale, "max_daily_loss_bps": "400", "reason": "second"},
        follow_redirects=False,
    )

    assert response.status_code == 200, "refused in place, not redirected as if it worked"
    assert "never saw" in response.text
    assert len(versions(database)) == 2, "the stale apply did not append"


def test_a_refusal_is_shown_rather_than_redirected_away(client: TestClient) -> None:
    """A redirect to a page showing the unchanged rule would look identical to success."""
    response = client.post(
        "/config/policy/apply",
        data={"base_identity": "veto-policy-does-not-exist", "correlation_threshold": "0.85"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "Refused" in response.text


# --- rollback -----------------------------------------------------------------------------


def test_a_rollback_moves_forward_and_keeps_the_versions_it_reverts(
    client: TestClient, database: Database
) -> None:
    """Rewinding would delete the record that the intervening version was ever in force, and
    every refusal recorded under it would become unattributable.
    """
    first = head_identity(database)
    client.post(
        "/config/policy/apply",
        data={"base_identity": first, "correlation_threshold": "0.85", "reason": "wider"},
        follow_redirects=False,
    )

    response = client.post(
        "/config/policy/rollback",
        data={"target_identity": first, "reason": "reverting"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    history = versions(database)
    assert [row["version"] for row in history] == [1, 2, 3], "forward, not backward"
    assert history[2]["supersedes"] == history[1]["identity"]
    assert history[0]["body"] == history[2]["body"], "the rule is restored"
    assert history[0]["identity"] != history[2]["identity"], "the version is new"


def test_rolling_back_to_a_version_that_was_never_recorded_is_refused(
    client: TestClient, database: Database
) -> None:
    """The history cannot restore something it does not contain, and must say so rather than
    silently doing nothing -- which from the browser is indistinguishable from success.
    """
    response = client.post(
        "/config/policy/rollback",
        data={"target_identity": "veto-policy-invented", "reason": "nope"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "does not contain" in response.text
    assert len(versions(database)) == 1


# --- what the page says -------------------------------------------------------------------


def test_the_page_reports_the_rule_in_force_after_a_change(
    client: TestClient, database: Database
) -> None:
    """A console describing a rule the runtime is not running is worse than one showing nothing."""
    client.post(
        "/config/policy/apply",
        data={
            "base_identity": head_identity(database),
            "correlation_threshold": "0.85",
            "reason": "wider clusters",
        },
        follow_redirects=False,
    )

    page = client.get("/config/policy").text

    assert versions(database)[1]["identity"] in page, "the fingerprint is still traceable"
    assert "Version 2" in page, "and the human-readable version leads"


def test_rendering_the_page_writes_nothing(client: TestClient, database: Database) -> None:
    """A GET that appends to the history is the wrong shape, and this one nearly did.

    The first version resolved the head with `active_policy_version`, which records the shipped
    defaults as version 1 when the store is empty. Correct for the runtime -- it genuinely is
    running under them -- and wrong for a page: rendering a form is not a change, and a preview
    that wrote would have changed the rule before the operator finished reading what the change
    was. The runtime writes version 1 when it starts; an apply from here writes it first.
    """
    assert versions(database) == []

    page = client.get("/config/policy").text

    assert versions(database) == [], "rendering the page appended a version"
    assert "Not yet written down" in page, "and it says so rather than implying a recorded rule"


def test_an_apply_records_the_shipped_defaults_before_superseding_them(
    client: TestClient, database: Database
) -> None:
    """History starts where the system started, even if the console is the first thing to write.

    Without this the chain would begin at version 2 with a `supersedes` naming a row that is not
    there, and every refusal recorded before the edit would cite an identity the history cannot
    explain.
    """
    client.post(
        "/config/policy/apply",
        data={
            "base_identity": head_identity(database),
            "correlation_threshold": "0.85",
            "reason": "wider clusters",
        },
        follow_redirects=False,
    )

    history = versions(database)
    assert [row["version"] for row in history] == [1, 2]
    assert history[0]["supersedes"] is None
    assert history[0]["applied_by"] == "project-default", "not attributed to the console"


def test_every_threshold_input_is_labelled(client: TestClient) -> None:
    """Eight boxes that all announce as "unchanged" are eight boxes nobody can tell apart.

    Found by reading the rendered page's accessibility tree, where every input showed the same
    name. A sighted operator reads the adjacent cell; anyone using a screen reader gets a form
    with no way to know which threshold they are about to change -- on the one surface in this
    console that changes a risk control.
    """
    from quantbot.console.control import proposable_fields

    body = client.get("/config/policy").text

    for name in proposable_fields():
        assert f'<label for="field-{name}">' in body, f"{name} has no label"
        assert f'id="field-{name}"' in body
        assert f'aria-describedby="in-force-{name}"' in body, (
            f"{name} does not point at the value it is replacing"
        )


def test_each_setting_shows_a_readable_name_and_keeps_its_machine_key(
    client: TestClient,
) -> None:
    """An operator should not have to know that `max_daily_loss_bps` is the daily loss limit.

    The machine key stays visible beside it, deliberately: it is what the API, the audit trail
    and every durable refusal record use, so hiding it would leave a reader unable to connect
    what they changed here with what they read anywhere else.
    """
    body = client.get("/config/policy").text

    assert "Daily loss limit" in body
    assert "max_daily_loss_bps" in body
    assert "Correlation cluster threshold" in body


def test_each_setting_explains_what_it_does(client: TestClient) -> None:
    """A number an operator cannot interpret is one they will either not touch or touch wrongly.

    The text comes from the policy model itself, so it travels in `model_json_schema()` and the
    OpenAPI document too -- one place to update rather than a template copy that drifts.
    """
    body = client.get("/config/policy").text

    assert "no new positions are opened for the rest of the day" in body
    assert "<details" in body, "disclosed on demand rather than filling the table"
    assert "&#9432;" in body


def test_the_description_is_read_from_the_model_not_the_template() -> None:
    """The property that keeps the two from drifting."""
    from quantbot.console.control import field_documentation
    from quantbot.risk.vetoes import PortfolioVetoPolicy

    documented = field_documentation()

    for key, (title, description) in documented.items():
        info = PortfolioVetoPolicy.model_fields[key]
        assert title == info.title
        assert description == info.description
    assert all(title != key for key, (title, _) in documented.items()), (
        "every proposable setting has a human-readable name"
    )


def test_documenting_a_setting_does_not_mint_a_new_policy_version() -> None:
    """Titles and descriptions are metadata, not policy.

    If they entered the identity, writing documentation would re-version the policy and every
    refusal recorded beforehand would cite a rule that no longer resolves -- documentation would
    become a breaking change, which is a good way to guarantee none gets written.
    """
    from quantbot.risk.vetoes import PortfolioVetoPolicy

    assert PortfolioVetoPolicy().identity == "veto-policy-v1-42458cc40925c9ae"


def test_no_description_loses_a_word_boundary() -> None:
    """The first version of these strings was chunked every 88 characters, which split words and
    dropped the space at each boundary: "At 0.90 themeasured clusters", "most ofthe book".

    Caught by reading the rendered page, not by a test -- the second console defect that day
    which only appeared when something actually looked at it.

    Checked at the *source*, against implicit string concatenation, because that is the
    mechanism that fails. My first attempt at this test pattern-matched the prose for fused
    words and flagged "position", which contains "it" between two letters: a check that fires on
    correct text is one somebody deletes.
    """
    from quantbot.risk.vetoes import PortfolioVetoPolicy

    for name, info in PortfolioVetoPolicy.model_fields.items():
        if not info.description:
            continue
        assert "  " not in info.description, f"{name} has a doubled space"
        assert info.description == info.description.strip()
        # A lost boundary fuses two words into one token. Real English tops out well below this;
        # a fused pair like "themeasured" or "distinguishinga" clears it only when both halves
        # are long, so the bound is set where a genuine word cannot reach but a fusion can.
        longest = max(info.description.split(), key=len)
        assert len(longest) <= 16, (
            f"{name} contains {longest!r}, which is what a lost space looks like"
        )


def test_a_description_explains_the_idea_not_just_the_setting() -> None:
    """"How closely two symbols must move together" only helps somebody who already knows why
    that matters. The operator reading this is deciding whether to change the number, and that
    needs the mechanism rather than the definition.
    """
    from quantbot.risk.vetoes import PortfolioVetoPolicy

    correlation = PortfolioVetoPolicy.model_fields["correlation_threshold"].description or ""

    assert "move at the same time" in correlation, "the concept is explained"
    assert "not really two holdings" in correlation, "why it matters is explained"
    assert "0.85" in correlation, "what moving it costs is explained"
    assert len(correlation) > 300, "a one-line gloss is not an explanation"
