"""Is the *first* answer to a prompt reproducible across cold starts?

The warm-repeat measurement says identical calls return identical text, which reads as
determinism. This checks the thing that actually matters for #18 and #48: whether the answer
recorded in an artifact can be reproduced later, when the cache that produced it is gone.

If two cold runs disagree while every warm repeat agrees, then "rerun it and check" passes for
the wrong reason -- the second run is reading a cache the first one populated, not recomputing
the answer. That is a false-positive reproducibility check, which is worse than no check.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

OLLAMA = r"C:/Users/JHutc/AppData/Local/Programs/Ollama/ollama.exe"
PROBE = Path(__file__).with_name("variance_probe.py")
MODEL = "mistral:7b"


def normalise(items: list[str]) -> set[str]:
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
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    firsts = []
    for trial in range(trials):
        subprocess.run([OLLAMA, "stop", MODEL], capture_output=True, timeout=60)
        subprocess.run(
            [sys.executable, str(PROBE), "1"],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        payload = json.loads(Path("variance_probe_unseeded.json").read_text(encoding="utf-8"))
        record = payload["results"][0]
        if "confounders" not in record:
            print(f"trial {trial}: no analysis ({record})")
            continue
        claims = list(record["confounders"]) + list(record["falsification_tests"])
        firsts.append(claims)
        print(f"trial {trial}: {len(claims)} claims, first = {claims[0][:70]!r}")

    if len(firsts) < 2:
        print("not enough successful cold runs to compare")
        return 1

    sets = [normalise(item) for item in firsts]
    overlaps = [
        (i, j, round(jaccard(sets[i], sets[j]), 3))
        for i in range(len(sets))
        for j in range(i + 1, len(sets))
    ]
    print("\ncold-vs-cold pairwise Jaccard:")
    for i, j, value in overlaps:
        print(f"  trial {i} vs trial {j}: {value}")
    values = [value for _, _, value in overlaps]
    print(f"\nmean {round(sum(values) / len(values), 3)}  min {min(values)}  max {max(values)}")
    identical = sum(1 for value in values if value == 1.0)
    print(f"identical pairs: {identical}/{len(values)}")
    Path("cold_repeat_results.json").write_text(
        json.dumps({"firsts": firsts, "overlaps": overlaps}, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
