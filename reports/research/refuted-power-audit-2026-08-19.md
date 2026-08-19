# Which refutations were answers, and which were silence

**Classification: `EXPLORATORY`.** This re-reads existing records; it measures nothing new and
consumes no statistical budget. Nothing in `REFUTED.md` is rewritten — the historical entries
stand exactly as recorded, and this is an addition beside them.

## Why

`REFUTED.md` labels 24 hypotheses `REFUTED`. #19 and #6 exist because **"we could not test
this" and "we tested this and it failed" are different facts**, and recording the first as the
second tells a later agent a mechanism was tried when it never was.

The distinction is enforced going forward. It was never applied to the corpus that predates it.
So: for each entry, did the study have the power to detect the effect it was looking for?

Only figures `REFUTED.md` itself reports are used. Where the file does not report enough to
compute power, the answer is "cannot determine" — not a guess.

## Demonstrably underpowered: the file's own confidence interval proves it

A 95% interval gives the standard error directly (`SE = width / 3.92`), and the minimum
detectable effect at the luck bar is `2.9 × SE`. Where the measured effect is smaller than that,
**the study could not have resolved the claim in either direction.**

| # | hypothesis | reported | implied SE | could only detect | measured |
|---|---|---|---:|---:|---:|
| 6 | BTC trend standalone | Sharpe 0.53, CI [−0.55, 1.61] | 0.551 | **1.60 Sharpe** | 0.53 |
| 10 | ML / meta-labelling | CI [−0.104%, +1.598%], n=39 | 0.434% | **1.26%** | ~0.75% |
| 19 | growth-optimal leverage beats SPY | gap CI [−$173.90, +$432.04] | $154.58 | **$448** | $129 |
| 20 | per-position vol targeting | gain $15.88, CI [−$86.76, +$76.79] | $41.72 | **$121** | $15.88 |

Four entries. In each the instrument was too blunt to see the thing being looked for, by a
factor of two to eight. **These are `UNDERPOWERED`, not `REFUTED`.** The mechanisms may work;
nothing was learned about them either way.

Entry 6 is the sharpest case. A Sharpe of 0.53 was measured with an instrument that could not
distinguish anything below 1.60 — and the file records the interval spanning zero as the reason
for refutation, when the interval spanning zero is what an underpowered study always produces.

## Stands, but on different evidence than the statistic quoted

**#22 — "the momentum ranking carries information at all"**, which `REFUTED.md` says "subsumes
most of the others."

Its statistics are underpowered. Alpha of 0.10%/yr at t=0.05 implies SE ≈ 2.00%/yr, so the test
could not have detected an alpha below **5.74%/yr**. Top-10-minus-all at 1.43%/yr and t=0.84
implies SE ≈ 1.70%/yr, detecting nothing below 4.89%/yr. Against a benchmark returning ~15%/yr
those are enormous thresholds; "no alpha" is not what was shown.

**But the entry carries a second, independent argument that does not depend on power at all:**

> the rotation is beta 0.71 with no alpha, and SPY held at 0.71x reproduces it to within
> **0.05 CAGR points** with no trading at all

That is an *equivalence demonstration*, not a null-hypothesis test. If a passive fractional
position reproduces the strategy to within 0.05 CAGR points, the strategy adds nothing of
consequence whatever the standard error is. The conclusion survives; the evidence for it is the
replication, not the t-statistic.

Worth separating explicitly, because the entry currently leads with the weak argument.

## Cannot determine, and why that is itself the finding

For the remaining entries the file reports a test statistic against a bar but no effect size and
no standard error, so power is not recoverable. What it shows is the shortfall:

| # | hypothesis | \|t\| | bar | fraction of the way |
|---|---|---:|---:|---:|
| 22 | momentum ranking carries information | 0.05 | 2.87 | 1.7% |
| 7 | BTC as a portfolio diversifier | 0.23 | 2.22 | 10.4% |
| 17 | overnight premium (corrected, paired) | 0.63 | 2.82 | 22.3% |
| 15 | cross-sectional breadth | 0.54 | 2.22 | 24.3% |
| 24 | momentum on global country indices | 0.78 | 2.90 | 26.9% |
| 16 | vol targeting as alpha | 1.01 | 2.90 | 34.8% |
| 18 | turn-of-month | 1.57 | 2.82 | 55.7% |
| 23 | momentum on single stocks | 2.08 | 2.89 | 72.0% |

A large shortfall is consistent with *either* a true null *or* an underpowered test, and
nothing here distinguishes them. **That ambiguity exists because no expected effect size was
pre-registered** — which is precisely the field #5 now requires and #19 now gates on. Every
future entry will be decidable; these are not.

## What this changes

**The corpus overstates how much has been ruled out.** At least four of twenty-four refutations
are silence recorded as an answer. BTC trend, per-position vol targeting, and meta-labelling are
not dead — they are untested.

**It does not follow that they should be re-run.** Re-testing them on the same window spends
trial budget that is already exhausted, and would produce the same silence. They become live
questions only if the data improves — and the power survey shows free macro history resolves
roughly twice as fine as the equity window, so that condition is reachable.

**It sharpens the reading of the whole record.** `REFUTED.md`'s own summary says every apparent
winner evaporated under a test. That remains true. What this adds is that a share of the tests
could not have shown anything, so the corpus is better read as *"nothing large was found"* than
as *"these mechanisms do not work"* — which is a materially weaker and more accurate claim.

## Method

Reproduced by `scripts/power_survey.py` for the dataset comparison, and by the arithmetic above
for the per-entry figures: `SE = (upper − lower) / 3.92`, `MDE = 2.9 × SE`. The 2.9 bar is the
project's own at 68 cumulative trials. Using each cycle's contemporaneous bar would move the
thresholds slightly and change no verdict, since the shortfalls are factors rather than margins.
