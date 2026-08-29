"""What a refusal rested on, made durable rather than transient (#85).

#57 exists so Elrond can eventually ask whether a risk gate added economic value. That question
is only answerable **under the rule and the evidence that existed at decision time**, and the
persistence boundary was keeping only the first half. `RejectionReason` carried `gate_id`,
`gate_version`, `rule_parameters_hash` and a prose `detail`; the structured fields #74 and #81
added to the live decision -- which session was expected, which was observed, which dependence
matrix the clustering came from -- reached the ledger as English and nothing else.

Two refusals under the same `rule_parameters_hash` are not necessarily comparable. The same
freshness policy refuses a missing Tuesday and a missing Thursday; the same correlation threshold
refuses under a matrix measured in March and one measured in August. Pooling those because the
parameter hash matches is the same error as pooling across a threshold change, one level down.

**Reconstruction is not preservation.** The tempting shortcut is to recompute the context later
from today's calendar or today's bars. That answers a different question: what the rule *would*
refuse now, not what it *did* refuse then. The whole point of a point-in-time record is that the
world has moved.

**Generic, because the next veto will have evidence too.** Adding a column per structured field
would mean the next veto silently loses its evidence until someone remembers a migration. So a
reason carries a `kind`, a `schema_version` and a canonical payload, and `EVIDENCE_FIELDS`
declares exactly which keys each kind holds -- checked on construction, so a payload cannot be
half-filled. `tests/unit/risk/test_evidence.py` then asserts mechanically that every structured
field on `VetoRejection` is claimed by some kind, which is what stops the next one being
forgotten rather than a note asking people to remember.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceKind(StrEnum):
    """Which structured decision context a rejection reason carries."""

    #: The veto refused on state already covered by its parameters -- a limit breach with no
    #: contemporaneous context beyond the numbers in the policy. Written, and meaningful.
    NONE = "NONE"
    #: `STALE_MARKET_DATA` (#74): which session the calendar expected against which arrived.
    MARKET_FRESHNESS = "MARKET_FRESHNESS"
    #: `CORRELATION_CONCENTRATION` (#58/#59): which dependence snapshot the cluster came from.
    DEPENDENCE = "DEPENDENCE"
    #: **Never written.** Read back for rows persisted before evidence was durable, so a legacy
    #: row is honestly distinguishable from one that genuinely had no context to record. The
    #: difference matters: `NONE` is a measurement, this is an absence of one.
    LEGACY_UNRECORDED = "LEGACY_UNRECORDED"


#: The exact keys each kind holds. Exact rather than minimum: a missing key means a writer that
#: half-populated the payload, and an extra key means one writing a field no reader expects.
#: Both are version changes, and both should fail loudly at the boundary rather than reach the
#: ledger and be discovered by an analysis that silently skipped the rows.
EVIDENCE_FIELDS: Mapping[EvidenceKind, frozenset[str]] = {
    EvidenceKind.NONE: frozenset(),
    EvidenceKind.LEGACY_UNRECORDED: frozenset(),
    EvidenceKind.MARKET_FRESHNESS: frozenset(
        {
            "freshness_policy_identity",
            "expected_session",
            "observed_session",
            "market_state",
        }
    ),
    EvidenceKind.DEPENDENCE: frozenset({"dependence_snapshot_identity"}),
}

#: Bumped when a kind's field set changes. Stored per row, so a later reader can tell which
#: shape it is looking at instead of inferring it from which keys happen to be present.
EVIDENCE_SCHEMA_VERSION = 1


class DecisionEvidence(BaseModel):
    """The contemporaneous state one veto refused on, canonical and content-addressed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvidenceKind
    schema_version: int = Field(default=EVIDENCE_SCHEMA_VERSION, ge=1)
    #: Scalars only, already rendered to strings. A payload that needed a decoder to compare
    #: would be a payload two analyses could disagree about.
    payload: Mapping[str, str] = {}

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        expected = EVIDENCE_FIELDS.get(self.kind)
        if expected is None:
            raise ValueError(f"{self.kind} declares no field set, so its payload cannot be checked")
        if set(self.payload) != set(expected):
            missing = sorted(expected - set(self.payload))
            extra = sorted(set(self.payload) - expected)
            raise ValueError(
                f"{self.kind} evidence must carry exactly {sorted(expected)}; "
                f"missing {missing}, unexpected {extra}"
            )
        return self

    @property
    def identity(self) -> str:
        """Hash of the kind, the version and the payload, for grouping refusals by basis."""
        canonical = json.dumps(
            {
                "kind": self.kind.value,
                "schema_version": self.schema_version,
                "payload": dict(sorted(self.payload.items())),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"evidence-v{self.schema_version}-{digest[:16]}"

    @property
    def recorded(self) -> bool:
        """Whether this row's context was actually captured, as opposed to predating capture."""
        return self.kind is not EvidenceKind.LEGACY_UNRECORDED


#: What a row written before #85 reads back as. A constant rather than a constructed default, so
#: there is exactly one legacy marker and it is greppable.
LEGACY_EVIDENCE = DecisionEvidence(kind=EvidenceKind.LEGACY_UNRECORDED, payload={})


__all__ = [
    "EVIDENCE_FIELDS",
    "EVIDENCE_SCHEMA_VERSION",
    "LEGACY_EVIDENCE",
    "DecisionEvidence",
    "EvidenceKind",
]
