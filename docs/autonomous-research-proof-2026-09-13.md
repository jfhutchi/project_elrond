# Autonomous research cycle on the Omen, re-proven after the holdout defect — 2026-09-13

With no human-created research task, the scheduled research component on `LEANNES-LAPTOP`
inspected its own measured state, chose a question from a slate it computed, opened the task,
measured it under the Windows AppContainer sandbox on the exploratory range only, graded it,
persisted the evidence, and reached a terminal scientific verdict. It then looked again, found
nothing eligible, recorded that, and on a fresh process wrote nothing new.

This supersedes the 2026-09-12 proof. That run was real, but its measurement read the 14 clean
confirmatory sessions it claimed to protect; see
[the correction on that document](autonomous-research-proof-2026-09-12.md) and
[what changed](#what-was-wrong-with-the-first-proof-and-what-it-cost).

Every value below is recorded output, not description. The live store was never opened: every
measurement ran against a byte copy, and the live files are SHA-256-identical before and after.

## Provenance

| | |
|---|---|
| Host | `LEANNES-LAPTOP` (OMEN by HP Gaming Laptop 16-n0xxx, Windows 11 Home 26200) |
| Code | `claude/research-proposer` @ `ac546f3afae1fcc04d543a1245ffee7019c82742`, clean worktree |
| Entry point | `deploy/omen/start-service.ps1 -Component research` — the Task Scheduler argv, unmodified |
| Component command | `research-cycle --max-steps 1 --commit --propose-when-idle` |
| Proposer / experiment version | `2` / `2` |
| Store | byte copy of `D:\Elrond\state\quantbot-paper-fresh-20260908.db` (+ `-wal`, `-shm`), migrated 27 → 28 with `migrate-database --confirm` |
| Model | Ollama 0.33.3 at `127.0.0.1:11434`, `qwen3:4b-instruct` (digest `0edcdef34593…`) |
| Environment file | proof-only: PAPER, `LIVE_TRADING_ACKNOWLEDGED=false`, **no Alpaca keys of any kind** |
| Session | 2026-09-12 local (Saturday, market closed) |

## Starting condition — nothing seeded

Measured on the copy at `2026-09-13T02:53:04Z`, before migration, and again at `02:53:20Z` after:

```
schema_version 27 -> 28     research_tasks 0     research_task_events 0
research_proposals 0        search_runs 0        hypotheses 0
research_records 25 (prior findings)             bars 7061 (23 symbols x 307 sessions)
```

No task, event, proposal or search record was written by hand at any point.

## Measured state the proposer saw

| identity | status | trials | stored | consumed through | unsearched | eligible |
|---|---|---|---|---|---|---|
| `us-equities-daily` | EXHAUSTED | 68 | 307 | 2026-08-21 | 14, from 2026-08-24 | yes |
| `crypto-daily` | AVAILABLE | 0 | 0 | — | 0 | no — no observations in this store |
| `us-macro-monthly` | AVAILABLE | 0 | 0 | — | 0 | no — no observations in this store |
| `sec-edgar-filings` | AVAILABLE | 0 | 0 | — | 0 | no — no observations in this store |
| `options-implied-daily` | UNSUPPORTED | 0 | 0 | — | 0 | no |

The three `AVAILABLE` sources remain untouched *and empty* — re-measured, not assumed. Nothing here
ingests them.

The slate, per question:

| question | clean sessions | refutations matched | decision |
|---|---|---|---|
| `benchmark-move-size-persists` v1 | 14 | `refuted-016` (adjudicated) | **eligible** |
| `dispersion-precedes-volatility` v1 | 0 | `refuted-016` (adjudicated) | rejected: all 14 already seen by this question (exposed through 2026-09-11) |
| `breadth-persists-next-session` v1 | 0 | `refuted-015` (adjudicated) | rejected: same |
| `dispersion-predicts-benchmark-direction` v1 | 0 | none | rejected: same |

23 refuting records were checked against subject terms.

## Run 1 — propose, then PROPOSED → SCOUTING

```
start 2026-09-13T02:53:39.411Z (22:53:39 EDT)   end 02:54:01.440Z   exit 0
```

The model was given one eligible option and chose it. Opened as
`auto-benchmark-move-size-persists-v1-us-equities-daily`, actor `proposer/2`, with this provenance on
the opening event and in `research_proposals` (`evidence_json.provenance` is byte-equal):

```
proposal_id              prop-01142a0a11f442e947399821       outcome CREATED, dataset_sequence 1
code_commit              ac546f3afae1fcc04d543a1245ffee7019c82742
proposed_at / as_of      2026-09-13T02:53:42+00:00 / 2026-09-13
template / version       benchmark-move-size-persists / 1
catalogue_hash           0e0af9d860d434675d2a0c466bd4a5aba306a2df43402d9f8b3e2368393175a0
estimand / effects       INFORMATION_COEFFICIENT, expected 0.20, minimum practical 0.10 (pre-registered)
dataset                  sip-us-equities-daily (us-equities-daily): EXHAUSTED, 68 trials, 307 stored
consumed / exploratory   through 2026-08-21 / through 2026-08-21; exposed_through none
clean sessions           14, first 2026-08-24
capacity_class           clean-confirmatory-sessions-present
novelty_decision         no task with this identity exists, no autonomous task for this question was
                         refuted, and every refuting record matching its subject terms is adjudicated;
                         the cycle's novelty gate runs next
refutations              matched refuted-016, adjudicated in the catalogue
selector                 model
model_identity           openai-compatible:qwen3:4b-instruct@configured
model_response_sha256    c55e115b5f3f6092d0fb4c030e10ff53bb313ad1afdb773c30b350aeb6c54ba1
prompt_template          research-candidate-selection@1
prompt_template_hash     df9d98eb598c29e910b71f7dba2547f0e52d70121261fa572d448425e6c899c6
alternatives_considered  (none: this was the only eligible option)
alternatives_rejected    the three exposed questions, each with its reason
```

The model's `reason` is recorded verbatim in `selection_reason`. It is the model's text, not
evidence, and nothing downstream reads it.

The same run's one cycle step, through the unchanged capacity and novelty gates:
`PROPOSED → SCOUTING` — *no stored record settles this question*.

## Run 2 — SCOUTING → UNDERPOWERED, measured in the sandbox

```
start 2026-09-13T02:54:11.902Z (22:54:11 EDT)   end 02:54:15.157Z   exit 0
```

| | |
|---|---|
| sandbox backend | `WindowsSandbox` (AppContainer, no capabilities), `os_enforced=True` |
| sandbox policy | wall clock 90 s, memory 256 MB, network disabled |
| resource use | 0.140 s, peak 7.45 MB |
| panel | 2025-06-24 .. **2026-08-21**, 293 sessions, 23 symbols — ends at the consumed boundary |
| panel_sha256 | `60bfd91689e2df4d70483f117dedabb989c853db1747639562e5fb212a8a304d` |
| script_sha256 | `9c317a09ff3c7a977be2c00c5ce564ef54fee9c744dc5d24699d410141f6c3b2` |
| stdout | `{"degenerate": false, "first_session": "2025-06-24", "last_session": "2026-08-21", "measurement_id": "benchmark_absolute_move_persistence", "observations": 291, "sessions": 293, "statistic": 0.02593945802823299, "t_statistic": 0.4411192161936907, "universe": 23}` |
| stderr | empty |

Grade, computed outside the sandbox from the pre-registered effect:

| | |
|---|---|
| cumulative trials | 69 (68 prior + the one a confirmation would add) |
| luck bar | t = 2.910019 |
| observations required | 210 |
| clean confirmatory observations | 12 (14 sessions, both lead-lag losses inside the clean range) |
| minimum detectable IC at 12 | 0.748707 |
| verdict basis | power gate on clean confirmatory observations |

Recorded reason:

> confirmation needs 210 observations for the pre-registered effect of 0.20 to clear a 2.910019 luck
> bar at 69 cumulative trials, and 12 clean confirmatory observation(s) exist. The exploratory
> measurement over 2025-06-24..2026-08-21 (291 observations, none after the exploratory boundary)
> returned benchmark_absolute_move_persistence=0.02593945802823299 (t=0.4411192161936907). It is
> recorded as exploratory evidence only: it did not decide this verdict, it cannot be cited as
> confirmation, and it is not a refutation.

Evidence written in the same transaction as the move:

| table | id | content |
|---|---|---|
| `search_runs` | `auto-exploratory:auto-benchmark-move-size-persists-v1-us-equities-daily:e2` | actor `autoexperiment` v2, 1 candidate evaluated, `sip-us-equities-daily` 2025-06-24..2026-08-21, role DISCOVERY, config hash = script hash, stdout/stderr, started 02:54:14.279Z finished 02:54:14.963Z |
| `research_records` | `exploratory:…:e2` | FINDING, `EXPLORATORY_ONLY` |
| `research_records` | `underpowered:…:e2` | FINDING, `UNDERPOWERED` |
| `research_relationships` | both records | `USES_DATASET → us-equities-daily` |

Capacity afterwards is unchanged — `us-equities-daily: EXHAUSTED; 68 trials spent; 14 unsearched
session(s) from 2026-08-24` — and cumulative trials still read 68: an exploratory look at already
consumed data spends no confirmatory capacity and registers no hypothesis.

## Runs 3 and 4 — idle, nothing eligible, and a restart writes nothing

```
run 3  02:54:37.306Z -> 02:54:39.904Z  exit 0  NO_ELIGIBLE  recorded=true   prop-e8a55e69586ec63c800bcbd3
run 4  02:54:39.942Z -> 02:54:42.484Z  exit 0  NO_ELIGIBLE  recorded=false  prop-e8a55e69586ec63c800bcbd3
```

Run 3 was idle (the task is terminal), so the proposer ran, found the dataset free again and every
question either asked or exposed, and recorded that. Run 4 is a new process against unchanged state:
same attempt id, no row written. This is what the six-hourly schedule will do until new data arrives.

## Final state

```
research_tasks        1   auto-benchmark-move-size-persists-v1-us-equities-daily  UNDERPOWERED
research_task_events  3   None->PROPOSED (proposer/2)  PROPOSED->SCOUTING  SCOUTING->UNDERPOWERED
research_proposals    2   CREATED (sequence 1), NO_ELIGIBLE
search_runs           1
research-status       tasks {"UNDERPOWERED": 1}, actionable_now 0, milestone M0_RESEARCH_PLATFORM_OPERATIONAL
```

## The control came back near zero, and the instrument was checked before reading anything into it

`benchmark-move-size-persists` was added because three earlier measurements on this panel returned
|t| below 1: if the instrument could not see volatility clustering either, those nulls would describe
the instrument. It did not see it: IC 0.026, t 0.44. Per this project's rule, the instrumentation was
validated before the result was interpreted — on the consumed range only, never on or after
2026-08-24, and outside the store (no record, no trial):

```
recomputed corr(|r_t|, |r_t+1|) for SPY, plain Python, no sandbox   0.025939   (sandbox: 0.025939)
sessions 293, 2025-06-24..2026-08-21, calendar gaps > 4 days        none
SPY daily stdev 0.774%, largest move 2.86% (2026-03-31)
Spearman lag-1 |r| -0.020   lag-1 r^2 0.032   lag-3 |r| 0.096
lag-1 |r| elsewhere: GLD 0.154  IWM 0.109  EFA 0.094  QQQ 0.047  TLT -0.033
```

The arithmetic is exact and the series is contiguous. What the check shows is a short, calm sample
— no move above 3% in 293 sessions — where persistence is weak, and a sample whose standard error on
an IC (≈0.059) is too wide to see a 0.1–0.2 effect reliably. So the earlier nulls are not explained by
an arithmetic defect, and they are also not strong evidence of absence: **this 14-month exploratory
panel is too short and too quiet to act as a sensitivity check even for a well-documented effect.**
That is the finding, and it bears on how much any future exploratory measurement on this local store
should be trusted. Nothing was re-run, re-parameterised or re-selected because of it.

## Isolation, measured against the real paths

`isolation_probe.py` at `2026-09-13T02:57:29Z`, through `select_isolated_backend(autoexperiment.POLICY)`
— the backend the stage uses. The parent read one byte of every target first, so a denial is
containment and not absence. No secret value was printed by either side.

**Static layer** — the naive read of the deployment dotenv is refused before anything runs:
`forbidden-literal — .env`.

**Kernel layer** — every sensitive string assembled at run time so the scan cannot see it:

```
deployment-dotenv (Alpaca paper credentials)   DENIED:PermissionError
live-durable-store                             DENIED:PermissionError
live-store-wal                                 DENIED:PermissionError
deployed-checkout                              DENIED:PermissionError
proof-store-copy                               DENIED:PermissionError
this-repository                                DENIED:PermissionError
user-profile-gitconfig                         DENIED:PermissionError
list C:\Users\leepa                            DENIED:PermissionError
write D:\Elrond\state\...                      DENIED:PermissionError   (file absent afterwards)
connect 127.0.0.1:11434 (model server)         DENIED:TimeoutError
connect 127.0.0.1:8787 (console)               DENIED:TimeoutError
resolve paper-api.alpaca.markets               DENIED:gaierror
child environment ALPACA_* / QUANTBOT_*        [] / []
control: write+read inside the workspace       OK
```

## PAPER boundary after the proof (Saturday; closed-market indicators, not trading activity)

* Live store `D:\Elrond\state\quantbot-paper-fresh-20260908.db` and `-wal`: **SHA-256 identical** to
  the pre-session baseline (`36659f02…` / `a94be059…`); db, wal, shm mtimes and sizes identical.
* Deployment dotenv unchanged since 2026-09-08 12:56 EDT: `TRADING_MODE=PAPER`,
  `BROKER_ENVIRONMENT=PAPER`, `LIVE_TRADING_ACKNOWLEDGED=false`, `KILL_SWITCH=false`, zero
  `ALPACA_LIVE_*` lines.
* Account identity: SHA-256 prefix of `EXPECTED_ACCOUNT_ID` (`e09a79ca34d782e3`) equals that of the
  latest account snapshot (captured 2026-09-11T20:05:00Z). Values not printed.
* Kill switch (from a copy taken after the proof): disengaged since 2026-09-10T21:15:23Z — unchanged.
  No kill-switch, readiness, reconciliation, risk, broker, execution or config source differs from
  `origin/main` on this branch.
* Reconciliation: latest run `run-20260911T200500Z` `RECONCILED` (Friday close). Coordinator
  observation: `COORDINATOR 5025b21 PAPER` at 2026-09-11T20:05:00Z.
* No orders: `order_intents` 11 / `broker_orders` 10 / `fills` 11 in the live copy, latest intent
  2026-09-09, latest fill 2026-09-10 — and identical counts in the proof store after four research
  runs. The research component removes broker secrets from its own environment before running, and
  the proof environment file held none.
* Services: `Elrond Console`, `Daemon`, `Protection`, `ElrondModelServer` Running; their process trees
  date from 2026-09-09/10/11, before this session. Console answers HTTP 200.
* `Elrond Ingestion`, `Research`, `Report` still refused with `CHECKOUT_NOT_CLEAN` — the deployed
  checkout at `5025b21` still carries ` M PROJECT_STATUS.md`. That is the status-file deadlock, unfixed
  on the host until `claude/status-file-deadlock` is deployed; this session did not modify the deployed
  checkout.

## What was wrong with the first proof, and what it cost

1. **It read the holdout.** Its panel held all 307 stored sessions, so the 14 sessions from 2026-08-24
   were inside a measurement described as exploratory. Those statistics were published, so for the
   three questions measured those sessions are spent. The catalogue now records
   `exposed_through=2026-09-11` for each, and they have no clean session until new data arrives.
   Sessions for *other* questions are unaffected.
2. **Its verdict text claimed the statistic sized the confirmation.** Sizing used only the
   pre-registered effect.
3. **Its evidence was one task event.** No search record, no finding memory could recall.
4. **It was not autonomous in production.** The scheduled component never proposed; the proof ran
   `research-propose` by hand. It also ran from a dirty tree (`cba3f1c…-dirty`), and later fixes
   landed after it.
5. **Refusals left no durable record**, and the commit resolver would have recorded `unknown` under
   the service, whose working directory is the state directory.

## What this does not claim

* No edge was found or looked for beyond what the catalogue pre-registered.
* `UNDERPOWERED` is not a refutation of volatility persistence, and the exploratory statistic is not
  evidence against it — see the instrument check.
* The three empty datasets remain empty; this proves the loop, not new capacity.
* The live store was not migrated and the new code is not deployed. Deploying requires
  `migrate-database --confirm` on the live store, and nothing here did that.
