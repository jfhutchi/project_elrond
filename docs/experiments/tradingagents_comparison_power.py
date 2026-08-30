"""Can #37's comparison be powered at all, and at what cost? (#37, #19)

Sol's plan: feed identical frozen event packets to (a) the native research arm and (b)
TradingAgents, then compare downstream hypothesis quality, survival and information gain under
identical Elrond gates.

That is a comparison of two proportions -- the share of packets from each arm producing a
hypothesis that survives the gates. CLAUDE.md is explicit that the power question gets answered
*before* the compute is spent, and this answers it.

Three inputs the design needs and does not have:

1. **The base survival rate.** Nothing has ever survived Elrond's gates; `rejected_candidates` is
   empty and no experiment has run. So `p_native` is not merely unmeasured, it is unobserved.
2. **The effect worth detecting.** "TradingAgents adds protected downstream value" is not a
   number. Something has to say how much better is worth a dependency.
3. **Within-arm variance.** Measured for the native arm (#122): deterministic within a session,
   26-37% claim divergence across session positions. TradingAgents' own is larger -- `REFUTED.md`
   records two identical invocations returning `Hold` then `Buy`.

Points 1 and 2 make this a planning calculation over assumed values rather than a measurement,
and that is exactly what it should be: the output is how many packets each arm would need, so
somebody can decide whether that is affordable before paying for it.

Run: `uv run python docs/experiments/tradingagents_comparison_power.py`
"""

from __future__ import annotations

import math

#: Two-sided 5%, and 80% power. The conventional pair, stated rather than assumed.
Z_ALPHA = 1.959964
Z_BETA = 0.841621

#: Roughly what one TradingAgents run cost when it was last exercised (#REFUTED, 2026-08-22):
#: a multi-agent debate against a local 7B model. Minutes, not seconds, and both arms have to be
#: repeated because neither is reproducible from its recorded provenance alone.
MINUTES_PER_TRADINGAGENTS_RUN = 6.0
MINUTES_PER_NATIVE_RUN = 0.2

#: Repeats per packet per arm, to average out the within-arm variance #122 measured. Without
#: repeats a single run's session position is confounded with the arm, and the comparison
#: measures the cache rather than the framework.
REPEATS = 3


def packets_per_arm(baseline: float, lift: float) -> int:
    """Packets each arm needs to detect `lift` over `baseline`, at 5% two-sided and 80% power.

    Standard two-proportion sample size. Deliberately the textbook formula rather than anything
    clever: the uncertainty here is overwhelmingly in the assumed rates, not in the arithmetic,
    and a more elaborate calculation would dress up a guess.
    """
    treated = min(baseline + lift, 0.999)
    pooled = (baseline + treated) / 2
    numerator = (
        Z_ALPHA * math.sqrt(2 * pooled * (1 - pooled))
        + Z_BETA * math.sqrt(baseline * (1 - baseline) + treated * (1 - treated))
    ) ** 2
    return math.ceil(numerator / (treated - baseline) ** 2)


def main() -> int:
    print("#37 comparison: packets required per arm\n")
    print("Two-sided 5%, 80% power. Both arms repeated 3x per packet, because neither is")
    print(
        "reproducible from its provenance and a single run confounds session position "
        "with arm."
    )
    print()
    print(f"{'base rate':>10} {'+5pp':>9} {'+10pp':>9} {'+20pp':>9}")
    print("-" * 42)

    for baseline in (0.02, 0.05, 0.10, 0.20, 0.30):
        row = [f"{baseline:>10.0%}"]
        for lift in (0.05, 0.10, 0.20):
            row.append(f"{packets_per_arm(baseline, lift):>9,}")
        print(" ".join(row))

    print("\nWhat that costs, at 3 repeats per packet per arm:\n")
    print(f"{'packets':>10} {'TA hours':>11} {'native hours':>14}")
    print("-" * 40)
    for packets in (100, 250, 500, 1000):
        ta = packets * REPEATS * MINUTES_PER_TRADINGAGENTS_RUN / 60
        native = packets * REPEATS * MINUTES_PER_NATIVE_RUN / 60
        print(f"{packets:>10,} {ta:>11.1f} {native:>14.1f}")

    print(
        "\nThe base rate is the problem, not the arithmetic. Nothing has ever survived Elrond's\n"
        "gates -- `rejected_candidates` is empty and no experiment has run -- so every row above\n"
        "is a planning figure over an assumed rate rather than a measurement.\n"
        "\n"
        "And the honest reading of a low base rate is the one that costs most: at a 2% survival\n"
        "rate, detecting even a 5-point improvement needs hundreds of packets per arm, which is\n"
        "hundreds of hours of TradingAgents debate against a local model. That is not a reason to\n"
        "refuse the comparison. It is a reason to know the number before starting, and to fix the\n"
        "base rate first -- because until something survives, there is no denominator to improve."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
