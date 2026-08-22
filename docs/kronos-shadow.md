# Kronos Shadow Forecasting

## Status and boundary

This integration is an **experimental, shadow-only research collector**. It generates and later
scores candle forecasts. It is not a strategy, broker, risk gate, paper observation, qualification
day, or promotion candidate.

There is **no direct order path**:

```text
active market-data DB (read only)
              |
              v
quantbot-kronos CLI -> immutable request -> isolated Kronos worker
              |                              (external Python/env/cache/repo)
              v
forecast-only SQLite ledger
  - immutable forecast artifact
  - later append-only realized outcome
```

The forecasting package is not imported by the trading runtime, broker, execution, operations,
risk, or strategy packages. Its database has a separate schema and migration chain containing no
signals, order intents, broker orders, deployments, or qualification tables. The CLI refuses to
use the source database, the default `quantbot.db`, `QUANTBOT_DB_PATH`, or a hard link to one of
those paths as its forecast database.

Forecast failure is fail-closed. A valid request receives a durable `FAILED` row with a safe reason
code such as `PROVIDER_SETUP_FAILED`, `WORKER_TIMEOUT`, `WORKER_FAILED`, or
`WORKER_ARTIFACT_INVALID`. Worker stderr, filesystem paths, and exception text are not persisted.
Failure has no effect on the paper daemon.

## Point-in-time contract

Every request freezes:

- symbol, `as_of`, exact ordered OHLCV bars, and last source-bar timestamp;
- production market-data key (`provider:feed:adjustment:timeframe`) and canonical adjustment
  metadata;
- source vintage ID and the time that vintage became available;
- exact future target timestamps, lookback, horizon, sampling settings, model pins, and attempt;
- content-derived source and request hashes.

Bars later than `as_of` are excluded before the lookback window is selected. A source vintage is
rejected when `available_at > as_of` or when it claims to be available before its final source bar.
The current active bar cache does not store a per-row publication time, so
`--source-available-at` and `--source-vintage-id` are explicit operator attestations. They must
identify the concrete retrieval/vintage used; they are not inferred from bar timestamps.

For exchange-aware evidence, pass one authoritative `--future-timestamp` per horizon step. The
fallback generator skips weekends but does **not** guess exchange holidays. Scoring requires an
outcome bar at every registered target timestamp; Saturday/Sunday, intraday, early, or merely
"next N" bars cannot mature a daily forecast.

Outcomes repeat the provider/vintage/availability/adjustment lineage. The ledger retains the exact
realized target bars as canonical `outcome_json`, its SHA-256, the scoring version, metrics, and
baseline results. A later data correction is therefore a distinct immutable outcome identity, not
an in-place rewrite.

## Reviewed and pinned artifacts

The first integration accepts only this manifest:

| Artifact | Identity | SHA-256 verified at runtime |
|---|---|---|
| Kronos code | commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a` | clean Git checkout required |
| Model | `NeoQuasar/Kronos-small` revision `901c26c1332695a2a8f243eb2f37243a37bea320` | weights `b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020` |
| Model config | same model revision, `config.json` | `5e0f6a605d5f81b5c9b559fe5cf716a1acb041c744e6f41bd05b097b7a685396` |
| Tokenizer | `NeoQuasar/Kronos-Tokenizer-base` revision `0e0117387f39004a9016484a186a908917e22426` | weights `59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee` |
| Tokenizer config | same tokenizer revision, `config.json` | `2366e7ccfec76cbc19cf3c4c1b9c5d901be336ca1e83f2d2292c9bff381b77a2` |

The worker rejects revision overrides, dirty, untracked, or ignored files in the Kronos checkout,
then imports only an ephemeral `git archive` of the pinned commit and verifies the module origin.
It also rejects artifact hash mismatches, Python versions other than 3.11, and package versions that differ from
`requirements-kronos-worker.txt`. It loads verified local snapshots in offline mode. The runtime
environment hash also records the worker file, platform, Python version, dependency versions, and
device.

Default zero-shot settings are lookback 40, horizon 12, temperature 0.6, top-p 0.9, top-k 0,
10 sampled paths, seed 0, and CPU. Sampling settings are configurable within validated bounds, but every change
changes request identity. The public Kronos API averages multiple samples, so this worker invokes
`sample_count=1` repeatedly with deterministic per-path seeds and retains every path.

The first integration is deliberately CPU-only. The GPU-memory field is reserved for a later CUDA
profile, but CUDA is rejected until its deterministic-library configuration and a hardware smoke
test are pinned and reproduced.

Sample dispersion is a descriptive property of those generated paths. It is **not confidence,
probability, calibrated uncertainty, position size, or risk authority**.

## Licenses and sources

- Kronos source: [shiyu-coder/Kronos](https://github.com/shiyu-coder/Kronos), pinned commit
  [67b630e](https://github.com/shiyu-coder/Kronos/tree/67b630e67f6a18c9e9be918d9b4337c960db1e9a),
  [MIT license](https://github.com/shiyu-coder/Kronos/blob/67b630e67f6a18c9e9be918d9b4337c960db1e9a/LICENSE).
- Model: [NeoQuasar/Kronos-small at the pinned revision](https://huggingface.co/NeoQuasar/Kronos-small/tree/901c26c1332695a2a8f243eb2f37243a37bea320), model card reports MIT.
- Tokenizer: [NeoQuasar/Kronos-Tokenizer-base at the pinned revision](https://huggingface.co/NeoQuasar/Kronos-Tokenizer-base/tree/0e0117387f39004a9016484a186a908917e22426), model card reports MIT.
- Paper: [Kronos: A Foundation Model for the Language of Financial Markets](https://arxiv.org/abs/2508.02739).

Elrond does not vendor Kronos source or model weights. If those artifacts are redistributed, retain
the upstream MIT notice and re-check the artifact repositories' terms at distribution time.

## Dedicated worker setup

Do not install the QuantBot project into the worker environment. Create all worker assets outside
the Elrond checkout. Example PowerShell setup:

```powershell
$ElrondRepo = "C:\path\to\project_elrond"
$WorkerRoot = Join-Path $env:LOCALAPPDATA "quantbot-kronos-worker"
$WorkerVenv = Join-Path $WorkerRoot "venv"
$KronosRepo = Join-Path $WorkerRoot "Kronos"
$KronosCache = Join-Path $WorkerRoot "cache"

py -3.11 -m venv $WorkerVenv
$WorkerPython = Join-Path $WorkerVenv "Scripts\python.exe"
& $WorkerPython -m pip install -r (Join-Path $ElrondRepo "requirements-kronos-worker.txt")

git clone https://github.com/shiyu-coder/Kronos.git $KronosRepo
git -C $KronosRepo checkout 67b630e67f6a18c9e9be918d9b4337c960db1e9a
git -C $KronosRepo status --porcelain=v1 --untracked-files=all --ignored
```

The final Git command must print nothing. Confirm the worker environment cannot import Elrond:

```powershell
& $WorkerPython -I -c "import importlib.util; assert importlib.util.find_spec('quantbot') is None"
```

Pre-warm the exact Hugging Face revisions while network access is intentionally available. Runtime
inference itself is offline:

```powershell
$env:HF_HOME = $KronosCache
& $WorkerPython -c "from huggingface_hub import snapshot_download; snapshot_download('NeoQuasar/Kronos-small', revision='901c26c1332695a2a8f243eb2f37243a37bea320')"
& $WorkerPython -c "from huggingface_hub import snapshot_download; snapshot_download('NeoQuasar/Kronos-Tokenizer-base', revision='0e0117387f39004a9016484a186a908917e22426')"
Remove-Item Env:HF_HOME
```

The worker verifies the downloaded `config.json` and single Safetensors file before loading them.

### Linux / WSL equivalent

The same setup on POSIX, which is where the worker has actually been exercised:

```bash
ELROND_REPO=/path/to/project_elrond
WORKER_ROOT="$HOME/quantbot-kronos-worker"

uv venv "$WORKER_ROOT/venv" --python 3.11
VIRTUAL_ENV="$WORKER_ROOT/venv" uv pip install -r "$ELROND_REPO/requirements-kronos-worker.txt"

git clone https://github.com/shiyu-coder/Kronos.git "$WORKER_ROOT/Kronos"
git -C "$WORKER_ROOT/Kronos" checkout 67b630e67f6a18c9e9be918d9b4337c960db1e9a

HF_HOME="$WORKER_ROOT/cache" "$WORKER_ROOT/venv/bin/python" - <<'EOF'
from huggingface_hub import snapshot_download
snapshot_download("NeoQuasar/Kronos-small", revision="901c26c1332695a2a8f243eb2f37243a37bea320")
snapshot_download("NeoQuasar/Kronos-Tokenizer-base", revision="0e0117387f39004a9016484a186a908917e22426")
EOF
```

**Install Torch from PyPI, not the CPU wheel index.** `torch==2.5.1` from the CPU index reports
its version as `2.5.1+cpu`, which is not the pinned string, and the worker refuses the
environment. The refusal is correct -- a different build is a different environment -- but the
failure surfaces as a bare `WORKER_FAILED`, so it is worth knowing before debugging it. The
default PyPI wheel bundles CUDA libraries and still runs on CPU; expect roughly 3 GB installed.

Worker stderr is deliberately never reflected into records or CLI output, so a failing worker
reports only `WORKER_FAILED`. To see why, run the staged worker directly:

```bash
cd "$WORKER_ROOT/cache"
env -i HF_HOME="$PWD" HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false TRANSFORMERS_OFFLINE=1   "$WORKER_ROOT/venv/bin/python" -I -B kronos-worker-*.py   --kronos-repository "$WORKER_ROOT/Kronos" --git-executable "$(command -v git)" < request.json
```

## Dry run and shadow run

Use a read-only source database that already contains production-format daily bars. The dry run
builds and hashes the request but does not construct the provider, run Kronos, or write a forecast:

```powershell
uv run quantbot-kronos shadow-run `
  --source-database .\quantbot.db `
  --forecast-database "$env:LOCALAPPDATA\quantbot-kronos-worker\shadow.db" `
  --symbol AAPL `
  --as-of 2026-08-19T20:10:00Z `
  --market-data-provider alpaca --market-data-feed iex `
  --market-data-adjustment all --timeframe 1Day `
  --source-vintage-id alpaca-rest-20260819T201000Z `
  --source-available-at 2026-08-19T20:10:00Z `
  --kronos-repository "$env:LOCALAPPDATA\quantbot-kronos-worker\Kronos" `
  --python-executable "$env:LOCALAPPDATA\quantbot-kronos-worker\venv\Scripts\python.exe" `
  --cache-directory "$env:LOCALAPPDATA\quantbot-kronos-worker\cache" `
  --dry-run
```

Remove `--dry-run` to infer and persist. Add `--future-timestamp` 12 times when an authoritative
exchange calendar is available. Add symbols with repeated `--symbol`. Each completed symbol is
persisted in its own transaction, and stdout is one canonical JSON line. Exit 0 means every result
was `SUCCESS` (or `DRY_RUN`); failed or insufficient artifacts return nonzero.

Retries do not overwrite an attempt. Repeating the same request returns the existing immutable
artifact without rerunning the model. After correcting an environmental failure, pass
`--attempt-number 1` (then 2, and so on) to create a new request identity.

## Scoring

After every registered target exists in a named outcome vintage:

```powershell
uv run quantbot-kronos score `
  --source-database .\quantbot.db `
  --forecast-database "$env:LOCALAPPDATA\quantbot-kronos-worker\shadow.db" `
  --as-of 2026-09-04T20:10:00Z `
  --outcome-vintage-id alpaca-rest-20260904T201000Z `
  --outcome-available-at 2026-09-04T20:10:00Z `
  --symbol AAPL
```

The scorer records terminal return direction, absolute error, squared error, and three transparent
baselines: 20-bar momentum, 5-bar short reversal, and zero-return persistence. These are research
comparisons only. Rank IC, turnover, spread/slippage/cost simulation, multiple-testing correction,
and the current strategy's own signal as an injectable baseline are deferred to a later batch
evaluator; this integration does not import or execute the protected strategy to obtain them.

## Known limitations and honest evidence status

- No model weights, external Kronos checkout, or dedicated worker environment are shipped with the
  repository; each operator stages their own. Real inference has now been exercised on the
  operator's machine (WSL2 Ubuntu, Python 3.11.15, Torch 2.5.1, CPU only), through
  `KronosSignalProvider` rather than by calling the model directly: 64 daily bars in, horizon 3,
  2 sample paths, model initialization 0.23 s, inference 0.18 s, peak process memory 660 MB. The
  GPU field remains null; GPU measurement is deferred with the CPU-only first integration.
- **Those runs establish plumbing and cost, not forecast value.** They say what the integration
  costs to run and nothing about whether Kronos predicts anything. No forecast accuracy is
  claimed anywhere in this document.
- **Kronos samples OHLC independently, and 4.35% of sampled candles could not have been real
  bars** -- typically a low above the close. Measured over 460 candles from 23 symbols. Sampled
  candles are therefore recorded as the model produced them rather than validated against
  real-bar ordering, and the violation count is persisted per forecast in
  `ForecastFeatures.inconsistent_candles`. Enforcing ordering discarded 61% of real forecasts at
  4 samples x horizon 5, and effectively all of them at settings large enough to estimate
  dispersion; worse, the loss correlated with model uncertainty, so the survivors were a biased
  subset. The count is `None` for artifacts written before the measurement existed, which is not
  the same claim as `0`. Whether inconsistent forecasts score worse is an open question the
  recorded counts make answerable.
- The process boundary scrubs application credentials, stages the worker outside the checkout,
  makes Elrond unimportable, disables common socket/process APIs before upstream imports, and runs
  with local-only artifacts. This is defense in depth, not an OS sandbox/chroot. On Windows it does
  not provide filesystem ACL isolation or a Job Object, and timeout cannot prove termination of an
  adversarial native descendant. The trust boundary therefore still includes the reviewed, clean,
  hash-pinned upstream code and dependencies.
- The model is initialized once per request. Multi-symbol batching and a persistent model service
  are deferred until measurements justify the added authority and lifecycle complexity.
- The worker manifest pins the seven direct inference packages, and the runtime hash binds those
  versions, Python, platform, device, and worker bytes. Transitive wheel hashes, CPU/thread/BLAS
  identity, and an immutable worker-image digest are not yet locked; retain the actual environment
  alongside evidence when bit-level reproduction matters. The root requirements recipe is a
  source-checkout artifact and is not yet bundled into wheel-only installations.
- Deterministic IDs and immutable inserts make sequential retries safe, but v1 has no durable
  pre-inference claim. Concurrent schedulers can duplicate expensive inference before one insert
  wins. Run one shadow scheduler until atomic claims and paginated scoring are added.
- Kronos-small's public training cutoff is not established here. Historical overlap cannot be
  ruled out, so historical scores are exploratory. Only forecasts frozen before outcomes arrive
  can become clean prospective shadow evidence.
- No forecast, score, or sampled dispersion counts toward paper qualification. No research
  hypothesis is registered and no protected forward-observation counter is consumed by this tool.
