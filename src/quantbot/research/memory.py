"""What this project has already learned, as records rather than recollection (#6).

`REFUTED.md` is the manual version of this and it is the highest-value artifact in the
repository: 24 refuted hypotheses whose measured effect is that sessions stop re-testing dead
ideas. Momentum ranking has now returned null on three structurally independent universes --
23 US sector ETFs (t=0.77), 595 survivorship-free single stocks (t=-2.08, a price-floor
artefact), and 22 global country indices (t=-0.78). Each was proposed because the previous null
had a plausible excuse. Without the written record, universe four gets proposed next week.

Four things are deliberate here.

**`UNDERPOWERED` is never allowed to become `REFUTED`.** They are distinct verdicts, and the
conversion is blocked rather than merely discouraged: recording an untestable hypothesis as
refuted teaches a later agent that a mechanism was tested and failed when it was never tested.

**Structural limits are curated, not generated.** The most valuable content in `REFUTED.md` is
not summaries of failures, it is the limits derived across them -- that execution cost rather
than prediction is usually binding, that resolving a Sharpe gap of 0.074 needs ~700 years, that
the drawdown halt forbids holding the one benchmark that beats everything built here. An LLM
summariser produces "momentum did not work" and loses the part that changes future decisions.
A `SUMMARY` record can never be the only carrier of a fact, and deleting one destroys nothing.

**Analysis defects are first-class.** Nine are recorded, and the pattern is the point: every one
flattered the result, and none was caught by the code looking wrong. That is a calibration prior
on every number in the repository and it is the part most likely to be lost in a migration.

**Relational, no embeddings.** At tens-to-hundreds of hypotheses, exact overlap on universes and
features plus keyword recall carries this. Embeddings and a graph database are complexity ahead
of evidence; the seam for them is `recall`, which is one function.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.research.manifest import content_id
from quantbot.research.registry import (
    DUPLICATE_OVERLAP,
    EXHAUSTED_TRIALS,
    HypothesisRegistry,
    NoveltyReport,
    WindowConsumption,
)
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.schema import research_records, research_relationships

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RecordKind(StrEnum):
    FINDING = "FINDING"
    #: A limit that bounds all future work. Curated by a human, never generated.
    STRUCTURAL_LIMIT = "STRUCTURAL_LIMIT"
    #: An error found in this project's own analysis. Kept because the error rate is evidence.
    ANALYSIS_DEFECT = "ANALYSIS_DEFECT"
    #: Derived prose. May be deleted without losing anything, by construction.
    SUMMARY = "SUMMARY"


class Verdict(StrEnum):
    REFUTED = "REFUTED"
    SURVIVED = "SURVIVED"
    #: The data could not resolve the claim. Not evidence against the mechanism.
    UNDERPOWERED = "UNDERPOWERED"
    #: The effect could not pay its own costs.
    UNECONOMIC = "UNECONOMIC"
    #: Measured, but outside a registration, so it cannot be cited as confirmatory evidence.
    EXPLORATORY_ONLY = "EXPLORATORY_ONLY"
    LITERATURE_SUPPORTED = "LITERATURE_SUPPORTED"
    LITERATURE_REFUTED = "LITERATURE_REFUTED"
    #: Not a research verdict: the thing cannot be done at all.
    IMPOSSIBLE = "IMPOSSIBLE"


#: Verdicts that mean "we could not test this", which must never be read as "we tested this".
NOT_TESTED = frozenset({Verdict.UNDERPOWERED, Verdict.UNECONOMIC})


class Relation(StrEnum):
    REFUTES = "REFUTES"
    SUPPORTS = "SUPPORTS"
    UNDERPOWERED_FOR = "UNDERPOWERED_FOR"
    DUPLICATES = "DUPLICATES"
    MUTATES = "MUTATES"
    CONTRADICTS = "CONTRADICTS"
    DEPENDS_ON = "DEPENDS_ON"
    USES_DATASET = "USES_DATASET"
    CONSUMES_WINDOW = "CONSUMES_WINDOW"
    INVALIDATED_BY_DEFECT = "INVALIDATED_BY_DEFECT"


class MemoryError_(ValueError):
    """Raised when a write would corrupt what the record means."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ResearchRecord(FrozenModel):
    """One durable thing this project knows, and what backs it."""

    record_id: Text
    kind: RecordKind
    #: The topic this is about, so recall is a query rather than a re-read of every record.
    subject: Text
    statement: Text
    verdict: Verdict | None = None
    hypothesis_id: str | None = None
    hypothesis_version: int | None = Field(default=None, ge=1)
    #: Manifest hash of the bundle evidencing this, when there is one.
    evidence_hash: str | None = None
    #: Where it came from: a cycle, a file, an agent. Preserved verbatim on import.
    source: Text
    curated: bool = False
    recorded_at: datetime
    detail: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if (self.hypothesis_id is None) != (self.hypothesis_version is None):
            raise ValueError("a hypothesis reference needs both id and version")
        if self.kind is RecordKind.FINDING and self.verdict is None:
            raise ValueError("a finding must carry a verdict")
        if self.kind is RecordKind.STRUCTURAL_LIMIT and not self.curated:
            raise ValueError(
                "a structural limit must be curated: a generated one loses the part that "
                "changes future decisions"
            )
        if self.kind is RecordKind.SUMMARY and self.verdict is not None:
            raise ValueError("a summary cannot carry a verdict; it is derived, not evidence")
        return self


class ResearchMemory:
    """Read and write durable research knowledge inside one caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._registry = HypothesisRegistry(session)

    def record(self, entry: ResearchRecord) -> bool:
        """Store one record. An exact repeat is accepted; a conflicting one is refused."""
        existing = self.get(entry.record_id)
        if existing is not None:
            if existing == entry:
                return False
            raise MemoryError_(f"record {entry.record_id} already exists with different content")
        if entry.kind is RecordKind.FINDING and entry.verdict is Verdict.REFUTED:
            self._refuse_refuting_what_was_never_tested(entry)
        self._session.execute(
            research_records.insert().values(
                record_id=entry.record_id,
                kind=entry.kind.value,
                verdict=None if entry.verdict is None else entry.verdict.value,
                subject=entry.subject,
                statement=entry.statement,
                hypothesis_id=entry.hypothesis_id,
                hypothesis_version=entry.hypothesis_version,
                evidence_hash=entry.evidence_hash,
                source=entry.source,
                curated=entry.curated,
                recorded_at=encode_utc(entry.recorded_at),
                document_json=entry.model_dump_json(),
            )
        )
        return True

    def _refuse_refuting_what_was_never_tested(self, entry: ResearchRecord) -> None:
        """The conversion #6 exists to prevent, blocked rather than discouraged.

        A hypothesis whose power assessment says the data could not resolve it has not been
        tested. Filing it as `REFUTED` would tell a later agent the mechanism failed.
        """
        if entry.hypothesis_id is None or entry.hypothesis_version is None:
            return
        assessments = self._registry.list_assessments(entry.hypothesis_id, entry.hypothesis_version)
        blocking = [
            assessment
            for assessment in assessments
            if assessment.verdict.value in {verdict.value for verdict in NOT_TESTED}
        ]
        if blocking:
            raise MemoryError_(
                f"{entry.hypothesis_id} v{entry.hypothesis_version} is "
                f"{blocking[-1].verdict.value}, which means it was never tested; record it as "
                "that verdict rather than as REFUTED"
            )

    def get(self, record_id: str) -> ResearchRecord | None:
        row = (
            self._session.execute(
                select(research_records).where(research_records.c.record_id == record_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return ResearchRecord.model_validate_json(str(row["document_json"]))

    def relate(
        self, source_id: str, relation: Relation, target_id: str, *, now: datetime, note: str = ""
    ) -> None:
        existing = self._session.execute(
            select(research_relationships).where(
                research_relationships.c.source_id == source_id,
                research_relationships.c.relation == relation.value,
                research_relationships.c.target_id == target_id,
            )
        ).first()
        if existing is not None:
            return
        self._session.execute(
            research_relationships.insert().values(
                source_id=source_id,
                relation=relation.value,
                target_id=target_id,
                recorded_at=encode_utc(now),
                note=note,
            )
        )

    def related(self, source_id: str) -> list[tuple[Relation, str]]:
        rows = self._session.execute(
            select(research_relationships)
            .where(research_relationships.c.source_id == source_id)
            .order_by(research_relationships.c.relation, research_relationships.c.target_id)
        ).mappings()
        return [(Relation(str(row["relation"])), str(row["target_id"])) for row in rows]

    def recall(
        self, topic: str, *, kinds: Iterable[RecordKind] | None = None
    ) -> list[ResearchRecord]:
        """Answer "what have we learned about X" with records, not regenerated narrative.

        Keyword matching on subject and statement. Deliberately not embeddings: at this scale
        exact recall is sufficient, and a wrong semantic neighbour is worse than a missed one
        when the answer is used to decide whether to spend a holdout.
        """
        statement = select(research_records).order_by(research_records.c.recorded_at)
        if kinds is not None:
            statement = statement.where(research_records.c.kind.in_([kind.value for kind in kinds]))
        pattern = topic.strip().lower()
        return [
            record
            for record in (
                ResearchRecord.model_validate_json(str(row["document_json"]))
                for row in self._session.execute(statement).mappings()
            )
            if pattern in record.subject.lower() or pattern in record.statement.lower()
        ]

    def structural_limits(self) -> list[ResearchRecord]:
        return list(self.recall("", kinds=[RecordKind.STRUCTURAL_LIMIT]))

    def analysis_defects(self) -> list[ResearchRecord]:
        return list(self.recall("", kinds=[RecordKind.ANALYSIS_DEFECT]))

    def latest_recorded_at(self) -> datetime | None:
        raw = self._session.execute(
            select(research_records.c.recorded_at)
            .order_by(research_records.c.recorded_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return None if raw is None else decode_utc(str(raw))


# --- REFUTED.md import ---------------------------------------------------------------------

_BULLET = re.compile(r"^\*\s+(?P<body>.+)$")


def import_refuted_markdown(
    memory: ResearchMemory, path: str | Path, *, now: datetime
) -> list[ResearchRecord]:
    """Load `REFUTED.md` into records without rewriting what any of it meant.

    Statements are carried verbatim. Nothing is paraphrased, condensed, or re-judged: the
    historical meaning of a row is the row. Verdicts come from the file's own words, and
    `IMPOSSIBLE` is not flattened into `REFUTED` because they are different facts.

    Each section is read in the shape it is actually written in -- tables as rows, structural
    limits as bullets, and the defect log as paragraphs. A parser that only handles one shape
    would silently drop the defect log, which is the section most worth keeping: it is the
    calibration prior on every number in the repository.
    """
    sections = _sections(Path(path).read_text(encoding="utf-8"))
    records: list[ResearchRecord] = []
    source = str(path)

    for line in sections.get("Refuted with measurement", ()):
        entry = _import_refuted_row(line, path=source, now=now)
        if entry is not None:
            records.append(entry)

    for heading, lines in sections.items():
        if heading.startswith("Not refuted"):
            records.extend(
                entry
                for entry in (_import_survivor_row(line, path=source, now=now) for line in lines)
                if entry is not None
            )
        elif heading.startswith("Structural limits"):
            records.extend(
                entry
                for entry in (
                    _import_bullet(line, RecordKind.STRUCTURAL_LIMIT, path=source, now=now)
                    for line in lines
                )
                if entry is not None
            )
        elif heading.startswith("Defects found"):
            records.extend(
                _import_paragraph(paragraph, path=source, now=now)
                for paragraph in _paragraphs(lines)
            )

    for entry in records:
        memory.record(entry)
    return records


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    heading = ""
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            sections[heading] = []
        elif heading:
            sections[heading].append(line.rstrip())
    return sections


def _paragraphs(lines: Sequence[str]) -> list[str]:
    """Blank-line-separated blocks, joined back into single strings, verbatim."""
    blocks: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            blocks[-1].append(line.strip())
        elif blocks[-1]:
            blocks.append([])
    return [" ".join(block) for block in blocks if block]


def _import_refuted_row(line: str, *, path: str, now: datetime) -> ResearchRecord | None:
    cells = _cells(line.strip())
    if len(cells) < 4 or not cells[0].isdigit():
        return None
    number, subject, verdict_text, evidence = cells[0], cells[1], cells[2], cells[3]
    return ResearchRecord(
        record_id=f"refuted-{number}",
        kind=RecordKind.FINDING,
        verdict=_verdict_from(verdict_text),
        subject=_plain(subject),
        statement=f"{subject} -- {verdict_text}. {evidence}",
        source=f"{path} #{number}",
        curated=True,
        recorded_at=now,
        detail={"row": number, "verdict_text": verdict_text},
    )


def _import_survivor_row(line: str, *, path: str, now: datetime) -> ResearchRecord | None:
    cells = _cells(line.strip())
    if len(cells) < 3 or cells[0].startswith("---") or cells[0] == "Mechanism" or not cells[0]:
        return None
    mechanism, effect, cost = cells[0], cells[1], cells[2]
    return ResearchRecord(
        record_id=f"survived-{content_id(mechanism)[:16]}",
        kind=RecordKind.FINDING,
        verdict=Verdict.SURVIVED,
        subject=_plain(mechanism),
        statement=f"{mechanism}: {effect}. Cost: {cost}",
        source=path,
        curated=True,
        recorded_at=now,
    )


def _import_bullet(
    line: str, kind: RecordKind, *, path: str, now: datetime
) -> ResearchRecord | None:
    match = _BULLET.match(line.strip())
    if match is None:
        return None
    return _record_from_text(match.group("body").strip(), kind, path=path, now=now)


def _import_paragraph(paragraph: str, *, path: str, now: datetime) -> ResearchRecord:
    return _record_from_text(paragraph, RecordKind.ANALYSIS_DEFECT, path=path, now=now)


def _record_from_text(body: str, kind: RecordKind, *, path: str, now: datetime) -> ResearchRecord:
    return ResearchRecord(
        record_id=f"{kind.value.lower()}-{content_id(body)[:16]}",
        kind=kind,
        subject=_plain(body).split(".")[0][:120],
        statement=body,
        source=path,
        # Imported from a human-written file, so it stays curated on the way in.
        curated=True,
        recorded_at=now,
    )


def _cells(line: str) -> Sequence[str]:
    if not line.startswith("|"):
        return ()
    return [cell.strip() for cell in line.strip("|").split("|")]


def _plain(value: str) -> str:
    return value.replace("**", "").replace("`", "").strip()


def _verdict_from(text: str) -> Verdict:
    upper = _plain(text).upper()
    if "IMPOSSIBLE" in upper:
        return Verdict.IMPOSSIBLE
    if "LITERATURE" in upper:
        return Verdict.LITERATURE_REFUTED
    return Verdict.REFUTED


__all__ = [
    "DUPLICATE_OVERLAP",
    "EXHAUSTED_TRIALS",
    "NOT_TESTED",
    "MemoryError_",
    "NoveltyReport",
    "RecordKind",
    "Relation",
    "ResearchMemory",
    "ResearchRecord",
    "Verdict",
    "WindowConsumption",
    "import_refuted_markdown",
]
