"""Point-in-time exposure classification, and the ways a concentration limit can be flattered.

Every test here is about the same asymmetry: an exposure that cannot be measured must not be
treated as an exposure of zero. Zero is not a conservative guess for a concentration numerator --
it is the most permissive value available.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.domain import Position
from quantbot.risk.exposure import (
    DEFAULT_CLASSIFICATION_PATH,
    ClassificationError,
    ExposureClassification,
    ExposureGroup,
    ExposureState,
    FactorModel,
    Provenance,
    build_exposure_state,
    load_exposure_classification,
)
from quantbot.risk.vetoes import (
    UNIMPLEMENTED,
    PolicyAuthority,
    PortfolioVetoInputs,
    PortfolioVetoPolicy,
    VetoName,
    build_veto_inputs,
    evaluate_portfolio_vetoes,
)

REPOSITORY = Path(__file__).resolve().parents[3]
VERIFIED = date(2026, 8, 27)
SESSION = date(2026, 8, 27)
OBSERVED = datetime(2026, 8, 27, 14, 30, tzinfo=UTC)


def table(**overrides: object) -> ExposureClassification:
    fields: dict[str, object] = {
        "version": 1,
        "source": "issuer fund descriptions",
        "effective_from": VERIFIED,
        "mandate": {
            "SPY": frozenset({ExposureGroup.US_EQUITY}),
            "XLK": frozenset({ExposureGroup.US_EQUITY, ExposureGroup.TECHNOLOGY}),
            "TLT": frozenset({ExposureGroup.TREASURY}),
        },
    }
    fields.update(overrides)
    return ExposureClassification.model_validate(fields)


def position(symbol: str, value: str) -> Position:
    """Market value is what concentration is measured on; price and quantity just have to agree."""
    amount = Decimal(value)
    return Position(
        symbol=symbol,
        quantity=amount / Decimal("100"),
        market_price=Decimal("100"),
        market_value=amount,
        average_entry_price=Decimal("100"),
    )


def policy(**overrides: object) -> PortfolioVetoPolicy:
    """One veto, one flat ceiling.

    The shipped policy gives `US_EQUITY` its own structural ceiling (#75), which is right in
    production and would mask the arithmetic under test here -- so these tests clear the
    per-group map and let every group share `max_group_exposure_bps`.
    """
    fields: dict[str, object] = {
        "enabled": frozenset({VetoName.EXPOSURE_CONCENTRATION}),
        "max_group_exposure_bps": 5000,
        "group_exposure_ceilings_bps": {},
    }
    fields.update(overrides)
    return PortfolioVetoPolicy(**fields)  # type: ignore[arg-type]


def inputs(**overrides: object) -> PortfolioVetoInputs:
    fields: dict[str, object] = {
        "symbol": "SPY",
        "equity": Decimal("10000"),
        "committed_net_exposure": Decimal("0"),
        "order_net_exposure": Decimal("1000"),
        "order_quantity": Decimal("2"),
        "session_date": SESSION,
        "observed_at": OBSERVED,
        "exposure": build_exposure_state(
            classification=table(), symbol="SPY", positions=[], as_of=SESSION
        ),
    }
    fields.update(overrides)
    return PortfolioVetoInputs(**fields)  # type: ignore[arg-type]


def test_default_classification_loads_from_external_service_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = load_exposure_classification(REPOSITORY / "config/exposure-classification-v1.yaml")
    monkeypatch.chdir(tmp_path)
    assert load_exposure_classification() == expected
    (tmp_path / "config").mkdir()
    (tmp_path / "config/exposure-classification-v1.yaml").write_text("invalid: policy")
    assert load_exposure_classification() == expected


def test_the_shipped_table_classifies_every_symbol_the_strategy_trades() -> None:
    """A universe symbol missing from the table blocks every order in it, silently in production.

    The veto would still be *correct* -- unknown blocks -- but the failure would present as "the
    risk layer refuses XLE" rather than "the table is incomplete", so it is worth an assertion.
    """
    import yaml

    loaded = load_exposure_classification(REPOSITORY / DEFAULT_CLASSIFICATION_PATH)
    universe = yaml.safe_load(
        (REPOSITORY / "config" / "strategy-v1-2.yaml").read_text(encoding="utf-8")
    )["universe"]

    missing = [symbol for symbol in universe if symbol not in loaded.mandate]
    assert not missing, f"unclassified universe symbols: {missing}"


def test_a_sector_only_reading_would_miss_this_portfolio_s_real_concentration() -> None:
    """The reason the veto is named for exposure rather than sector.

    SPY, QQQ, IWM and MDY share no sector. Under a sector table they are four uncorrelated bets;
    they are US equity beta wearing four tickers, and a concentration limit that cannot see that
    is a limit reporting itself as enforced while the risk walks through it.
    """
    loaded = load_exposure_classification(REPOSITORY / DEFAULT_CLASSIFICATION_PATH)

    def groups(symbol: str) -> frozenset[ExposureGroup]:
        found = loaded.groups_for(symbol, as_of=VERIFIED)
        assert found is not None, f"{symbol} is unclassified"
        return found

    for symbol in ("SPY", "QQQ", "IWM", "MDY"):
        assert ExposureGroup.US_EQUITY in groups(symbol), symbol
    besides_equity = [
        set(groups(symbol)) - {ExposureGroup.US_EQUITY} for symbol in ("SPY", "IWM", "MDY")
    ]
    assert not set.intersection(*besides_equity), (
        "these three share nothing but the exposure a sector table cannot see"
    )

    # And the sector SPDRs are US equity too: a roster of all nine is the index, not nine bets.
    assert ExposureGroup.US_EQUITY in groups("XLK")
    assert ExposureGroup.TECHNOLOGY in groups("XLK")


def test_a_classification_cannot_be_applied_before_it_was_verified() -> None:
    """The anti-look-ahead rule, and it binds even for ETFs.

    Fund mandates change -- constituents left XLK when Communication Services was created in
    2018 -- so a portfolio scored over that history against today's table would be judged on a
    composition that did not exist. Rather than research an inception date per fund and risk
    getting one wrong, the table refuses to speak about anything before it was checked.
    """
    known = table()

    assert known.groups_for("SPY", as_of=VERIFIED) == frozenset({ExposureGroup.US_EQUITY})
    assert known.groups_for("SPY", as_of=date(2026, 8, 26)) is None, "one day early is unknown"
    assert known.groups_for("SPY", as_of=date(2017, 1, 3)) is None

    # The shipped table speaks from the last sector reshuffle, not from the day it was written.
    shipped = load_exposure_classification(REPOSITORY / DEFAULT_CLASSIFICATION_PATH)
    assert shipped.effective_from == date(2018, 10, 1)
    assert shipped.groups_for("XLK", as_of=date(2018, 9, 28)) is None, (
        "before Communication Services split out, XLK held something else"
    )
    assert shipped.groups_for("XLK", as_of=date(2018, 10, 1)) is not None


def test_an_unlisted_symbol_is_unknown_and_unknown_refuses() -> None:
    """The fail-open shape this exists to prevent.

    An unclassified instrument belongs to no group, so it adds to no group's concentration and
    passes every limit by virtue of not being recognised. The more expensive version is a *held*
    position nobody could classify: every group total is then an understatement.
    """
    assert table().groups_for("NVDA", as_of=VERIFIED) is None

    # The order's own symbol is unknown. The state must *name* it, not merely leave its groups
    # empty: #57 studies which symbols could not be classified, and a rejection that says only
    # "something was unclassified" cannot be grouped on.
    unknown_order = build_exposure_state(
        classification=table(), symbol="NVDA", positions=[], as_of=SESSION
    )
    assert unknown_order.unclassified == ("NVDA",)

    refused = evaluate_portfolio_vetoes(
        inputs(symbol="NVDA", exposure=unknown_order), policy()
    )
    assert not refused.allowed
    assert refused.rejections[0].unmeasured == "UNCLASSIFIED_SYMBOL"
    assert "NVDA" in refused.rejections[0].reason

    # A held position is unknown, while the order's symbol is fine. This is the one that would
    # otherwise pass, because the order itself is perfectly classifiable.
    held_unknown = build_exposure_state(
        classification=table(),
        symbol="SPY",
        positions=[position("NVDA", "9000")],
        as_of=SESSION,
    )
    understated = evaluate_portfolio_vetoes(inputs(exposure=held_unknown), policy())
    assert not understated.allowed
    assert understated.rejections[0].unmeasured == "UNCLASSIFIED_SYMBOL"
    assert "understates" in understated.rejections[0].reason


def test_a_missing_table_refuses_rather_than_reporting_every_group_at_zero() -> None:
    """`None` is not an empty table, and an empty table is not a diversified portfolio."""
    decision = evaluate_portfolio_vetoes(inputs(exposure=None), policy())

    assert not decision.allowed
    assert decision.rejections[0].unmeasured == "exposure_classification"

    # And the loader refuses to invent one.
    with pytest.raises(ClassificationError, match="no exposure classification"):
        load_exposure_classification(Path("does-not-exist.yaml"))


def test_a_symbol_counts_in_every_group_it_carries() -> None:
    """The conservative direction: more ways to trip a limit, never fewer.

    XLK is US equity *and* technology. Holding SPY and buying XLK concentrates US equity even
    though the two share no sector, and a single-group table would not see it.
    """
    state = build_exposure_state(
        classification=table(),
        symbol="XLK",
        positions=[position("SPY", "4500"), position("XLK", "1000")],
        as_of=SESSION,
    )

    assert state.committed_by_group[ExposureGroup.US_EQUITY] == Decimal("5500")
    assert state.committed_by_group[ExposureGroup.TECHNOLOGY] == Decimal("1000")

    # 5500 committed + 1000 ordered = 6500 against a 5000 ceiling (5000bps of 10000 equity).
    decision = evaluate_portfolio_vetoes(inputs(symbol="XLK", exposure=state), policy())
    assert not decision.allowed
    assert "US_EQUITY (MANDATE) would reach 6500" in decision.rejections[0].reason
    assert decision.rejections[0].unmeasured is None, "a breached limit is not an unmeasured one"


def test_a_short_does_not_net_away_a_concentration() -> None:
    """Absolute value, not signed.

    Long SPY against short XLK is not an absence of US equity exposure; it is two positions whose
    shared exposure a netted total would report away.
    """
    state = build_exposure_state(
        classification=table(),
        symbol="SPY",
        positions=[position("SPY", "4000"), position("XLK", "-4000")],
        as_of=SESSION,
    )

    assert state.committed_by_group[ExposureGroup.US_EQUITY] == Decimal("8000")


def test_a_group_under_its_ceiling_is_allowed() -> None:
    """The veto must be capable of passing, or every test above proves only that it refuses."""
    state = build_exposure_state(
        classification=table(),
        symbol="SPY",
        positions=[position("TLT", "9000")],
        as_of=SESSION,
    )

    decision = evaluate_portfolio_vetoes(inputs(exposure=state), policy())

    assert decision.allowed, decision.explain()
    assert decision.evaluated == (VetoName.EXPOSURE_CONCENTRATION,)


def test_a_missing_reference_price_refuses_here_too() -> None:
    """Same rule as `NET_EXPOSURE`: a missing price is not a zero one.

    Zero would let a real order add nothing to its group while the veto still reported itself as
    evaluated.
    """
    decision = evaluate_portfolio_vetoes(inputs(order_net_exposure=None), policy())

    assert not decision.allowed
    assert decision.rejections[0].unmeasured == "REFERENCE_PRICE_UNAVAILABLE"


def test_the_veto_ships_enabled_with_a_ceiling_that_actually_binds() -> None:
    """#75 reversed the earlier default, and the reason it shipped disabled is worth keeping.

    It was off because threshold selection was called operator policy, with a 100%-of-equity
    placeholder that could never refuse anything. That left a tested control doing nothing while
    waiting for a preference nobody had been asked for.

    A single global ceiling genuinely cannot serve this universe -- too loose for the narrow
    groups if set for `US_EQUITY`, and refusing every order if set for them. Per-group ceilings
    are what made enabling it defensible rather than theatrical.
    """
    shipped = PortfolioVetoPolicy()

    assert VetoName.EXPOSURE_CONCENTRATION in shipped.enabled
    assert VetoName.EXPOSURE_CONCENTRATION not in UNIMPLEMENTED

    # The general ceiling binds: four of ten equal-weight holdings in one exposure.
    assert shipped.max_group_exposure_bps == 4000

    # `US_EQUITY` is structural for this strategy -- measured at ~5972bps of equity on the
    # deployed roster -- so a 4000bps limit would refuse nearly every order it wants to place.
    assert shipped.group_exposure_ceilings_bps[ExposureGroup.US_EQUITY] == 10000
    assert ExposureGroup.TECHNOLOGY not in shipped.group_exposure_ceilings_bps

    # And the numbers are the system's, stated as such rather than implied to be approved.
    assert shipped.authority is PolicyAuthority.PROJECT_DEFAULT


def test_a_structural_group_and_a_narrow_one_are_not_held_to_one_number() -> None:
    """The whole point of per-group ceilings, on the shipped policy rather than a fixture.

    The same book is fine on US equity and refused on technology, which a single threshold could
    not express in either direction.
    """
    shipped = PortfolioVetoPolicy()
    loaded = load_exposure_classification(REPOSITORY / DEFAULT_CLASSIFICATION_PATH)

    # 45% of equity already committed to a technology-carrying holding, buying more.
    state = build_exposure_state(
        classification=loaded,
        symbol="XLK",
        positions=[position("XLK", "4500")],
        as_of=SESSION,
    )
    decision = evaluate_portfolio_vetoes(
        inputs(symbol="XLK", exposure=state, order_net_exposure=Decimal("1000")), shipped
    )

    assert not decision.allowed
    # The shipped policy runs every veto and does not short-circuit, so the concentration
    # rejection is selected by name rather than by position.
    concentration = next(
        item
        for item in decision.rejections
        if item.veto is VetoName.EXPOSURE_CONCENTRATION
    )
    assert "TECHNOLOGY" in concentration.reason, concentration.reason
    assert "US_EQUITY" not in concentration.reason, (
        "the structural group has room and must not be reported as breached"
    )


def test_two_classification_versions_are_two_studies() -> None:
    """Sol's requirement on #57 and #59: a rejection must name the definition that produced it."""
    base = table()

    assert base.identity.startswith("exposure-classification-v1-")
    assert table(version=2).identity != base.identity
    assert table(source="somewhere else").identity != base.identity
    assert table(effective_from=date(2026, 1, 2)).identity != base.identity

    moved = table(
        mandate={**dict(base.mandate), "TLT": frozenset({ExposureGroup.CREDIT})}
    )
    assert moved.identity != base.identity, "regrouping a symbol is a new definition"

    # The state carries it, so a rejection can be traced to the table that produced it.
    state = build_exposure_state(classification=base, symbol="SPY", positions=[], as_of=SESSION)
    assert state.classification_identity == base.identity


def test_a_mandate_claim_and_a_stress_proxy_are_not_the_same_claim() -> None:
    """Sol's #59 follow-up: issuer mandate and empirical factor carry different evidence.

    HYG holds high-yield credit by mandate and behaves like equity in a drawdown. Both belong in
    a concentration limit, and recording them identically would let a declared stress assumption
    borrow the authority of a prospectus.
    """
    loaded = load_exposure_classification(REPOSITORY / DEFAULT_CLASSIFICATION_PATH)

    # The mandate says credit and only credit. HYG owns no equities.
    assert loaded.mandate["HYG"] == frozenset({ExposureGroup.CREDIT})

    # The equity exposure is still measured -- it is why the limit matters in a drawdown -- but
    # it is labelled as the weaker claim.
    provenance = loaded.provenance_for("HYG", as_of=VERIFIED)
    assert provenance is not None
    assert provenance[ExposureGroup.CREDIT] is Provenance.MANDATE
    assert provenance[ExposureGroup.US_EQUITY] is Provenance.FACTOR

    # LQD is rate-sensitive and owns no Treasuries, so duration is a factor and not a holding.
    assert ExposureGroup.TREASURY not in loaded.mandate["LQD"]
    lqd = loaded.provenance_for("LQD", as_of=VERIFIED)
    assert lqd is not None and lqd[ExposureGroup.DURATION_FACTOR] is Provenance.FACTOR


def test_provenance_is_a_property_of_the_assignment_not_of_the_group() -> None:
    """`US_EQUITY` is definitional for SPY and inferred for HYG. Same exposure, different evidence.

    Attaching provenance to the group instead would force one label onto both and lose exactly
    the distinction this split exists to keep.
    """
    loaded = load_exposure_classification(REPOSITORY / DEFAULT_CLASSIFICATION_PATH)

    spy = loaded.provenance_for("SPY", as_of=VERIFIED)
    hyg = loaded.provenance_for("HYG", as_of=VERIFIED)
    assert spy is not None and hyg is not None
    assert spy[ExposureGroup.US_EQUITY] is Provenance.MANDATE
    assert hyg[ExposureGroup.US_EQUITY] is Provenance.FACTOR

    # And when both halves claim the same exposure, the stronger evidence wins the label.
    # Reporting a definitional exposure as empirically inferred understates what is known, and
    # would let a study discount a refusal that rested on a prospectus.
    doubled = table(
        factor_model=FactorModel(
            method="declared stress proxy",
            as_of=VERIFIED,
            version=1,
            assignments={"SPY": frozenset({ExposureGroup.US_EQUITY})},
        )
    )
    both = doubled.provenance_for("SPY", as_of=VERIFIED)
    assert both is not None
    assert both[ExposureGroup.US_EQUITY] is Provenance.MANDATE


def test_a_refusal_says_which_mechanism_it_acted_on() -> None:
    """#57 compares refusals, and two refusals under different mechanisms are not one dataset."""
    factored = table(
        factor_model=FactorModel(
            method="declared stress proxy",
            as_of=VERIFIED,
            version=1,
            assignments={"TLT": frozenset({ExposureGroup.US_EQUITY})},
        )
    )
    state = build_exposure_state(
        classification=factored,
        symbol="TLT",
        positions=[position("SPY", "4500")],
        as_of=SESSION,
    )

    decision = evaluate_portfolio_vetoes(inputs(symbol="TLT", exposure=state), policy())

    assert not decision.allowed
    assert "US_EQUITY (FACTOR)" in decision.rejections[0].reason, decision.rejections[0].reason


def test_the_factor_model_is_versioned_apart_from_the_mandate() -> None:
    """A stress assumption changing is a new definition, and must not be invisible.

    The mandate half can be stable while the factor half moves; if only the mandate fed the
    identity, two studies under different stress assumptions would pool into one.
    """
    base = table()
    assert base.factor_model is None

    factored = table(
        factor_model=FactorModel(
            method="declared stress proxy",
            as_of=VERIFIED,
            version=1,
            assignments={"TLT": frozenset({ExposureGroup.US_EQUITY})},
        )
    )
    assert factored.identity != base.identity, "adding a stress model is a new definition"

    reassigned = table(
        factor_model=FactorModel(
            method="declared stress proxy",
            as_of=VERIFIED,
            version=2,
            assignments={"TLT": frozenset({ExposureGroup.CREDIT})},
        )
    )
    assert reassigned.identity != factored.identity
    assert reassigned.factor_model is not None
    assert reassigned.factor_model.identity.startswith("factor-model-v2-")


def test_an_instrument_cannot_be_classified_on_stress_behaviour_alone() -> None:
    """A factor row with no mandate row would classify a symbol entirely on the weaker claim.

    That is the direction that must not happen quietly: the instrument would appear classified,
    and everything about it would rest on an assumption nobody registered as a mandate.
    """
    with pytest.raises(ValueError, match="cannot be classified on stress behaviour alone"):
        table(
            factor_model=FactorModel(
                method="declared stress proxy",
                as_of=VERIFIED,
                version=1,
                assignments={"NVDA": frozenset({ExposureGroup.US_EQUITY})},
            )
        )


def test_a_symbol_classified_into_nothing_is_refused_at_load() -> None:
    """An empty group set reads as classified and behaves as unclassified.

    That is the fail-open shape wearing a "we checked this one" label, so the table refuses it
    and the symbol must be omitted instead -- where it classifies as unknown and blocks.
    """
    with pytest.raises(ValueError, match="omit it so it classifies as unknown"):
        table(mandate={"SPY": frozenset()})


def test_the_input_builder_leaves_exposure_absent_when_no_table_is_supplied() -> None:
    """A caller with no classification gets `None`, not an empty one, and the veto then refuses."""
    without = build_veto_inputs(
        symbol="SPY",
        equity=Decimal("10000"),
        positions=[],
        order_quantity=Decimal("2"),
        order_price=Decimal("500"),
        equity_marks=[],
        symbol_bars=[],
        observed_at=OBSERVED,
        volume_sessions=20,
    )
    assert without.exposure is None

    with_table = build_veto_inputs(
        symbol="SPY",
        equity=Decimal("10000"),
        positions=[position("XLK", "1000")],
        order_quantity=Decimal("2"),
        order_price=Decimal("500"),
        equity_marks=[],
        symbol_bars=[],
        observed_at=OBSERVED,
        volume_sessions=20,
        classification=table(),
    )
    assert with_table.exposure is not None
    assert with_table.exposure.committed_by_group[ExposureGroup.US_EQUITY] == Decimal("1000")


def test_the_state_is_a_measurement_and_not_an_assertion() -> None:
    """A caller could hand-build an `ExposureState`; the veto still refuses an unknown order.

    The guard is separate from `unclassified` on purpose: the builder populates both together,
    and a directly-constructed state must not be able to claim a classified order while carrying
    no groups for it.
    """
    asserted = ExposureState(
        classification_identity="exposure-classification-v1-deadbeefdeadbeef",
        order_groups=None,
        committed_by_group={},
        unclassified=(),
    )

    decision = evaluate_portfolio_vetoes(inputs(exposure=asserted), policy())

    assert not decision.allowed
    assert decision.rejections[0].unmeasured == "UNCLASSIFIED_SYMBOL"
