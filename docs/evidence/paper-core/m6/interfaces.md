# Implementation source map

Only `src/quantbot/paper_core`, additive storage version35/database/schema mapping, the new launcher and isolated core tests/fixtures changed. No global broker/coordinator/config implementation changed. Optional source code may remain in the pinned release inventory without becoming a runtime role dependency.

| Interface | Supported behavior | Authority/evidence |
|---|---|---|
| `release.py` | Deterministic prepare/verify; exact executable inventory; read-only status/export | Source/runtime/distribution/lock/config/schema pins; selected decision joins |
| `__main__.py`, launcher | Three roles; explicit prepare/verify/migrate/acquire/commission/fence/rollback | Runtime startup refuses uncommissioned PAPER; disable-only flags |
| `authority.py` | Typed initial commission, entry fence, independent heartbeat | Actual account/hash/endpoint/store/path/config/release/owner/generation; no self-ack |
| `runtime.py` | Narrow coordinator `_gate` and concurrency-safe `_adopt` | Retained parent outbox/client/send; exact intent comparison/ack checks |
| `alpaca_adapter.py`, `operational_costs.py` | Retained retail transport/clock/calendar/features/activity adapter | Bounded cursor/revisions; explicit component applicability; unknown remains unknown |
| `commissioning.py` | Future explicitly approved read-only fresh acquisition | Stable account/positions, no unknown/open orders, original owner map, canonical metadata |
| `canonical.py`, learning | Metadata-only fresh canonical protection and permanent trial import | `us-equities-daily`, current UTC guard independent from learning cutoff |
| `roles.py` | Bounded independent guardian/outcome scheduling and daily learning | Completed-session dedup, transient retry; no entry wait coupling |
| guardian/trader/sendguard/outcomes | Independent acknowledgement, owned exits, actual locked-send commission | Original protection/client/owner; no inherited adoption; cash vs cost separated |
| schema35 migration | Additive immutable docs/current pointers | Populated v34 typed IDs and omission semantics unchanged; forward-only recovery |

`CoreExecutionCoordinator._gate`: retained checks are validate_trading_authority/PAPER/expected account; actual account restrictions/current positions/inherited exposure; core50bps cap/25bps risk/100bps stop/BP500bps reserve; manual-stop/KILL on ENTRY; market eligibility; supported purpose; parent exit competing order/open-order/reduce-only checks; core original owned-plan quantity. Replaced policy inputs are optional research/general veto/drawdown rules, explicitly excluded rather than declared healthy. DRAWDOWN_LIQUIDATION receives no implicit approval. Both claim and locked send still enforce current model/config, uncertainty reservations, protection readiness and timing authority.

`_adopt`: demonstrated POST-ack/outcome-recovery overlap may move SUBMITTING to SUBMISSION_UNKNOWN while an acknowledgement returns. Under BEGIN IMMEDIATE reload current intent and compare all immutable fields before retained `_adopt`. A prior FILLED/partial/cancelled acknowledgement stays monotonic, and mismatched broker identity raises the existing conflict; no retry POST/client replacement occurs.

The supported initial operational store is the dated legacy schema29 store after a separately approved additive migration, with no existing core baseline/commission. v34 accepted synthetic populated fixtures prove compatibility, not permission to cast their account/model to PAPER. Release/owner upgrades are explicitly refused; future transitions require separately reviewed protocol rather than table/payload identity overwrite. Existing core plans remain executable under the same pinned generation, while legacy plans retain their distinct legacy runtime.
