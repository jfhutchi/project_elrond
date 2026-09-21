# Learner dashboard

The operator console now exposes `/learner` and `/api/learner`. Operations and Research link to
the new page. `/api/state` contains the same learner projection. Refresh reads the latest saved
records; there is no polling worker, training action, order action or promotion action in this UI.

The page shows model versions and training commits, historical/prospective/shadow forecast
counts, unresolved outcomes, counted fit attempts and evidence timestamps. Saved portfolio
reports supply the equity chart, after-cost return, SPY excess, drawdown, rank IC, cost, turnover
and exposure. Missing results remain unknown; a deployment declaration never proves liveness.

## Data contract

Configure `QUANTBOT_LEARNER_DIRECTORY` as an absolute path in the console environment file.
The directory contains `state/learner.db`, optional `deployment.json`, and the existing immutable
`evidence/mean-economics.json`, `ridge-economics.json`, `ridge_successor-economics.json` reports.
The API deliberately returns a limited projection, not raw model documents, arbitrary metadata,
execution inputs, environment files, account identifiers or full paths.

The reader opens SQLite with `mode=ro`, `query_only=ON` and a read transaction. It cannot create
or migrate a database. It does not import the learner service, recompute fits or construct a
broker. This allows the schema-28 operational console to read the separate schema-29 learner
store without upgrading the operational database. Model document hashes, report hashes,
model/family/successor identity, execution contract, comparison dates and baselines are checked.
Invalid reports are excluded; incompatible stores do not break other dashboard pages.

Report hashes establish local integrity, not provider authentication or new market evidence.
Only the existing next-open/fifth-close, 5-bps-per-side execution contract is displayed under
those assumptions. Distinct model selections may differ while comparison dates remain matched.
Dates, timestamps, family and hashes are validated; credential-shaped scalar text is rejected.

## September 21, 2026 rollout

- Development commit: `22dfab668cc5115e28f012b8f246b73c2859de47` on `codex/elrond-learner`.
- Console-only release: `d23b2b55ec8599a2946cda3c7bfe68c1544018de`, the dashboard commit applied
  to the existing operational base `a49203d`, in `C:/Users/leepa/elrond-console-runtime`.
- Only `Elrond Console` was updated/restarted. Its existing native task host, Interactive user
  `leepa`, triggers and permissions are retained. The console environment is
  `C:/Users/leepa/.config/quantbot/quantbot-console.env`, copied with the original ACL from the
  existing environment and extended with the learner directory. Keep shared configuration changes
  synchronized deliberately; this is a separate environment file, not a live include.
- Dependencies installed from the local cache with a frozen lock and native Python on D:.
- Initial task startup returned launcher exit 70 using the previous launcher log. A bounded
  manual launcher probe confirmed the new application served successfully; that probe was then
  stopped. Changing only the task's launcher-log destination to
  `D:/Elrond/learner/logs/console-launcher.log` allowed the scheduled launch to succeed. No log
  was deleted and no ACL/security setting was weakened. The precise cause of the old-log failure
  was not established; do not claim it was a code or permission fix.
- Measured server ancestry: Python → PowerShell → native service host → Task Scheduler's
  `svchost`, independent of Codex. The console listener is `127.0.0.1:8787`; no network binding
  was widened. The page was opened and visually checked in the browser, including its chart.
- `Elrond Daemon` and `Elrond Protection` remained Running with identical task actions. Their
  runtime checkout remains `a49203d`. Operational schema remains 28; learner schema remains 29.
- Pre-change backup: `D:/Elrond/backups/pre-console-learner-20260921T155057Z.db`.
  Original console task XML: `D:/Elrond/backups/console-before-learner-panel.xml`.
  To roll back the console, stop only that task, restore that XML, and start that task. Original
  operational checkout and environment remain available; neither database needs restoration.

## Verification and displayed result

The exact console release passed **173 native Windows console tests**, including 11 new learner
dashboard regressions. Both Linux and win32 mypy targets passed over all 233 release source
files, and Ruff passed. Linux execution itself was not run; no Docker/WSL dependency added.
An independent review's report-label, malformed-metadata, credential-text and execution-contract
findings were fixed and reproduced in synthetic regression tests.

Live `/api/learner` returned NOT_SCHEDULED, 3,696 historical forecasts, 3,696 outcomes, zero
prospective/shadow forecasts, zero pending outcomes, three fits and no report errors. The page
renders the January 5–March 31, 2026 matched holding windows: mean/equal-weight -0.13%, ridge
and successor -3.46%, SPY -6.02%. These are saved exploratory historical results. The ridge
model underperforms the simple baseline and has negative rank IC; no edge or promotion is claimed.

Continuous forward measurement is still not implemented. The dashboard explicitly displays that
blocker, the preserved April–September holdout and the need to keep the 60-session lookback out
of protected observations. It must not be described as a live learner or automatic future
performance tracking until the missing forward-data loop is implemented and verified.
