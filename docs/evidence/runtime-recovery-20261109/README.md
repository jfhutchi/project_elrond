# November 9 repair — code deployed; entries remain safely fenced

**Latest status12:05EST:** [Alpaca case392277 submitted and acknowledged; original disposition pending](support-case-392277.md). The operator authorized the request and conditional guarded recovery. A fourth genuine news review was generated automatically at12:01EST. Trading remains fenced; no disposition was invented.

Earlier: [11:38–11:43 verification and recovered HTTP403 diagnostic](follow-up-verification.md). The cycle-level403 narrows the original request investigation but does not resolve it. [Reviewed support request](alpaca-support-request.md).

Verified 2026-11-09T11:13:14.975993-05:00 on Leannes-Laptop. **Trading has not resumed.** Four existing application roles run; the Trader task is intentionally disabled. No duplicate engine was started. This is a blocked handoff, not a claim of complete trading recovery or profitability.

## Implemented and deployed

Feature source ad2a6995fa02c5ff64d47dfd59da7acc2d0695fd and rejection-provenance source **553c51f0e4a8175c547bde818af75bb435d524c7** are installed as exact committed component deltas into D:/Elrond/releases/paper-core-14352cc-20261015. Runtime **7327ff3ccffd2f702f6dbdeae51398fb19bf7bd9c23e1957230637676e094dfc** verifies all **403 executable pins**. Schema37 and the exact existing runtime/economic configuration, including night/DST limits, remain unchanged. Account generation40 remains bound to the predecessor while entries are fenced; no entry rebind or resume was performed. Console remains pinned to a4e397186fd5e9b71385d08b5a00cb4446c9ac87.

- Quotes: an operating ETH quote arrived1.801209s old, then spent4.576047s in local checks before refusal. Repeated current-ledger hydration was consuming its5-second lifetime. Consolidation reduces seven inventory validations to three across unlocked/locked crypto admission, using only immediate same-transaction results. Public entrypoints and the locked fence still revalidate fresh. Genuine provider-old quotes still fail; the5-second limit was not changed. Live submission performance remains unverified because entries are fenced.
- Guardian: each full capture used about10 noncritical requests. At15-second cadence this requested40/min against its existing36/min allowance, causing OWNED_PROTECTION_REQUEST_RESERVE. Full discretionary captures now pace at30seconds while time/exit checks continue at15seconds. Hints are valid only against the original current authority; changes invalidate them. Unresolved work reports INVENTORY_REFRESH_PENDING without a proof/readiness claim. Owned exits and their reserve are preserved.
- Learner: the old rejected BTC claim makes activation._boundary raise ACTIVATION_UNRESOLVED_CLAIM. Daily consideration previously minted fresh authorities and repeatedly threw StateConflictError. The boundary is now preflighted before minting, with narrow handling of expected conflicts both before and during locked activation. Learning considers normally and reports ACKNOWLEDGEMENT_DEFERRED; no claim or promotion gate was cleared.
- Future rejection evidence: actual HTTP errors now retain immutable bounded provider/method/path/status/numeric-code and canonical-request/response digests. Initial matching POST /v2/orders/client diagnostics commit atomically with REJECTED. Local failures, missing metadata, GET errors, timeouts and5xx do not masquerade as a submission rejection. A failed diagnostic write rolls back the state transition. Raw bodies, headers, free text and credentials are not retained. These are diagnostic receipts, **not** terminal/no-send authority, and no receipt was fabricated for the legacy BTC request.

## Running verification

Guardian PID18856, Outcomes29084, Research36468 and Console10648 were running. Trader: **0 active processes / task disabled**. Deadline and Protection legacy tasks remain disabled. Six dashboard routes returned HTTP200. Final role heartbeats were fresh.

Across the final deployed observation window: **37 distinct inventory proofs**; Guardian acquisition-retry errors0; learner LEARNING_RETRY:StateConflictError0. Expected scheduled inventory waits occurred. Outcomes continues to report the actual unresolved acknowledgement. See reason counts in the running evidence; these are bounded observations, not a guarantee of future availability.

Financial row counts and SHA256 fingerprints matched before/after both stopped-writer cutovers:4922 intents,334 broker orders,336 fills,154 completed round trips,195 outcome revisions. No economic history was deleted, rewritten or copied for tests. No schema migration or new ledger backup was performed. The one existing database backup remains; older log/report ZIPs are not ledger copies. Primary file size was 58022232064bytes; no storage shrink is claimed.

## Exact remaining external blocker

BTC/USD intent core-entry-fa5f1cabf5f085faa2b6360cba367aa0e07daabbb7c440dcf625028ec7b25ac2, client **qb-fa5f1cabf5f085faa2b6360cba367aa0e07daabb**, was created November8 at18:05:04.720194EST. Final locked authorization was18:05:52.496300EST. It is recorded REJECTED, but the original broker HTTP status/body/request disposition was discarded. Locked authorization does not prove a POST occurred. Current direct client lookup returns404; exhaustive crypto-order and position reads return0. **Absence/404 is not terminal evidence under the existing contract.**

An initial generation41 policy/lease handover correctly refused ENTRY_POLICY_CORE_NOT_FLAT. It was not activated. The supported code-only transition was then used with the **exact unchanged generation40 configuration**, preserving the entry fence and old claim. Four protection/research/dashboard roles were restored; the Trader was not started. The operator's explicit stops were not cleared.

I should have checked the resume boundary before starting the fence/cutover. A read-only resume_preflight.py and its BLOCKED result are retained under D:/Elrond/deploy/runtime-recovery-20261109 to prevent repeating that deployment-planning mistake.

Required next evidence: the authentic Alpaca request-level rejection/disposition matching this client and timestamp, or an actual broker order plus terminal activity evidence if an order was created. It must support an audited resolution; do not invent a BrokerOrder, delete the intent, infer no-send from404, or write a balancing entry. Current APIs contain no honest terminal receipt for this legacy request. After supported reconciliation, finish the ordinary guarded account-lease handover and fresh-readiness resume. The original-evidence question is pending; no further approval question was manufactured.

## Broker truth

At 2026-11-09T11:12:49.389563-05:00, independent paper-only GETs verified the expected account without exposing its ID. All334 returned orders were below the500-row page limit. Explicit crypto-order query:0. Crypto positions:0. Recent fill activities14, below the100-row page limit, all non-crypto. Canonical ledger likewise has0 crypto orders/fills/completed round trips. Intents remain BTC1REJECTED,ETH7ERROR,SOL11ERROR. Eighteen old refusals have explicit no-send evidence; BTC does not. No crypto execution date, actual cost or realized P&L exists. No demonstration trade was forced.

## Actual news learning and remaining gates

News documents **14042**, excluding SEC data; latest news page receipt **2026-11-09T16:12:06.255739+00:00**. Actual local inference receipts **4**; retained model-generated reviews **3**, including one genuinely generated November9 at10:00:57EST. The read-only eligibility join verified3 examples; it did not fit or promote anything. Current journaled context snapshots **73**; outcome feedback0; news candidate fits/evaluations/promotions0. Earlier generic FitReceipts from October6 are price work, not news fits.

Latest gate counts: [["COMPLETED_OUTCOME_UNAVAILABLE", 9], ["PREDECLARED_EVALUATION_CUTOFF_NOT_REACHED", 1]]. These are batch exclusion/gate counts, not failed-trade totals. Published eligible_examples0 is pre-evaluation bookkeeping; the separate pure join found3 retained eligible examples. No completed post-commission trade outcome exists to fabricate feedback from. Retained observations continue to be consumed from the backlog; the disabled Trader is not producing new pre-trade cycles.

Training began Nov7 01:00EST, held-out split Nov14 01:00EST, evaluation ends Nov21 01:00EST. Mechanical floors8training/4heldout across at least2dates in each partition remain unmet. Parent trial charges remain **2 of6**, measured read-only; no manual fit or budget reset. News remains **UNTRAINED_FIXED_LEXICAL_BASELINE / SHADOW_ONLY**; the active price model remains **COLLECT**, nonpredictive baseline. Teacher agreement is not ground truth or profitability. No news model currently influences trading decisions, and improved decisions/profitability have not been demonstrated.

## Tests and measured limits

Main affected release run: **589 passed /3 fixture failures in605.31s**. One synthetic review test used the real completion date against a fixed November6 as-of cutoff; its clock is now controlled, while production completion timestamps remain real. Two locked-send test spies lacked an existing context_research keyword; they now forward it to the actual guard. The complete affected files reran **14 passed in10.54s**. Thus every one of the592 selected cases was observed passing across the release run and corrected-fixture reruns; no single all-green592-case run is claimed.

Separate checks: quote/readiness21passed; Guardian/cadence12passed; learner/activation29passed; final broker rejection/transport/coordinator/crypto/learning gate **73 passed in44.51s**. All16 changed Python files pass Ruff and formatting; all8 production files pass mypy. The final formatting-only normalization preserved the Python AST.

The full repository command was attempted twice: an initial sweep was interrupted for final source changes; a final sweep was bounded while rebuilding historical fault/deployment fixtures. Neither completed. **No full-repository-suite pass or coverage percentage is claimed.** No remaining observed failure is being hidden. Successful synthetic sessions auto-cleaned; blocked diagnostic scratch is listed separately.

Source delta versus6f806ae: **249 lines added,17 removed**,8 production files touched,0 removed modules,0 dependencies added,0 services eliminated. This is not net code reduction. Controlled CPU/peak-memory/whole-cycle timing and live-fill latency comparisons are unavailable. Reduced validation repetition and acquisition scheduling are measured/tested mechanisms, not fabricated profitability or broad performance percentages.

## Rollback and cleanup

Both cutovers saved exact source/config/manifest/task predecessors under D:/Elrond/deploy/runtime-recovery-20261109 and its rejection-provenance subdirectory. For rollback: retain the fence, stop the same task trees, verify zero writers, restore only the appropriate source/config artifacts, verify/reseal the exact configuration and use the guarded code-only transition with current binding evidence. Preserve all economic rows, fit charges, claims and stops. Do not restore an old ledger or bypass lease/resume checks. No destructive ledger restore was rehearsed.

Automatic approval review rejected removal of13 new owned failed/interrupted-test directories with **blocked by policy**. Seven were measured at about2.10GB combined; six quote-test directories are also retained. No alternate deletion route was used. These are synthetic scratch, not operating-ledger copies. Exact paths are in cleanup-blockers.json. The old backup ZIP and two prior scratch deletions confirmed November7 are resolved and are not current blockers.

[Runtime](running-verification.json) · [Processes/tasks](process-verification.json) · [Broker GETs](broker-readonly.json) · [Financial history](financial-history-after.json) · [News audit](news-evidence-audit.json) · [Trial budget](trial-budget-verification.json) · [Measurements](measurements.json) · [Cleanup paths](cleanup-blockers.json)
