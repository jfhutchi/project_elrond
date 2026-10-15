# Worker environment identity across execution backends

> **PAPER core identity boundary.** Environment/release measurements remain distinct from economic policy and immutable model identity in [PAPER core](paper-core.md). A dependency or executable repair changes release identity; it may retain policy/model lineage only through the audited compatible binding. Do not discard compatible experience merely because the code SHA changes, or silently reuse outcomes after a semantic contract change. The historical worker default below remains specific to those research backends.

**Status:** `PROJECT_DEFAULT` · **Version:** 1 · **Recorded:** 2026-08-30 · **Issue:** #137

**Provenance:** chosen by Claude (Opus 5) under CLAUDE.md's rule that an unspecified operator
preference is not a blocker when a conservative, reversible, versioned default can be selected
from existing evidence. **This is not an operator ratification.** No human has approved it; it is
an agent-chosen default, and any artifact measured under it says so.

---

## The decision

**Environment identity is recomputed per environment. It is never held constant across execution
backends.**

Three consequences, all deliberate:

1. A worker running in a container has a **different** environment identity from the same worker
   running in a local venv, because it is a different environment — different base image,
   different resolved wheels, possibly a different libc.
2. Existing forecasting evidence keeps `kronos-worker-py311-requirements-v1` **permanently**. It
   is never relabelled, re-attributed, or migrated to a new identity.
3. Forecasts produced under two different environment identities are **not pooled** without an
   explicit bridging measurement.

## Why this and not the alternative

#136 makes a second execution backend possible, and #137 observed that this forces a choice with
two honest but incompatible answers.

**Holding the identity constant across environments** keeps evidence comparable and costs nothing
up front. It was rejected because the identity would then no longer describe what actually ran.
That is the "a control correct in isolation and never reached in production" failure applied to
provenance — and provenance is the one thing in this system that has no independent check. A
result whose recorded environment is wrong cannot be caught by looking at the result.

**Recomputing per environment** is the option that refuses more, which is CLAUDE.md's stated
tiebreak: a false refusal costs a comparison somebody has to earn back with a measurement, and a
false permit costs the ability to ever tell which environment produced a number.

## What it costs, stated plainly

The first containerised forecast starts a **new evidence series**. It is not comparable to the
existing venv series, and the existing series does not transfer. If shadow forecasting has
accumulated *n* sessions of evidence under the venv identity, moving to a container resets that
count to zero for the purpose of any claim that depends on the environment.

That is a real cost and it is not hypothetical. It should be paid deliberately, not discovered.

**Mitigation, when a container backend lands:** run both environments in parallel over an overlap
window and measure agreement directly. That produces a bridging measurement — evidence that the
two environments produce the same forecasts — rather than an assumption that they do. Until that
measurement exists, the two series stay separate.

## What a container backend must derive its identity from

An **image digest**, not a requirements filename.

`kronos-worker-py311-requirements-v1` names a file that describes an intended environment. An
image digest names the environment itself, byte for byte. It is a strictly stronger attestation,
and it is the thing a container can actually prove about itself.

This matters beyond tidiness. The pinned-artifact verification is what made the then-open
`torch==2.5.1` advisories unreachable (#117): the loader requires exactly one safetensors file,
hash-verified, so a pickle never reaches `torch.load`. That argument is environment-specific. A
container backend has to carry an equivalent argument, and its test has to assert it *there*, not
inherit the venv's.

## The first time this decision actually bit

#200 moved the worker to `kronos-worker-py311-requirements-v2` on patched `torch 2.13.0`. Until
then this document described a rule with one identity in existence, so nothing could violate it.

The qualification measured what the rule predicts and found it true: v1 and v2 diverge on 236 of
240 sampled values on the same frozen input, while each reproduces itself exactly at a fixed
seed. They are two forecasters. v1 forecasts keep `kronos-worker-py311-requirements-v1`
permanently; v2 starts a new series; the two are not pooled. The full measurement, the tolerance
policy it was read against, and its limits are in
[`kronos-worker-v2-qualification.md`](kronos-worker-v2-qualification.md).

Nothing pools forecast evaluations today -- no aggregation surface over them exists yet -- so
this is a constraint on the first one built rather than a defect being fixed. The identity is on
every row inside `request_json` and is baked into `forecast_id`, since the request id hashes the
config; promoting it to a queryable column is worth doing when something needs to group by it.

## Superseding this

Changing the default creates **version 2**. Evidence measured under version 1 keeps the authority
it was measured under and is never relabelled — the same rule the veto policy versions follow
(#75, #74).

An operator may ratify, amend or reverse this at any time. Until they do, it is a project default
and every artifact that depends on it says so.

## Mechanical guard added alongside this decision

`kronos_worker.py` cannot import from `quantbot`, because it runs in an isolated venv. So the nine
pinned constants — model and tokenizer ids, revisions, weight and config SHA256s, and the
environment id — exist **twice**, once in the worker and once in `forecasting/models.py`.

Nothing asserted the two copies agreed. They did agree when checked on 2026-08-30, so this was a
latent risk rather than a live defect: had they drifted, `KronosConfig` would have claimed one set
of pins while the worker verified another, and a forecast would have been attributed to an
identity that did not describe what verified it.

`test_the_two_copies_of_the_pinned_contract_agree` now asserts it. The duplication itself is
correct and stays — the isolated venv is what makes the attestation meaningful.
