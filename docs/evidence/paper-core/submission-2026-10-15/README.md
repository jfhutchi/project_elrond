# Submission readiness repair — October 15, 2026

The reviewed fix is installed on the HP Omen; entries were explicitly resumed at 13:13
Eastern. Guardian/outcomes heartbeats advance without objections. Retained protection
assessed **HEALTHY, zero unprotected positions**, with no deadline objections at 13:16.
The console and all five trading/protection services run. At 13:18 there were **zero new
core fills, zero completed core round trips and zero learning runs**. Service startup
and local tests do not establish an operational trade.

## Fixed causes

- Whole-account equality made ordinary equity marks reject entries despite unchanged
  spending authority. The gate now compares account identity, currency, cash and buying
  power, and still enforces the current equity risk limits.
- Paired PAPER acquisition also rejected moving market marks. Under the existing PAPER
  balance policy, equity and position marks may move while restrictions, spending
  authority, quantities and entry basis remain stable. Both real snapshots are retained.
- A fresh pre-POST refusal could leave ERROR without an unsent fact, reserving the slot
  forever. Refusal evidence and ERROR now commit atomically; a failed commit rolls both
  back. POST ambiguity remains reserved for recovery.
- Proven unsent entries no longer generate missing-order outcome complaints or redundant
  order lookups. They create no synthetic outcome or training label.

One old stranded ERROR was reconstructed using its original authorization event and the
same-process heartbeat 0.279 seconds later. The old source could produce that cash-gate
refusal and ERROR combination only before POST. Earlier entries had durable refusals
before this entry was created, excluding competing prepared entries. Exact evidence and
ERROR/no-ack state were revalidated under the writer lock. Fresh exact-client absences,
no symbol position and no subsequent symbol fill were corroboration, not the sole proof.
The original ERROR and client ID remain unchanged. See historical-refusal-reconstruction.json.

## Native deployment

Executable: `aaa40bdbc8f49b8f63ad736740cbffb0fc00f9d14f96e344e0aba4d68c6d78d0`.
Its base is source `14352cc` with seven reviewed source replacements from commit
`3de4fff606a6943d6f0f069b4c796fec4ad9c273`; it is not a full checkout of that commit.
patch-plan.json binds the exact old/new file bytes. RuntimeConfig, schema36, interpreter
and all 46 installed dependency distributions are unchanged. The executable manifest and
installed verification record the actual pins, including native file line endings.

Entries were durably fenced; all three core tasks were disabled/stopped and old processes
checked absent. Retained Protection/Deadline stayed running. An exclusive WAL-aware backup
passed integrity checking before the audit append. A second prepatch backup and original
executable files remain local. The existing guarded binding-transition and exact-fence
resume APIs were used. Guardian/outcomes provided two advancing clean independent polls
before trader restart. No paid CI was triggered.

The economic config remains `d17b148b8e6cf215ff9cde280da818c90a1146f118f45f96d40f4f9b0dd8c926`,
owner paper-core, generation 1, store UUID `cd2a5911-853a-484e-bbc8-d0c27b5d27aa`.
Commission, baseline, COLLECT model, learning origin/history and inherited ownership remain
intact. The separate manual switch retains its disengaged state and original timestamp.
No stop was cleared, old client resent, order canceled, position adopted or fill invented.

## Remaining acceptance blocker

At 13:18 Eastern, authenticated historical and latest IEX requests still returned 12:56
as the latest AAPL/SPY minute bar; the last five-minute start was 12:55. Raw/adjusted
requests agreed, with current HTTP Date headers. Recent SIP returned HTTP403 under the
existing entitlement. This is a measured stale-feed condition for these requests, not a
declared provider-wide incident. Alpaca's [status page](https://status.alpaca.markets/)
reported no incident that day; its [feed documentation](https://docs.alpaca.markets/us/docs/market-data-faq)
distinguishes IEX from paid recent SIP access.

The trader still reports NO_ELIGIBLE_COHORT_MEMBERS/FRESH_COMPLETE_BAR_UNAVAILABLE for its
ten stocks. It remains enabled and retries. Freshness, feed identity and entitlements
were not weakened, and no subscription was purchased. New broker submission/fill and a
protected round trip remain unverified until fresh eligible bars return.

The learner uses executed wins/losses as positive/negative return targets. V3 uses gross
return, keeping unavailable fees/net unknown. After ten completed exchange sessions it
considers daily fitting on all compatible eligible history, latest ten sessions weighted
2 versus 1 for older sessions. Validation/activation remain separate and improvement is not
guaranteed. No operational fit is claimed.

## Verification

The initial focused suite passed 57 tests. The final submission/final-send run passed 19,
overlapping that suite; ten field-contract checks also passed. Seven offline backup-clone
audit checks passed. Ruff and diff checks passed. Independent review found no substantive
blocker before maintenance. verification.json distinguishes the overlapping runs.

The helpers record a dated procedure, not reusable approval to rerun it. Databases,
credentials, raw account identity and private transition/resume approvals remain local
under `D:\Elrond`. Public artifact hashes are in artifact-index.json. The
[preceding readiness package](../readiness-2026-10-15/README.md) remains unchanged.
