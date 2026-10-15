# Affordable stocks and whole-share PAPER handover — October 15, 2026

Measured at 2026-10-15T19:40:44.173918+00:00. The approved entry amendment is active locally on the HP Omen.
The feature branch is `hutch/whole-share-fallback`; merging its PR is separate from this deployment.

## Behavior

Retain the original ten stocks and add F, T, PFE, VZ, HPQ, SOFI and UBER. Prefer fractions
when explicitly supported; otherwise floor ordinary sizing to whole shares with an exact
one-share minimum. This minimum alone may exceed the 0.5% notional cap. Cash, 95% of buying
power, 25 bps planned loss at the unchanged 1% stop, one open lot and six daily reservations
remain enforced. Ordinary sizing targets 99% of the normal allocation as equity-mark headroom.
Typed asset evidence is reacquired before POST; a capability change refuses the original
intent atomically. Unknown eligibility grants no permission.

The first actual AAPL BUY filled 0.007228 shares at $342.402 on 19:00:31 UTC, under the
preceding executable. Its owned exit completed under the compatible code-only bridge,
before the entry amendment. This is a real broker-PAPER lifecycle, distinct from offline
whole-share tests. Current core fills: 2; completed lots: 1.
The first closed lot has gross P&L `0.003859752`, status `COSTS_PENDING`,
actual fees `None` and net `None`. Unknown does
not mean zero. No fit or activation is claimed; the ten-completed-session warmup and all
eligible compatible history with recent-ten weighting remain intact.

## Native handover

- Original executable: `aaa40bdbc8f49b8f63ad736740cbffb0fc00f9d14f96e344e0aba4d68c6d78d0`.
- Compatible code-only bridge: `d3d0701ee0f4409de8ee47a9b54984e73cbe80bd90356f737af385918122f351`.
- Initial amended executable: `172810ad8e4170e19e66235be3980b78cf9d56956d5ec4677127b5d2586256de`.
- Active corrected executable: `a93b508b76f318a030fb1813f8f0a06eae98cd919fadddcefa65df574bfce61e`; entry policy `ded04e1a1559a55731c452e283f2a29420ddafd1be083b9f6d2a3a8d14b645a4`.
- Operating store: `D:/Elrond/state/quantbot-paper-fresh-20260908.db`, schema36.
- Core root: `D:\Elrond\releases\paper-core-14352cc-20261015`; resolve the current manifest/binding before later work.
- Core source is a reviewed 17-file overlay, recorded in patch-plan.json. Dependencies and
  interpreter remain unchanged. The bridge, amended and corrected manifests pin native bytes.
  A final one-line trader-cycle universe validation correction was applied as a same-config
  code-only transition before entries resumed; it preserves the approved amendment.
- Dashboard uses clean commit381787e; retained Protection uses clean commit383c548 in its
  separate native checkout. Retained Deadline remained running at its existing pinned release.

Entries were fenced; old core writers were absent before installation; a WAL-aware backup
passed integrity checks. The bridge retained original entry/client IDs and owned exits.
Only after that lot closed did the explicit flat/fenced policy amendment run. Guardian and
outcomes published fresh current-bound readiness before audited entry resume. The original
commission, base config, model, activation, stop, learning origin and history were preserved.
Full approval documents, task XML and database backups remain local, outside this package.

The first fill also exposed retained Protection's dependence on full cash certification.
The fix adds a separate current position-certification reader. It preserves unknown cash/
fees, checks exact quantity and basis, and delegates a core lot only with matching fresh
guardian/plan evidence. Stale authority remains visibly blocked without transferring the
exit owner. Action creation and dispatch reject competing core ownership; possible sends
keep lookup-only recovery and original IDs. The retained service's measured coverage is
recorded in health-final.json and protection-check.json.

## Validation and remaining acceptance

See verification.json and the exact local test logs. The 144-test main gate, 66-test
protection/execution gate and frozen 78-test core gate passed. The later one-line cohort
correction passed its final 35-test trader/adapter/selection gate. Gates overlap and are
not additive full-suite totals.
Independent review accepted both changes after findings were fixed. Changed-file Ruff and
the two new policy/bridge modules' targeted type checks passed. No paid CI was dispatched.

A real nonfractionable whole-share fill still requires an eligible provider opportunity;
offline tests verify its entry, owned exit, refusal and accounting paths. Ten completed
sessions and reliable eligible outcomes are still needed before actual daily fitting.
These are acceptance limits, not evidence of profit improvement. Provider bars/capability
may still be unavailable; those remain scoped waits. No subscriptions, manual orders,
short selling, inherited liquidation or stop clearance were used.

At the last 19:41 UTC check, the provider returned eight current five-minute bars for all
17 candidates and SPY. The 19:40 cutoff's full 30-minute feature target would be 20:10 UTC,
past the verified 20:00 session close, so no new entry cohort was eligible. This is the
preserved full-horizon rule, not evidence that the available bars are stale. The generic
runtime message is FRESH_COMPLETE_BAR_UNAVAILABLE; late-session-wait.json records the cause.

The routine recovery API resolved exactly two obsolete guardian inventory waits using a
newer certified proof and fresh independent guardian/time evidence while entries remained
fenced. scoped-recovery.json records both old event IDs, proof/time IDs and the unchanged
manual stop. Fresh trading bars were not asserted; the resumed trader retains normal bar waits.

## Rollback

Fence entries and retain guardian/outcomes/protection. Wait for verified flat core ownership
and resolve possible sends using original IDs. Use an explicit exact-current entry-policy
transition to the prior policy (None), preparing a forward rollback manifest from the active
verified executable with only entry_policy changed. Use the bridge configuration as the
prior-policy reference; its older executable hash is not the active corrected byte set.
Preserve current history and approval evidence, and retain code that understands amended
payloads. Do not restore the older AAA binary over newer policy bindings, rewind a database
or clear an explicit stop.

hashes.json seals this selected nonsecret package. Earlier evidence packages remain unchanged.
