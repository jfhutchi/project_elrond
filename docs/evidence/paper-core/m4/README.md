# M4 bounded shadow learning

Final frozen affected gate: **508 passed, 26 skipped, 590.12 s, exit 0**.
All six exact Ruff/type CI checks exit 0. The skips require disposable PostgreSQL
infrastructure. No operational activation, deployment or broker acceptance is claimed.

- [Summary](summary.json): actual prior/resulting artifact parameters, 30/10 split,
  validation metrics and +19.86754966887444 bps positive prediction.
- [Source and command pins](validation.json): accepted base, schema 33, fourteen
  exact tested source/test/fixture SHA256 values and the root-selected affected gate.
- [CI commands and log hashes](ci-results.json).
- [Export index](exports.json): complete immutable joined update, NO_CHANGE,
  failed fit, process recovery and late-cost correction bundles.
- [Log index](logs.json): original raw path/hash and lossless compressed-copy hash.
- [Final raw log, losslessly compressed](green-affected.txt.gz).
- [Implementation report](../../../../.superpowers/sdd/2026-10-13-paper-core/m4-report.md).

Each `.json.gz` contains the full original reference arrays and joins; none are
omitted. Use `gzip.decompress(Path(path).read_bytes())` to recover bytes and verify
the raw SHA256 from the index. Large raw test logs are similarly compressed;
original files remain at their recorded isolated scratch paths.

The update is learned from actual production trader/guardian/outcome code using
synthetic fills and reported synthetic fee activities. The features use the
existing causal five-minute bar mathematics, with balanced ±20 bps executed
after-cost targets and an explicitly synthetic −20 bps constant incumbent.
This demonstrates a numerical update; validation on this traded synthetic cohort
does not establish lasting profitability or real provider execution quality.

Process evidence distinguishes explicit disposable-child termination, automatic
child-owned 30-second watchdog expiry, pre-lock orphan cancellation, and actual
later-session recovery preserving previous trial charges. Historical payload IDs
are protected by the populated accepted-v32 migration/replay fixture captured
before M4 contract changes. Earlier red/intermediate results and pre-registration
CI results remain separately labeled. Independent review follows this submission.
