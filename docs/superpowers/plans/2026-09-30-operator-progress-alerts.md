# Operator progress alerts implementation plan

Goal: first focused batch for #251–#253, distinguish learner progress, expected waits and
operator-required PAPER entry gates without changing evidence or trading authority.

Architecture: a dedicated SQLite alert store under the existing learner results directory;
one transaction records conditions, acknowledgement, reminders and recovery events. Worker
receipts and console project its status. Existing scheduled launcher stdout is the initial
notification route; external delivery remains explicitly unconfigured.

Constraints: PAPER only; no production writes, task changes, model promotion, merge or deployment.
Keep protected windows, kill switch and risk controls unchanged. Native inline execution is
selected to conserve credits; the user has authorized implementation and tests already.

- [x] Add failing lifecycle tests: immediate critical alert, dedup, restart persistence,
  acknowledgement distinct from resolution, reminder/escalation and recovery duration.
- [x] Implement `operations/alerts.py` with SQLite transactions, severity policy and stdout events.
- [x] Add failing progress tests: capture alone does not mean learning, closed/protected/warmup
  pauses do not age the watchdog, active-session stalls warn/turn critical, progress recovers.
- [x] Implement `learner/progress.py`; explicitly separate forecast, training and PAPER readiness.
- [x] Wire worker receipts, safe CLI acknowledgement and console projection; test integration.
- [ ] Verify focused regressions, lint, both mypy targets and full pytest; document runtime limits.
- [x] Push focused branch and open a draft PR against the unmerged learner branch. Keep issues
  open until broader delivery and deployment acceptance have actual evidence.

Focused checks, lint and both mypy targets pass. The initial full suite exposed environment
and fixture failures; bounded corrected reruns pass, but the final full suite and native
AppContainer qualification remain incomplete. See `docs/operator-progress-alerts.md`.
