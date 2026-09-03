# Kronos worker environment v2: patched PyTorch, and what it changed

**Status:** measured · **Issue:** #200 · **Run:** 2026-09-02 · **Host:** HP Omen, Windows 11
26200, AMD Ryzen 7 6800H, CPU inference (no CUDA device present)

**Provenance:** an agent-run qualification, not an operator ratification. Everything below is a
recorded measurement with the command that produced it; nothing here is an approval.

---

## What changed

| | v1 | v2 |
|---|---|---|
| Identity | `kronos-worker-py311-requirements-v1` | `kronos-worker-py311-requirements-v2` |
| `torch` | `2.5.1` | `2.13.0` |
| `einops` / `huggingface-hub` / `numpy` / `pandas` / `safetensors` / `tqdm` | unchanged | unchanged |

**Exactly one line differs**, and that was measured rather than hoped for: `torch==2.13.0`
resolves and imports against the identical pin set on Python 3.11.16, including `numpy==1.26.4`.
No co-dependency bump was required.

`torch==2.5.1` sat behind three patched security releases — CVE-2025-32434 (fixed 2.6.0),
CVE-2026-24747 (fixed 2.10.0), CVE-2025-3000 (fixed 2.13.0). PR #117 established that the
`torch.load` deserialisation path is unreachable behind the safetensors-only loader. That
mitigation is unchanged and still enforced. It was never a reason to keep shipping the
unpatched version: an unreachable advisory is not a patched dependency, and the next advisory
may not land on a path something else happens to block.

2.13.0 rather than 2.14.0 (released the same day): 2.13.0 is the floor that clears all three
advisories and the target prior work already proposed. Moving beyond it is a further
re-attestation, on the same evidence this document records for this one.

## The pinned artifacts verify

Downloaded fresh from Hugging Face at the pinned revisions on 2026-09-02 and hashed:

| Artifact | Pinned SHA-256 | Match |
|---|---|---|
| `NeoQuasar/Kronos-small` weight | `b082dfcb…c3e020` | ✅ |
| `NeoQuasar/Kronos-small` config | `5e0f6a60…a68539` | ✅ |
| `NeoQuasar/Kronos-Tokenizer-base` weight | `59d85f6a…fc6bee` | ✅ |
| `NeoQuasar/Kronos-Tokenizer-base` config | `2366e7cc…b77a2` | ✅ |

Each snapshot contains exactly one `.safetensors` file, so the worker's
"exactly one safetensors" requirement is satisfiable rather than vacuous. The pinned Kronos
checkout (`67b630e`) clones clean with no ignored or untracked files.

This is an independent confirmation that the constants in the repository still describe the
upstream artifacts, which nothing had checked since they were written.

## The qualification

`scripts/qualify_kronos_environment.py` drives a worker entry file exactly as
`KronosSignalProvider` does (`forecasting/provider.py`) — same argument vector, same allowlisted
offline environment, same isolated interpreter flags — and compares two environments on one
frozen corpus. (Named `KronosSubprocessProvider` here until 2026-09-03; no such class exists, so
a reader checking the claim would have grepped and found nothing.)

The corpus is **synthetic**, from a fixed seed: 256 daily bars of a geometric random walk with
plausible drift and volatility, `corpus_hash =
11f86ed0bf11e6f1a6541bcddc607dd460f16ef99de39e99067e5de94ff0ba25`. The question is whether two
environments compute the same function, not whether the model is any good. Synthetic bars answer
that exactly as well as market data, consume no protected evaluation window, and add nothing to
the multiple-testing budget.

**The tolerance policy was fixed before the numbers were read** — a threshold chosen after seeing
the result is not a threshold. It is written into the script's module docstring: both
environments must produce a well-formed artifact, matching schema and shape, all finite values,
and must each reproduce themselves exactly at a fixed seed. Given all of that, **bit-identical
output is the only result that makes v2 the same forecaster as v1.** No tolerance band, because
choosing how much drift is "harmless" for a claim nobody has made yet is choosing a number to get
a convenient answer.

### Result

| Measure | v1 (`torch 2.5.1`) | v2 (`torch 2.13.0`) |
|---|---|---|
| Runs completed | 2 / 2 | 2 / 2 |
| Self-reproducible at fixed seed | **exact** — 0 of 240 values differ | **exact** — 0 of 240 values differ |
| Output shape | 4 paths × 12 candles, OHLCV | identical |
| Non-finite values | none | none |
| Model initialisation | 0.28 s | 0.50 s |
| Inference (4 samples × 12 steps) | 2.17 s | 2.98 s |
| Wall clock, whole worker invocation | 6.2 – 6.5 s | 11.6 – 11.8 s |
| Peak working set | 453 MB | 481 MB |

Cross-environment, on the same frozen input:

| Field | Values differing | Max relative deviation |
|---|---|---|
| `open` | 47 / 48 | 3.08 % |
| `high` | 47 / 48 | 2.03 % |
| `low` | 47 / 48 | 3.39 % |
| `close` | 47 / 48 | 3.89 % |
| `volume` | 48 / 48 | 49.42 % |

Mean terminal return across sample paths: **−3.18 % (v1)** vs **−3.27 % (v2)**. The direction
agrees; the individual sampled paths do not.

### Verdict: `DISTINCT_FORECASTER_NEW_EVIDENCE_SERIES`

Both environments are internally deterministic — each reproduces itself to the last digit — so
the divergence is a real implementation difference, not sampling noise. Kronos samples
autoregressively under a seeded RNG; a change in kernel implementation or RNG consumption makes
the token sequence diverge on the first step and stay diverged.

Per the #137 decision in [`worker-environment-identity.md`](worker-environment-identity.md),
that makes v2 a different forecaster:

- **Existing v1 forecasts keep `kronos-worker-py311-requirements-v1` permanently.** They are not
  relabelled, re-attributed, migrated, or recomputed. `WORKER_ENVIRONMENTS` holds the v1 package
  set as an immutable record, and `test_the_retired_v1_environment_record_is_never_rewritten`
  asserts it literally so a later tidy-up cannot rewrite history.
- **New forecasts carry v2 and start a new evidence series.** They are not pooled with the v1
  series. Nothing pools them today — no aggregation surface over evaluations exists yet — but
  the first one built must group by environment identity, which is available on every row inside
  `request_json` and is baked into `forecast_id` (the request id hashes the config).
- **This is not a bridging measurement.** One synthetic corpus, one host, one CPU is not the
  population. It is enough to establish that the two environments differ; it is not enough to
  establish a mapping between them, and nothing here licenses combining the series.

## Security verification

Queried mechanically rather than asserted, on 2026-09-02. Every open Dependabot alert on this
repository was `torch` on `requirements-kronos-worker.txt` — nine of them:

| Alert | Severity | Advisory | Vulnerable range | Cleared by 2.13.0 |
|---|---|---|---|---|
| #3 | critical | `GHSA-53q9-r3pm-6pq6` | `< 2.6.0` | ✅ patched in 2.6.0 |
| #7 | medium | `GHSA-vgrw-7cvw-pwgx` | `< 2.9.1` | ✅ patched in 2.9.1 |
| #2 | medium | `GHSA-887c-mr87-cxwp` | `<= 2.7.1` | ✅ patched in 2.8.0 |
| #5 | medium | `GHSA-f4hp-rmr7-r7v8` | `<= 2.6.0` | ✅ out of range |
| #8 | low | `GHSA-qfhq-4f3w-5fph` | `< 2.10.0` | ✅ patched in 2.10.0 |
| #9 | low | `GHSA-rrmf-rvhw-rf47` | `<= 2.12.1` | ✅ patched in 2.13.0 |
| #1 | low | `GHSA-3749-ghw9-m3mg` | `< 2.7.1-rc1` | ✅ patched in 2.7.1-rc1 |
| #4 | low | `GHSA-c678-jfcj-6jmf` | `<= 2.6.0` | ✅ out of range |
| #6 | low | `GHSA-x3gm-94wq-g975` | `<= 2.6.0` | ✅ out of range |

**All nine clear.** Three of them (#4, #5, #6) have no declared patched version in the advisory
database; 2.13.0 is simply outside their stated vulnerable range. That is "no longer applicable",
which is a weaker statement than "patched", and it is written that way here rather than folded
into a count.

Nothing is introduced in exchange:

```
gh api "advisories?ecosystem=pip&affects=torch@2.5.1"   -> 9 advisories
gh api "advisories?ecosystem=pip&affects=torch@2.13.0"  -> 0 advisories
gh api "advisories?ecosystem=pip&affects=torch@2.14.0"  -> 0 advisories
```

The 2.5.1 query returning exactly the nine alerted advisories is what makes the empty 2.13.0
result meaningful rather than a query that silently matched nothing.

2.14.0 is equally clean, so choosing 2.13.0 leaves nothing on the table security-wise. It is a
maturity choice, not a security one.

No other package in the manifest changed, so no new dependency advisory is possible from this
edit.

## The attestation contract is enforced, end to end

Running the v2 worker against the v1 environment fails closed in 0.2 s, before any model is
loaded:

```
WorkerContractError: worker dependency version is not pinned: torch
```

This is what makes the manifest an attestation rather than a comment. It is also why the
requirements file and `EXPECTED_PACKAGE_VERSIONS` must move together: had this PR bumped only
one, every forecast would have failed with a bare `WORKER_FAILED` — the safe outcome, and a
useless one. `test_standalone_requirements_match_worker_attestation_manifest` and
`test_the_two_copies_of_the_pinned_contract_agree` hold both halves together, and
`.github/dependabot.yml` keeps a bot from moving one of them.

Raw artifact: `reports/kronos-worker-v2/run-mismatch.json`.

## A defect the qualification found

`peak_process_memory_bytes` was `null` on **every Kronos forecast ever produced on Windows**.

`_peak_process_memory_bytes` called `GetProcessMemoryInfo` through `ctypes.windll` with no
`restype` or `argtypes`. The 64-bit `GetCurrentProcess` pseudo-handle came back as a default
`c_int` — truncated to `-1` — and was marshalled back as a 32-bit argument. The call failed, and
the function returned `None`.

Nothing noticed because `None` is a *valid* answer: it is what the function returns on a platform
that genuinely cannot report the figure. A broken probe and an unavailable one were
indistinguishable in the record, and the field is optional, so nothing downstream complained.

Fixed by declaring the signatures. The v1 environment's peak working set in the table above
(453 MB) was measured with a diagnostic build carrying the fix, since the v1 worker as shipped
could not report it.

## Reproducing this

```bash
WR=<worker root, outside the Elrond checkout>

git clone https://github.com/shiyu-coder/Kronos.git "$WR/Kronos"
git -C "$WR/Kronos" checkout 67b630e67f6a18c9e9be918d9b4337c960db1e9a

uv venv "$WR/venv-v2" --python 3.11
VIRTUAL_ENV="$WR/venv-v2" uv pip install -r requirements-kronos-worker.txt

HF_HOME="$WR/cache" HF_HUB_DISABLE_SYMLINKS=1 "$WR/venv-v2/Scripts/python" -c "
from huggingface_hub import snapshot_download
snapshot_download('NeoQuasar/Kronos-small', revision='901c26c1332695a2a8f243eb2f37243a37bea320')
snapshot_download('NeoQuasar/Kronos-Tokenizer-base', revision='0e0117387f39004a9016484a186a908917e22426')"

python scripts/qualify_kronos_environment.py corpus --out "$WR/corpus.json"
python scripts/qualify_kronos_environment.py run --corpus "$WR/corpus.json" \
    --worker-entry src/quantbot/forecasting/kronos_worker.py \
    --worker-python "$WR/venv-v2/Scripts/python.exe" \
    --kronos-repository "$WR/Kronos" --cache-directory "$WR/cache" \
    --git-executable "$(command -v git)" --out "$WR/run-v2-a.json"
```

`HF_HUB_DISABLE_SYMLINKS=1` is required on a default Windows install: without Developer Mode or
an elevated shell, `huggingface_hub` fails with `OSError: [WinError 1314] A required privilege is
not held by the client` while creating its cache symlinks. The documented setup in
[`kronos-shadow.md`](kronos-shadow.md) did not mention it and would have failed for the next
person to follow it.

Raw run artifacts, including both repeat runs per environment and the machine-readable
comparison, are in [`../reports/kronos-worker-v2/`](../reports/kronos-worker-v2/).

## What this does not establish

- **Not a claim about forecast quality.** Neither environment was scored against realised
  outcomes here, and the synthetic corpus could not support such a claim if it had been.
- **Not a claim about other hosts.** CPU only, one machine, one OS. A Linux or GPU worker is a
  third environment and needs its own identity and its own measurement.
- **Not a security clearance for `torch` generally.** Three named advisories no longer apply to
  the pinned version. The safetensors-only loader, the SHA-256 weight and config verification,
  the pinned-source materialisation and the pickle-checkpoint refusal all remain in force and
  are all still covered by their own tests, because the next advisory has not been written yet.
