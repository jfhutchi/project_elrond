# Adaptive PAPER source compatibility baseline

Implementation begins from approved plan commit `087dfc8c89d07986bded1c799fbf25450eb87189` in an existing isolated linked worktree. Native branch: `codex/adaptive-paper-coordinator`. No runtime/task changes are made to establish this baseline.

| Retained component | Source revision | Integration status |
| --- | --- | --- |
| Exclusive five-minute PAPER entry | `5c1db64f427c45f9ee99f8710568a1b82b59b3ef` | Ancestor of execution base; gateway, bindings, clock and risk gates retained |
| Observational five-minute contract | `054ed1ac2a07893a867fe75de5632e10e92b9bd9` | Existing contract/producer available; native observer remains separately pinned |
| Console and component liveness | `4e643ae6aef82ad6def47f9a2d920b616b7cc14f` | Direct ancestor; no changes in current console/monitor subtree relative to it |
| Protection/deadline/daily close | `90490a3aabae5c49c2381d97c322cf3ba2b4d045` | Existing compatible schema0029 foundation; retained safety behavior |
| Guarded trade tracking | `2ccd3d22a62082c8e617a025ea73088f7f751278` | Direct ancestor; raw shadow artifacts and observation-only service not changed |
| Approved news shadow research | `4ee6e5ff82355dfb2821f9f187406e661ae875ee` | Separate local source repository; source object imported without network. Three isolated feature commits add ten files; import under Task3 tests, not runtime |

New coordinator uses an additive separate experiment store, existing `IntradayPaperServices`, `PaperModelBinding`, `PaperDeadlineSupervisor`, and retained portfolio veto/sizing. No operational migration is planned. If durable independent exit ownership cannot be represented safely using the existing handoff/orders interface, stop that integration for an exact migration review.

News source commits for later tested import: `d288c16` journal, `508a01a` bounded provider mapping, `d3be36d` observational launch. The old `fit_shadow_challengers` function fits ridge and quadratic for a one-instrument study; the new pooled constant/ridge experiment uses estimator primitives and must not reuse that trial-expanding helper.

Initial verification: existing tracking/raw-return test group, 28 passed in 5.17 seconds. Further retained tests and new component tests are recorded in the execution ledger. Source compatibility is not a fresh runtime-health assertion. No account values, holdings, provider credentials or private runtime logs are published here.
