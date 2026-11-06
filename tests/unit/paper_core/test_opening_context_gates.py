from datetime import timedelta
from decimal import Decimal

import pytest
from tests.fixtures.core_m2 import NOW, prepare

from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.paper_core.contracts import ModelArtifact
from quantbot.paper_core.trader import evaluate_entry


@pytest.mark.parametrize(
    "mode, momentum, allowed",
    [("COLLECT", 0.002, True), ("COLLECT", -0.002, False), ("PREDICT", 0.002, False)],
)
def test_opening_context_requires_signal_and_never_uses_intraday_predictor(
    tmp_path, monkeypatch, mode, momentum, allowed
):
    db, store, _, proof, observation, _ = prepare(tmp_path)
    try:
        observation = observation.model_copy(
            update=dict(
                feature_context="OPENING_CONTIGUOUS_V1",
                feature_version="five-minute-price-alpha-v1",
                session_open=NOW,
                source_end=NOW,
                source_start=NOW - timedelta(minutes=35),
                features=tuple((name, momentum) for name in FEATURE_NAMES),
            )
        )
        model = ModelArtifact(feature_version="five-minute-price-alpha-v1").model_copy(
            update={"mode": mode}
        )

        def no_inference(*args):
            pytest.fail("Existing intraday predictor must never score opening-context rows")

        monkeypatch.setattr("quantbot.paper_core.learning.predict_bps", no_inference)
        decision = evaluate_entry(store.config, observation, model, proof)
        assert (decision.action == "BUY") is allowed, decision.gates
        assert decision.prediction_bps is None
        if mode == "PREDICT":
            assert "OPENING_CONTEXT_MODEL_UNSUPPORTED" in decision.gates
        elif not allowed:
            assert "OPENING_SIGNAL_NOT_QUALIFIED" in decision.gates
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,allowed",
    [
        ("qualified", True),
        ("negative_gap", False),
        ("wide_spread", False),
        ("missing_references", False),
        ("stale_quote", False),
        ("future_quote", False),
        ("altered_features", False),
        ("predict_model", False),
    ],
)
async def test_actual_opening_quote_evidence_requires_valid_qualified_signal(
    tmp_path, monkeypatch, case, allowed
):
    from tests.integration.paper_core.test_opening_adapter import adapter

    broker = adapter(tmp_path, 0, missing=True, gap=True)
    cohort = await broker._acquire_cohort(("XLE",), verify_assets=False)
    (observation,) = cohort.observations
    assert observation.feature_context == "OPENING_QUOTE_GAP_V1"
    db, store, _, proof, _, _ = prepare(tmp_path)
    try:
        # Align only synthetic portfolio certification with genuine adapter inputs.
        proof = proof.model_copy(update={"recorded_at_utc": observation.recorded_at_utc})
        asset, market = observation.opening_quote_sources
        if case in {"negative_gap", "wide_spread"}:
            bid, ask = (
                (Decimal("99.79"), Decimal("99.80"))
                if case == "negative_gap"
                else (Decimal("102"), Decimal("103"))
            )
            asset = asset.model_copy(update={"bid": bid, "ask": ask})
            gap = (bid + ask) / 2 / asset.previous_close - 1
            market_gap = (market.bid + market.ask) / 2 / market.previous_close - 1
            observation = observation.model_copy(
                update={
                    "price": ask,
                    "opening_quote_sources": (asset, market),
                    "features": (
                        ("gap_return", float(gap)),
                        ("market_gap_return", float(market_gap)),
                        ("relative_gap", float(gap - market_gap)),
                        ("spread_bps", float((ask - bid) / bid * 10000)),
                    ),
                }
            )
        elif case == "missing_references":
            observation = observation.model_copy(update={"opening_quote_sources": None})
        elif case in {"stale_quote", "future_quote"}:
            asset = asset.model_copy(
                update={
                    "quote_at": asset.quote_at
                    + timedelta(seconds=-6 if case == "stale_quote" else 1)
                }
            )
            observation = observation.model_copy(update={"opening_quote_sources": (asset, market)})
        elif case == "altered_features":
            observation = observation.model_copy(
                update={
                    "features": tuple((name, value + 0.01) for name, value in observation.features)
                }
            )
        model = ModelArtifact(feature_version="five-minute-price-alpha-v1").model_copy(
            update={"mode": "PREDICT" if case == "predict_model" else "COLLECT"}
        )

        def no_inference(*args):
            pytest.fail("Intraday predictor must never score opening quote observations")

        monkeypatch.setattr("quantbot.paper_core.learning.predict_bps", no_inference)
        decision = evaluate_entry(store.config, observation, model, proof)
        assert (decision.action == "BUY") is allowed, decision.gates
        assert decision.prediction_bps is None
        if case in {"negative_gap", "wide_spread"}:
            assert "OPENING_SIGNAL_NOT_QUALIFIED" in decision.gates
        if case == "predict_model":
            assert "OPENING_CONTEXT_MODEL_UNSUPPORTED" in decision.gates
    finally:
        db.close()
