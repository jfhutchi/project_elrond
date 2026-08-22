"""What this project already knows, in the form the gates can read (#6).

`window_consumption`'s docstring says it exists because "a constraint that only exists in prose
is one an agent may or may not read". On 2026-08-22 that constraint still only existed in prose:
the live registry held **no research records, no registered hypotheses, no recorded trials and
no claimed windows**, while `REFUTED.md` recorded twenty-four settled hypotheses and an
exhausted equity window.

The consequence was live rather than theoretical. A new hypothesis asking whether momentum sorts
ETFs would have passed the novelty gate -- because memory had nothing to recall -- against a
question already answered as REFUTED #22 with `t=0.05`. The mechanism built to stop exactly that
was reading an empty table.

So the findings are curated here as data rather than parsed out of the markdown. Parsing prose
is the wrong shape for this: a parser that silently matched nothing would reproduce the empty
ledger it is meant to fix, and the failure would look identical to success. `tests` cross-checks
the count against `REFUTED.md` instead, so adding a row there without adding it here fails
loudly.

**These are records, not registrations.** Seeding memory makes the novelty gate work. It does
not reconstruct the trial burden or the consumed windows, which attach to registered hypotheses
and would require inventing registrations that never existed in this schema -- fabricating the
provenance of work done under a different system. Those two gaps are real, and named in
`REFUTED.md` and on #2 rather than papered over here.
"""

# This module is a data table of evidence sentences copied from REFUTED.md. Wrapping them
# across implicit concatenations would make the table unreadable and its diffs unreviewable,
# which defeats the purpose of curating them by hand.
# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, datetime

from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict

#: One entry per row of the `Refuted with measurement` table in `REFUTED.md`, keyed by the same
#: number so the two can be compared by eye. `subject` carries the words a later question is
#: likely to contain, because `ResearchMemory.recall` matches substrings rather than embeddings.
#: When these entered *this* store, fixed rather than wall-clock. A moving `recorded_at` makes
#: every re-seed differ from the stored record, and `ResearchMemory.record` correctly refuses a
#: conflicting write -- so the second `research-cycle` of the day would crash. Found by running
#: the CLI twice; the unit test had pinned its own clock and hidden it.
#:
#: This is not the measurement date. Those are in REFUTED.md, which `source` points at.
SEEDED_AT = datetime(2026, 8, 22, tzinfo=UTC)

PRIOR_FINDINGS: tuple[tuple[int, str, str, Verdict], ...] = (
    (
        1,
        "momentum parameter tuning",
        "31 candidates over 10.6y; the best failed parameter robustness -- the edge existed only at momentum_long=252 exactly",
        Verdict.REFUTED,
    ),
    (
        2,
        "the full shipped strategy",
        "Ranks 6 of 6. CAGR 0.50%, Sharpe 0.15, $105.41 from $100 over 10.6y against SPY's $454.70",
        Verdict.REFUTED,
    ),
    (
        3,
        "signal-family ensembles",
        "Component signals correlate 0.75+; no diversification is available to harvest",
        Verdict.REFUTED,
    ),
    (
        4,
        "exposure normalisation",
        "Sharpe is scale-invariant; the ensemble's low drawdown was under-risk, not skill",
        Verdict.REFUTED,
    ),
    (5, "crypto momentum", "-79.9%, Sharpe -0.45, 80.6% drawdown", Verdict.REFUTED),
    (
        6,
        "BTC trend standalone",
        "Sharpe 0.53 with a 95% CI of [-0.55, 1.61] spanning zero",
        Verdict.REFUTED,
    ),
    (
        7,
        "BTC as a portfolio diversifier",
        "Correlation is genuinely low (+0.152) and the blend improvement is 0.23 sigma, p~0.82, inside the luck band for 7 weight trials",
        Verdict.REFUTED,
    ),
    (
        8,
        "shorter horizons and 1-day reversal",
        "The strongest raw edge measured (+427.9% gross) nets -28.8% at 5bps, and never beats the slow signal at any cost level",
        Verdict.UNECONOMIC,
    ),
    (
        9,
        "options as a route to faster gains",
        "Affordable contracts at $100 are 0DTE at 10-18% spreads; the 5bps that killed the reversal applies 200-370x harder",
        Verdict.UNECONOMIC,
    ),
    (
        10,
        "machine learning and meta-labelling",
        "Looked strong (PF 2.29 v 1.06) and failed: n=39, t=1.72, p=0.085, CI spans zero against a 2.22 luck threshold for 43 cumulative trials",
        Verdict.REFUTED,
    ),
    (
        11,
        "energy predictability via geopolitics",
        "Energy trend persistence 21.2d is shorter than equities' 26.5d. -22.0% return, 78.3% drawdown; geopolitical shocks mean-revert",
        Verdict.REFUTED,
    ),
    (
        12,
        "FX diversification",
        "Lowest correlation in the study (0.02) and unusable -- Sharpe -0.14. A losing stream cannot diversify",
        Verdict.REFUTED,
    ),
    (
        13,
        "Reddit and social sentiment",
        "Long-minus-short WSB portfolios produce alpha indistinguishable from zero, with higher return and worse Sharpe",
        Verdict.LITERATURE_REFUTED,
    ),
    (
        14,
        "Coinbase for paper trading",
        "Advanced Trade sandbox returns static fixtures with no P&L; Coinbase for Agents is real money only, with no paper or testnet",
        Verdict.IMPOSSIBLE,
    ),
    (
        15,
        "cross-sectional breadth and whole-market momentum",
        "Survivorship-free over 511 names including 241 delisted: Sharpe 0.63 v SPY 0.80, -0.54 sigma, p~0.59",
        Verdict.REFUTED,
    ),
    (
        16,
        "volatility targeting as an alpha source",
        "Looked like the best result yet and died on the correct test: Jobson-Korkie-Memmel z=1.01, p=0.31, winning 6 of 12 assets",
        Verdict.REFUTED,
    ),
    (
        17,
        "the overnight close-to-open premium",
        "An unpaired test inflated it; corrected to paired, SPY falls from t=2.74 to 0.63 and 1 of 18 assets clears the bar",
        Verdict.REFUTED,
    ),
    (
        18,
        "the turn-of-month effect",
        "Positive on 4 of 7 assets, largest t=1.57 against a 2.82 bar",
        Verdict.REFUTED,
    ),
    (
        19,
        "beating SPY at growth-optimal leverage",
        "Stationary bootstrap 95% CI on the gap is [-$173.90, +$432.04], spanning zero and losing in 22.8% of resamples",
        Verdict.REFUTED,
    ),
    (
        20,
        "per-position vol targeting for wealth",
        "At matched exposure the bootstrap 95% CI is [-$86.76, +$76.79], losing in 36.8% of resamples",
        Verdict.REFUTED,
    ),
    (
        21,
        "weight-ramping as a substitute for tranching",
        "Ramping over 5 or 10 sessions changed dispersion by -9% and -6%, slightly worse than jumping",
        Verdict.REFUTED,
    ),
    (
        22,
        "whether the momentum ranking carries information at all",
        "Alpha against SPY is 0.10%/yr, t=0.05. Top-decile minus bottom-decile is t=0.77. SPY held at 0.71x reproduces the rotation to within 0.05 CAGR points with no trading",
        Verdict.REFUTED,
    ),
    (
        23,
        "momentum sorts on individual stocks",
        "Survivorship-free over 595 names: the ranking sorts backwards, and the spread vanishes exactly at a $20 price floor (t=0.00) -- it was a low-price artefact",
        Verdict.REFUTED,
    ),
    (
        24,
        "momentum sorts on global country indices",
        "22 indices 2016-2026. Top third 6.54% CAGR against bottom third 8.96% -- backwards again. Top minus bottom -2.47%/yr, t=-0.78 against a 2.90 bar",
        Verdict.REFUTED,
    ),
    (
        25,
        "zero-shot Kronos-small beating a naive baseline",
        "42 non-overlapping 5-day windows, 30 scored. Direction 0.501. On absolute error it loses to predicting zero: t=-2.53 against a 1.79 luck bar, worse in 21 of 30 windows",
        Verdict.REFUTED,
    ),
)


def prior_records() -> tuple[ResearchRecord, ...]:
    """The settled findings as records the novelty gate can recall.

    Byte-identical on every call, which is what makes seeding safe to repeat.
    """
    return tuple(
        ResearchRecord(
            record_id=f"refuted-{number:03d}",
            kind=RecordKind.FINDING,
            subject=subject,
            statement=statement,
            verdict=verdict,
            # Curated: every one of these was written up by a person from a measurement, and
            # `REFUTED.md` is the evidence a reader should go to rather than this file.
            curated=True,
            source="REFUTED.md",
            recorded_at=SEEDED_AT,
        )
        for number, subject, statement, verdict in PRIOR_FINDINGS
    )


def load_prior_findings(memory: ResearchMemory) -> int:
    """Write the settled findings into memory, returning how many were new.

    Idempotent through `ResearchMemory.record`, which accepts an exact repeat and refuses a
    conflicting one. So running this twice is harmless, and running it after someone edited a
    statement fails rather than silently overwriting the record a decision was based on.
    """
    return sum(1 for record in prior_records() if memory.record(record))


__all__ = ["PRIOR_FINDINGS", "SEEDED_AT", "load_prior_findings", "prior_records"]
