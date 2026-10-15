# Approved affordable-stock and whole-share fallback

The operator selected: add affordable liquid stocks and allow a one-share minimum while
keeping the existing loss budget. Retain the original ten stocks and add F, T, PFE, VZ,
HPQ, SOFI and UBER. This is a bounded extension of the existing cohort/sizing/send flow.

Use fractions when supported. When a verified asset response explicitly says fractions
are unsupported, floor normal sizing to whole shares, with an exact one-share minimum.
The minimum may exceed the 0.5% notional target; it must still fit cash, 95% of buying
power and the existing planned loss ceiling of 25 basis points of equity at the unchanged
1% stop. Never round to two shares merely to force a trade. Unknown eligibility blocks.
The amendment targets 99% of the normal 0.5% allocation before rounding, leaving 1% of
that target as headroom for ordinary equity marks during guardian handoff. The actual
0.5% maximum remains unchanged except for the explicitly authorized one-share minimum.
One concurrent core lot, six daily reservations, confidence-first/exploration selection,
30-minute horizon, 120-second exit buffer and all owned-quantity exit rules remain intact.
No short selling, inherited-position liquidation or owner adoption is introduced.

Represent this as an optional, immutable RuntimeConfig entry-policy amendment, with its
own ID on decisions and a distinct audited entry-policy binding transition. Base economic
CoreConfig, commission, baseline, active model, original learning origin and all historical
rows remain immutable. The transition requires explicit exact old/new/fence/activation
approval, old-writer fencing and no open or ambiguous core lot; ordinary code-only
maintenance still requires the full configuration to remain equal. All configuration
outside this amendment must be identical. Older payloads omit absent optional fields and
retain their hashes. Console readers must understand the new binding/config variant.
An explicit exact-current amendment may restore the prior entry policy while flat; this
is an audited policy transition, never deletion of the amendment or its trade history.

Capture typed asset eligibility with each new observation. Reacquire it before actual
POST, recheck cash/positions, and bind its fresh receipt to locked send authorization.
If fractions disappear after sizing, refuse the original fractional intent atomically as
unsent; only a fresh later decision may choose another quantity. Never rewrite/replay an
old client ID. An uncertain POST still reserves its slot.

The exit/feature/return contracts are unchanged, so compatible wins and losses remain
eligible for all-history daily learning after ten completed exchange sessions, with the
latest ten weighted twice. Unknown fees/net remain unknown. Operational fills, completed
round trips, fits and activation must be reported separately.

Deploy only reviewed exact bytes with WAL-aware backups and a durable entry fence.
Retained Protection/Deadline stay running. Preserve owned exits if an old core lot is
open; wait for its verified closure before the policy transition. Start fresh guardian
and outcomes before guarded resume. Do not trigger paid CI or buy market-data access.
