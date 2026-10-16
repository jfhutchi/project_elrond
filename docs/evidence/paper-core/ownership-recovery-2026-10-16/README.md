# Owned-fill quarantine repair and actual exit — October 16, 2026

Measured on the HP Omen at 15:03 Eastern. Source corrections `f5be9c1` and
`37dfaa4` on `hutch/whole-share-fallback` are deployed locally. This record
supersedes the protection-health conclusion in the [11:13 runtime audit](../runtime-check-2026-10-16/README.md).
The earlier JSON captures remain historical evidence.

## Actual GOOGL recovery and exit

GOOGL's 0.007112-share BUY filled at 11:11:25 Eastern at $344.346. The guardian
subsequently quarantined a transient inventory discrepancy while canonical fill
activities lagged the broker's stable order acknowledgement. Later matching
inventory proofs did not automatically resolve that integrity quarantine. Its
11:40:57 deadline passed without an exit. The earlier healthy heartbeat and
retained protection report did not reveal this blocked exit; that reporting was
incomplete and the earlier statement that the mismatch had cleared was incorrect.

The explicit resolver checked the original failed proof, exact original owned
order and protection plan, retained acknowledgement, later canonical fill sources,
unchanged position basis and fresh matching inventory, binding, guardian and time
evidence. It appended an audit receipt and resolved only the identified health
event. Historical evidence, ownership, client IDs and protection plans were retained.
The recovery helper has no broker submission interface.

The normal guardian then sold **all 0.007112 GOOGL shares at $339.558 at
14:55:03.377503 Eastern**, broker order
`536aee54-10bc-4235-8e8e-f383aeef3bb8`. The core recorded a closed round trip with
**gross P&L -$0.034052256** and a fresh independent inventory proof confirms zero
GOOGL shares. Actual fees and net P&L remain null (`COSTS_PENDING/FEE_UNAVAILABLE`).
The late exit exceeded the original planned stop loss; this was the incident being
repaired, not a change to the loss budget or stop policy.

## Prevention and reporting

The correction retains an exact owned acknowledgement as retry evidence even
while the original submission is unresolved, provided the locked-send authority
and order contract match. It grants no fill, cash, inventory or new-send authority.

After the initial deployment, the next automatic META entry exposed a second
interleaving: concurrent polls could leave the shared progress pointer referring
to an older acquisition after the current poll lost its compare-and-swap. The
guardian rejected that older timestamp despite its own valid immutable evidence.
The final correction carries each poll's exact acquisition ID through recovery
and classification. It checks ledger state, order identities, quantities, cash,
fill IDs and generation under the same writer transaction as classification and
health append. Crossed authority produces a retry instead of an integrity event.

META's corresponding evidence passed the same explicit audited recovery at 14:57.
Its original 0.003456-share position remains open under its original protection
plan; it was not exited early. The 15:03 inventory and delegation check confirms
its ownership and fresh guardian protection. Persistent blocking health now
appears in guardian objections, and retained protection independently checks it
before accepting delegation. Genuine contradictions remain quarantined.

## Deployment and current state

Entries were fenced for both code-only handovers. The three old core workers were
stopped and their absence checked; retained Protection and Deadline continued.
Consistent SQLite backups passed integrity checks. Schema, baseline, economic
commission, model, activation and manual-stop table hashes were preserved at each
handover. No configuration, strategy or risk amendment was made.

Final native executable:
`6a1672214339009b26f04cc6f1a2c2f3439dcdb9d09dfc9611af0e1a8c9cc773`.
Binding: `12a7196a0a1f9b508eb41896da987c3d41e9162ae2ed3bc99a9446162153ad50`.
Retained Protection uses the clean `37dfaa4` checkout with frozen dependencies.
The dashboard and retained Deadline service remain on their existing releases.

Entries resumed through fresh independent readiness evidence at 15:02:18.
All six services are Running; the dashboard returns HTTP 200. Guardian PID 15852,
outcomes PID 19620 and trader PID 5680 report fresh heartbeats against the final
executable. No active health quarantine remains. Retained Protection's 15:02:28
cycle evaluated all nine positions with zero unprotected positions. The trader
waits on META's occupied slot and fresh eligible bars; this does not mean another
entry has been submitted. The final executable has not yet observed another new
entry lifecycle at this capture.

## Learning and validation

Four completed gross outcomes are recorded overall: AAPL yesterday, and MSFT,
AMZN and GOOGL today. All four pass the approved gross target constructor. The
guarded selector currently accepts yesterday's outcome; today's three await
`SESSION_NOT_COMPLETED`. This preserves **one completed session out of ten**,
the COLLECT model and **zero fit attempts**. Three assessment receipts are not
three completed sessions or three fitted models. All eligible compatible history
and recent-ten weighting remain unchanged. No fees were guessed or assigned zero.

The affected local gates passed **60 distinct tests**: 19 ownership-recovery,
12 daily-loop, 25 retained-bridge/protection/final-send and four health-authority
cases. The lost-cursor regression failed before correction and passed afterward;
its final version also verifies that a competing SQLite writer cannot enter during
classification. Canonical-fill and order-state changes are covered separately.
Ruff, four-source-file mypy and independent review passed. A private database-copy
rehearsal validated GOOGL's retained evidence before operating recovery; its
explicit logical-path fixture is not evidence of production database identity.

The JSON captures contain exact patch/binding, writer fences, preservation hashes,
audited recovery and resume, services, inventory, costs and learner evidence.
`hashes.json` seals the 19 JSON records. Protected values were scanned before
publication. Credentials, raw account identifiers, task XML and database backups
remain local. No manual broker order, paid CI, subscription or PR merge occurred.
