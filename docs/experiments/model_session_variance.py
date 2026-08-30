"""Measure the native semantic arm's run-to-run variance on one frozen input.

Sol's #37 plan compares the native research baseline against TradingAgents on identical frozen
event packets, under identical Elrond gates. That comparison needs a number nobody has measured:
how much each arm's output varies when the input does not change at all.

TradingAgents' variance is already known to be large -- REFUTED.md records two identical
invocations returning `Hold` and then `Buy`. If the native arm also varies materially, a
single-run comparison cannot distinguish either arm's contribution from its own noise, and the
whole design needs repeats. That is a statistical-power question, and CLAUDE.md is explicit that
it gets answered before the compute is spent rather than after.

Nothing here is evidence about markets. It measures an instrument.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import UTC, date, datetime

os.environ.setdefault("QUANTBOT_MODEL_ENDPOINT", "http://localhost:11434/v1")
os.environ.setdefault("QUANTBOT_MODEL_GENERATOR", "llama3.1:8b")
os.environ.setdefault("QUANTBOT_MODEL_CRITIC", "mistral:7b")

from quantbot.research.composition import build_model_runtime  # noqa: E402
from quantbot.research.semantic import SemanticRefused, SemanticWorker  # noqa: E402
from quantbot.research.sources import Source, SourceKind  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

# One frozen packet, standing in for a #56 anomaly event with its catalyst context. Fixed text,
# fixed ids, fixed dates: every run below sees byte-identical input, which is the whole point.
HYPOTHESIS = (
    "Equities that print an unusually large single-session volume shock relative to their own "
    "trailing baseline continue to outperform their sector over the following five sessions."
)
SOURCES = [
    Source(
        source_id="packet-0001",
        kind=SourceKind.MARKET_DATA,
        uri="elrond://anomaly/packet-0001",
        title=(
            "SPY volume 3.4x its 60-session median on 2026-03-16; price closed +1.9% while the "
            "sector index closed +0.4%"
        ),
        publisher="elrond-anomaly-detector",
        published_at=datetime(2026, 3, 16, 21, 0, tzinfo=UTC),
        retrieved_at=datetime(2026, 3, 16, 21, 5, tzinfo=UTC),
        content_hash="packet0001",
        parser_version="probe-v1",
        derived=True,
        derived_from="market_anomaly_events",
    ),
    Source(
        source_id="packet-0002",
        kind=SourceKind.MARKET_DATA,
        uri="elrond://anomaly/packet-0002",
        title=(
            "The same session was an index rebalance date, and the venue feed covers roughly "
            "one third of consolidated volume"
        ),
        publisher="elrond-anomaly-detector",
        published_at=datetime(2026, 3, 16, 21, 0, tzinfo=UTC),
        retrieved_at=datetime(2026, 3, 16, 21, 5, tzinfo=UTC),
        content_hash="packet0002",
        parser_version="probe-v1",
        derived=True,
        derived_from="market_anomaly_events",
    ),
]


def normalise(items: list[str]) -> set[str]:
    """Compare claims by their content words, not their phrasing.

    A model rewording the same confounder is not a different confounder, and counting it as one
    would overstate variance -- which would bias this measurement toward the conclusion that the
    comparison is infeasible. Erring the other way is the honest direction here.
    """
    stop = {
        "the", "a", "an", "of", "in", "on", "to", "and", "or", "is", "are", "was", "were",
        "that", "this", "it", "its", "for", "with", "as", "by", "be", "may", "could", "would",
        "which", "than", "from", "at", "not", "but", "if", "then", "there", "these", "those",
    }
    return {
        word
        for item in items
        for word in item.lower().replace(",", " ").replace(".", " ").split()
        if word not in stop and len(word) > 3
    }


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    seed = sys.argv[2] if len(sys.argv) > 2 else None
    runtime = build_model_runtime()
    if seed is not None:
        # Patch the seed onto every resolved spec. A real fix belongs in `composition`; this is
        # measuring whether it would help before writing it.
        from quantbot.research.models import ModelRole

        for role in ModelRole:
            try:
                resolved = runtime.resolve(role, now=NOW)
            except Exception:  # noqa: BLE001
                continue
            object.__setattr__(
                resolved.spec,
                "parameters",
                {**resolved.spec.parameters, "seed": seed},
            )
    print(f"condition: seed={seed!r}")
    worker = SemanticWorker(runtime, model_cutoff=date(2024, 12, 31), cutoff_source="probe")

    results = []
    for index in range(runs):
        started = time.perf_counter()
        try:
            analysis, _assessment, response = worker.analyse(HYPOTHESIS, SOURCES, now=NOW)
        except SemanticRefused as refusal:
            results.append(
                {
                    "run": index,
                    "refused": str(refusal)[:200],
                    "seconds": round(time.perf_counter() - started, 1),
                }
            )
            print(f"run {index}: REFUSED after {results[-1]['seconds']}s -- {refusal}")
            continue
        except Exception as error:  # noqa: BLE001 -- a probe reports its own failures
            results.append(
                {
                    "run": index,
                    "error": repr(error)[:300],
                    "seconds": round(time.perf_counter() - started, 1),
                }
            )
            print(f"run {index}: ERROR after {results[-1]['seconds']}s -- {error!r}")
            continue
        elapsed = round(time.perf_counter() - started, 1)
        results.append(
            {
                "run": index,
                "seconds": elapsed,
                "model": response.spec.model,
                "confounders": analysis.confounders,
                "falsification_tests": analysis.falsification_tests,
                "disagreements": analysis.disagreements,
                "unsupported_claims": analysis.unsupported_claims,
            }
        )
        print(
            f"run {index}: {elapsed}s  confounders={len(analysis.confounders)} "
            f"falsifiers={len(analysis.falsification_tests)}"
        )

    good = [item for item in results if "confounders" in item]
    summary: dict[str, object] = {"runs": runs, "succeeded": len(good)}
    if len(good) >= 2:
        counts = [len(item["confounders"]) + len(item["falsification_tests"]) for item in good]
        sets = [
            normalise(list(item["confounders"]) + list(item["falsification_tests"]))
            for item in good
        ]
        overlaps = [
            jaccard(sets[i], sets[j])
            for i in range(len(sets))
            for j in range(i + 1, len(sets))
        ]
        summary.update(
            {
                "claim_counts": counts,
                "claim_count_mean": round(statistics.fmean(counts), 2),
                "claim_count_sd": round(statistics.pstdev(counts), 2),
                "pairwise_jaccard": [round(value, 3) for value in overlaps],
                "jaccard_mean": round(statistics.fmean(overlaps), 3),
                "jaccard_sd": round(statistics.pstdev(overlaps), 3),
                "seconds_mean": round(statistics.fmean([item["seconds"] for item in good]), 1),
            }
        )
    print("\n" + json.dumps(summary, indent=2))
    out = f"variance_probe_{'seeded' if seed else 'unseeded'}.json"
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"summary": summary, "results": results}, handle, indent=2)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
